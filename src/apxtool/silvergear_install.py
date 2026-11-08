"""Backup-first, journalled full-chip write for the Silvergear 4 MiB flash from a forced BootROM.

Default is an offline preview. This module is the general counterpart of ``silvergear_recovery`` (which is limited to
three reviewed sectors): it writes the sectors in which a 4 MiB target image differs from the chip, using the
``silvergear-full-restore`` payload (944016 chip only, SRAM window only, its own request-flags value, no PM writes).
It serves two uses with the same code: restoring the verified stock image, and installing a packed SDK image
(``spi_image.py --board silvergear``). It does not enter BootROM, run DDR initialization, launch the SDK, change PM
registers, or clear write protection.
"""
import argparse
import hashlib
import json
import os
import struct
import time
from pathlib import Path

from .errors import ApxError, TransferError, UnsafeOperation
from .flash import _write_new_file
from .flasher import HEADER, MAGIC, PENDING, PayloadFlash, SRAM
from .rom_rescue import loader_sg_paths, open_loader
from . import spi_boot

CAPACITY = 0x400000
SECTOR = 4096
PAGE = 256
JEDEC = bytes.fromhex('944016')
WRITE_FLAGS = 0x53464752  # 'SFGR'; the three-sector recovery binary uses a different value on purpose
STOCK_SHA256 = '4874af74051372363e8c36ff99e369657648eb353950b41fe2a5a00d35acdb9e'


def digest(data):
    return hashlib.sha256(data).hexdigest()


STOCK_LOAD_ADDRESS = 0x02000000
STOCK_LOAD_SECTORS = 1319


def _stock_prefix(target):
    """The prefix with the three fields a packed SDK image may change put back to stock values."""
    prefix = bytearray(target[:spi_boot.PREFIX_SIZE])
    struct.pack_into('<I', prefix, 0x20, STOCK_LOAD_ADDRESS)
    struct.pack_into('<I', prefix, 0x28, STOCK_LOAD_SECTORS)
    prefix[spi_boot.SILVERGEAR_ENTRY_PATCH:spi_boot.SILVERGEAR_ENTRY_PATCH+8] = spi_boot.SILVERGEAR_ENTRY_STOCK
    return bytes(prefix)


def validate_target(target, expected_sha256):
    """Refuse any image the BootROM would not accept or that is not for this board.

    The prefix (header and vendor bootstrap) must be the reviewed Silvergear one, except for the three fields the
    packer rewrites: header load address and sector count, and the application hook instruction pair.
    """
    if not isinstance(target, bytes) or len(target) != CAPACITY:
        raise ValueError('target must be exactly 4 MiB')
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError('the target SHA-256 must be given explicitly')
    try:
        bytes.fromhex(expected_sha256)
    except ValueError:
        raise ValueError('invalid target SHA-256') from None
    if digest(target) != expected_sha256.lower():
        raise ValueError('target image does not match the expected SHA-256')
    if digest(_stock_prefix(target)) != spi_boot.SILVERGEAR_PREFIX_SHA256:
        raise ValueError('target bootstrap is not the reviewed Silvergear bootstrap (stock or packed SDK image)')
    if target[4:8] != b'BLDR' or target[510:512] != b'\x55\xaa' or struct.unpack_from('<H', target, 2)[0] != 2:
        raise ValueError('target does not carry a BootROM-acceptable BLDR header')
    load, start, count = struct.unpack_from('<III', target, 0x20)
    if start != 17 or not 0 < count <= (CAPACITY - spi_boot.PREFIX_SIZE) // 512 or not 0x02000000 <= load < 0x02400000:
        raise ValueError('target header load record is outside the reviewed range')
    first, second = struct.unpack_from('<II', target, spi_boot.SILVERGEAR_ENTRY_PATCH)
    if first & 0xffff0000 != 0x18400000 or second & 0xffff0000 != 0xa8420000:
        raise ValueError('target application hook is not a movhi/ori r2 pair')
    return target


def changed_sectors(current, target):
    """Sectors to write: everything that differs, ascending, with sector 0 (boot header) always last."""
    if len(current) != CAPACITY or len(target) != CAPACITY:
        raise ValueError('images must be exactly 4 MiB')
    sectors = [a for a in range(SECTOR, CAPACITY, SECTOR) if current[a:a+SECTOR] != target[a:a+SECTOR]]
    if current[:SECTOR] != target[:SECTOR]:
        sectors.append(0)
    return sectors


def write_request(flash, target, allowed, command, address, data=b''):
    if command == 3:
        if address not in allowed or data:
            raise ValueError('erase outside the planned sectors')
    elif command == 2:
        if (address & ~(SECTOR - 1)) not in allowed or address % PAGE or len(data) != PAGE:
            raise ValueError('program outside the planned sectors or not a full page')
        if data != target[address:address+PAGE]:
            raise ValueError('program bytes differ from the target image')
    else:
        raise ValueError('unsupported write command')
    flash.sequence = (flash.sequence + 1) & 0xffffffff
    request = HEADER.pack(MAGIC, flash.sequence, command, address, len(data), PENDING, WRITE_FLAGS) + data
    control = flash.region.control
    flash._write(control, request)
    if flash._read(control, len(request)) != request:
        raise TransferError('request upload mismatch; callback not invoked')
    flash.device.execute(flash._cdb(control, flash.region.entry), data_in_length=HEADER.size)
    reply = HEADER.unpack(flash._read(control, HEADER.size))
    if reply != (MAGIC, flash.sequence, command, address, len(data), 0, WRITE_FLAGS):
        raise TransferError(f'write command failed at {address:#x}: {reply}')


def install(flash, target, expected_sha256, backup, journal, log=print):
    """Make the chip equal ``target``. The original is read and saved first; every sector is verified."""
    validate_target(target, expected_sha256)
    if flash.flash_id() != JEDEC:
        raise UnsafeOperation("this writer requires the camera's 944016 flash")
    protection = flash.command(9, 0, 1)[0]
    if protection & 0xfc:
        raise UnsafeOperation(f'flash protection/status {protection:#04x}; no protection bits will be changed')
    backup, journal = Path(backup), Path(journal)
    if backup.exists() or journal.exists():
        raise FileExistsError('backup/journal must be new')
    original = flash.read(0, CAPACITY)
    before = digest(original)
    target_sha = digest(target)
    if before == target_sha:
        return {'status': 'already_target', 'sha256': before}
    sectors = changed_sectors(original, target)
    _write_new_file(backup, original)
    if backup.read_bytes() != original:
        raise TransferError('fresh backup verification failed; no erase issued')
    state = {'status': 'backed_up', 'before_sha256': before, 'target_sha256': target_sha,
             'backup': str(backup), 'changed_sectors': sectors, 'verified_sectors': [],
             'boot_header_committed': False}

    def save():
        temporary = Path(str(journal) + '.new')
        with temporary.open('w') as output:
            json.dump(state, output, indent=2); output.write('\n'); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, journal)

    save()
    allowed = set(sectors)
    try:
        for address in sectors:
            state.update(status='writing', pending_sector=address); save()
            write_request(flash, target, allowed, 3, address)
            for offset in range(0, SECTOR, PAGE):
                page = target[address+offset:address+offset+PAGE]
                if page != b'\xff' * PAGE:
                    write_request(flash, target, allowed, 2, address+offset, page)
            if flash.read(address, SECTOR) != target[address:address+SECTOR]:
                raise TransferError(f'sector verification failed at {address:#x}')
            state['verified_sectors'].append(address)
            if address == 0:
                state['boot_header_committed'] = True
            save(); log(f'Verified sector {address:#08x} ({len(state["verified_sectors"])}/{len(sectors)})')
        final = digest(flash.read(0, CAPACITY))
        if final != target_sha:
            raise TransferError('full-chip verification failed')
        state.update(status='verified', readback_sha256=final); save()
        return state
    except BaseException as error:
        state.update(status='interrupted', error=str(error)); save()
        raise


def bring_up_flash(flash, seconds, log=print, sleep=time.sleep, clock=time.monotonic):
    """Read-only: probe the chip's JEDEC ID until it answers (the SPI short has been released) or time runs out.

    The loader is already kept alive by the payload (its first call switches the ROM watchdog off), so waiting here is
    safe. Only JEDEC reads are repeated; nothing destructive is ever retried."""
    deadline = clock() + seconds
    announced = False
    while True:
        try:
            return flash._bring_up_spi('sdk')
        except TransferError:
            if clock() >= deadline:
                raise
            if not announced:
                log('Flash not answering yet (SPI short still held?). Release it; probing again every second.')
                announced = True
            sleep(1)


def preview(target, expected_sha256):
    validate_target(target, expected_sha256)
    return {'target_sha256': digest(target), 'bytes': len(target),
            'header_load_address': hex(struct.unpack_from('<I', target, 0x20)[0]),
            'header_load_sectors': struct.unpack_from('<I', target, 0x28)[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path, help='4 MiB image to make the chip equal to')
    parser.add_argument('--expect-sha256', required=True, help='SHA-256 of the target image')
    parser.add_argument('--payload', type=Path, required=True, help='build/flasher/silvergear-full-restore.bin')
    parser.add_argument('--expect-payload-sha256', help='required with --yes')
    parser.add_argument('--output', type=Path, help='prefix for the new backup and journal files (required with --yes)')
    parser.add_argument('--yes', action='store_true', help='authorize erase/program of the planned sectors')
    parser.add_argument('--device')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--wait-flash', type=float, default=0, help='keep probing the flash JEDEC ID (read only) for this many seconds, so the SPI short can be released after the loader is up')
    args = parser.parse_args()
    target = args.target.read_bytes()
    payload = args.payload.read_bytes()
    print(json.dumps(preview(target, args.expect_sha256) | {'payload_sha256': digest(payload)}, indent=2), flush=True)
    if not args.yes:
        print('Offline preview only. No camera commands issued.'); return
    if not args.expect_payload_sha256 or digest(payload) != args.expect_payload_sha256.lower():
        raise ValueError('--expect-payload-sha256 must match the payload')
    if not args.output:
        raise ValueError('--output is required with --yes')
    if not 0 < args.timeout <= 300:
        raise ValueError('invalid loader wait timeout')
    print('Waiting for the BootROM loader. Keep power held through verification.', flush=True)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        paths = [args.device] if args.device else loader_sg_paths()
        if paths:
            if not 0 <= args.wait_flash <= 120:
                raise ValueError('invalid --wait-flash time')
            # One attempt only: never automatically repeat a destructive action.
            stem = args.output.resolve().parent / (args.output.name + '-' + str(time.time_ns()))
            with open_loader(paths[0]) as device:
                # Open the loader at once (it times out within seconds): the payload's first call keeps it alive,
                # then wait for the flash to answer before anything else happens.
                with PayloadFlash(device, args.payload, SRAM, spi_init='none') as flash:
                    print('Payload running; loader kept alive.', flush=True)
                    flash.spi_path = bring_up_flash(flash, args.wait_flash, log=lambda m: print(m, flush=True))
                    result = install(flash, target, args.expect_sha256, stem.with_suffix('.before.bin'),
                                     stem.with_suffix('.journal.json'), log=lambda message: print(message, flush=True))
                    print(json.dumps(result, indent=2), flush=True)
            return
        time.sleep(.02)
    raise ApxError('no loader appeared; no flash writes issued')


if __name__ == '__main__':
    main()
