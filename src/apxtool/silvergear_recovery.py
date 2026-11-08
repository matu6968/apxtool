"""Incident-specific, three-sector stock recovery. Default is an offline preview.

This module does not enter BootROM, run DDR initialization, launch the SDK,
change PM registers, clear write protection, or provide a general flash writer.
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

STOCK_SHA256 = '4874af74051372363e8c36ff99e369657648eb353950b41fe2a5a00d35acdb9e'
CURRENT_SHA256 = '95f77c123650198ecab05a7afb1bf048950293cb6608752393949dc0c872bdcf'
PAYLOAD_SHA256 = '40411b4154c32db93795be50976d74dc0efc26699126c497ab45bb523925c8a1'
CAPACITY = 0x400000
WRITE_ORDER = (0x3ff000, 0x1000, 0)
WRITE_FLAGS = 0x53470000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_plan(path):
    path = Path(path).resolve()
    document = json.loads(path.read_text())
    if document.get('schema') != 'silvergear-stock-recovery-1':
        raise ValueError('unrecognized recovery plan')
    files = {}
    for key, expected in [('stock', STOCK_SHA256), ('current', CURRENT_SHA256), ('payload', PAYLOAD_SHA256)]:
        filename = (path.parent / document[key]['file']).resolve()
        data = filename.read_bytes()
        if not expected or digest(data) != expected or document[key]['sha256'] != expected:
            raise ValueError(f'{key} recovery hash mismatch')
        files[key] = (filename, data)
    stock, current = files['stock'][1], files['current'][1]
    if len(stock) != CAPACITY or len(current) != CAPACITY:
        raise ValueError('recovery requires full 4 MiB images')
    if not files['payload'][1] or len(files['payload'][1]) > SRAM.code_limit:
        raise ValueError('recovery payload does not fit its SRAM reservation')
    changed = tuple(a for a in range(0, CAPACITY, 4096) if stock[a:a+4096] != current[a:a+4096])
    if changed != tuple(sorted(WRITE_ORDER)) or document.get('write_order') != list(WRITE_ORDER):
        raise ValueError('recovery is restricted to the three reviewed sectors')
    for record, address in zip(document.get('sectors', []), WRITE_ORDER):
        if record != {'address': address, 'bytes': 4096,
                      'before_sha256': digest(current[address:address+4096]),
                      'after_sha256': digest(stock[address:address+4096])}:
            raise ValueError('sector evidence mismatch')
    if len(document.get('sectors', [])) != 3:
        raise ValueError('missing sector evidence')
    return document, files


def write_request(flash, stock, command, address, data=b''):
    if command == 3:
        if address not in WRITE_ORDER or data:
            raise ValueError('unreviewed erase request')
    elif command == 2:
        if (address & ~4095) not in WRITE_ORDER or address % 256 or len(data) != 256:
            raise ValueError('unreviewed program range')
        if data != stock[address:address+256]:
            raise ValueError('program bytes differ from the verified stock backup')
    else:
        raise ValueError('unsupported recovery write command')
    flash.sequence = (flash.sequence + 1) & 0xffffffff
    request = HEADER.pack(MAGIC, flash.sequence, command, address, len(data), PENDING, WRITE_FLAGS) + data
    control = flash.region.control
    flash._write(control, request)
    if flash._read(control, len(request)) != request:
        raise TransferError('recovery request upload mismatch; callback not invoked')
    flash.device.execute(flash._cdb(control, flash.region.entry), data_in_length=HEADER.size)
    reply = HEADER.unpack(flash._read(control, HEADER.size))
    if reply != (MAGIC, flash.sequence, command, address, len(data), 0, WRITE_FLAGS):
        raise TransferError(f'recovery command failed at {address:#x}: {reply}')


def restore(flash, stock, backup, journal, log=print):
    if len(stock) != CAPACITY or digest(stock) != STOCK_SHA256:
        raise ValueError('stock image mismatch')
    if flash.flash_id() != bytes.fromhex('944016'):
        raise UnsafeOperation('recovery requires this camera\'s 944016 flash')
    protection = flash.command(9, 0, 1)[0]
    if protection & 0xfc:
        raise UnsafeOperation(f'flash protection/status {protection:#04x}; no protection bits will be changed')
    original = flash.read(0, CAPACITY)
    actual = digest(original)
    if actual == STOCK_SHA256:
        return {'status': 'already_stock', 'sha256': actual}
    if actual != CURRENT_SHA256:
        raise UnsafeOperation('current flash differs from the reviewed readback; no erase issued')
    _write_new_file(Path(backup), original)
    if Path(backup).read_bytes() != original:
        raise TransferError('fresh backup verification failed; no erase issued')
    state = {'status': 'backed_up', 'before_sha256': actual, 'target_sha256': STOCK_SHA256,
             'write_order': list(WRITE_ORDER), 'verified_sectors': [], 'boot_header_committed': False}

    def save():
        temporary = Path(str(journal) + '.new')
        with temporary.open('w') as output:
            json.dump(state, output, indent=2); output.write('\n'); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, journal)

    save()
    try:
        for address in WRITE_ORDER:
            state.update(status='writing', pending_sector=address); save()
            write_request(flash, stock, 3, address)
            for offset in range(0, 4096, 256):
                page = stock[address+offset:address+offset+256]
                if page != b'\xff' * 256:
                    write_request(flash, stock, 2, address+offset, page)
            if flash.read(address, 4096) != stock[address:address+4096]:
                raise TransferError(f'sector verification failed at {address:#x}')
            state['verified_sectors'].append(address)
            if address == 0:
                state['boot_header_committed'] = True
            save(); log(f'Verified sector {address:#08x}')
        final = digest(flash.read(0, CAPACITY))
        if final != STOCK_SHA256:
            raise TransferError('full-chip verification failed')
        state.update(status='verified', readback_sha256=final); save()
        return state
    except BaseException as error:
        state.update(status='interrupted', error=str(error)); save()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('--yes', action='store_true', help='authorize erase/program of the three reviewed sectors')
    parser.add_argument('--device')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    plan, files = load_plan(args.plan)
    print(json.dumps(plan, indent=2), flush=True)
    if not args.yes:
        print('Offline preview only. No camera commands issued.'); return
    if not 0 < args.timeout <= 300:
        raise ValueError('invalid loader wait timeout')
    print('Waiting for normal BootROM connection. Keep power held through verification; no SPI short.', flush=True)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        paths = [args.device] if args.device else loader_sg_paths()
        if paths:
            # One attempt only: never automatically repeat a destructive action.
            output = args.plan.resolve().parent / ('restore-' + str(time.time_ns()))
            with open_loader(paths[0]) as device:
                with PayloadFlash(device, files['payload'][0], SRAM, spi_init='sdk') as flash:
                    result = restore(flash, files['stock'][1], output.with_suffix('.before.bin'),
                                     output.with_suffix('.journal.json'), log=lambda message: print(message, flush=True))
                    print(json.dumps(result, indent=2), flush=True)
            return
        time.sleep(.02)
    raise ApxError('no loader appeared; no flash writes issued')


if __name__ == '__main__':
    main()
