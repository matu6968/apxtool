import ctypes
import hashlib
import json
import struct
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apxtool import silvergear_install as install, silvergear_recovery as recovery, spi_boot
from apxtool.errors import ApxError, TransferError, UnsafeOperation
from apxtool.flasher import PayloadFlash, SRAM
from test_flasher import LoaderModel, CODE, SDK

STOCK_PATH = Path(__file__).resolve().parents[2] / 'stock-backups/silvergear-3222882-20261102/stock.bin'
pytestmark = pytest.mark.skipif(not STOCK_PATH.exists(), reason='Silvergear stock backup not present')


def build(tmp_path, name, defines):
    library = tmp_path / f'{name}.so'
    subprocess.run(['cc', '-std=c11', '-O1', '-fPIC', '-shared', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=undefined', '-fno-sanitize-recover=undefined', '-DFLASHER_HOST',
                    *defines, str(SDK / 'boot/flasher.c'), str(SDK / 'tests/flasher_model.c'), '-o', str(library)],
                   check=True)
    lib = ctypes.CDLL(str(library))
    lib.model_nor.restype = ctypes.POINTER(ctypes.c_uint8 * 0x400000)
    lib.model_set_sram_window.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.model_call.argtypes = [ctypes.c_void_p]
    return lib


@pytest.fixture(scope='module')
def stock():
    data = STOCK_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == install.STOCK_SHA256
    return data


@pytest.fixture
def packed(stock, tmp_path, monkeypatch):
    manifest = tmp_path / 'demo.manifest.json'
    manifest.write_text('{}')
    segments = [(0x02180000, bytes((i * 7) & 255 for i in range(0x30000))), (0x02220000, b'TABLE' * 4000)]
    monkeypatch.setattr(spi_boot, 'load_manifest',
                        lambda p: ({'spi_entry': 0x02180010, 'build_id': 'test'}, segments))
    image, _ = spi_boot.build_image(stock, manifest, 'silvergear')
    return image


@pytest.fixture
def setup(tmp_path, stock):
    lib = build(tmp_path, 'full', ['-DSILVERGEAR_BOOT_RESTORE', '-DSILVERGEAR_FULL_RESTORE'])
    loader = LoaderModel(lib, ddr_alive=False, jedec=bytes.fromhex('944016'))
    flash = PayloadFlash(loader, CODE, SRAM, spi_init='sdk')
    return flash, loader, tmp_path


def load_chip(loader, data):
    ctypes.memmove(ctypes.addressof(loader.nor), data, len(data))


def test_installs_packed_image_over_stock_header_last_and_restores_stock(setup, stock, packed):
    flash, loader, tmp = setup
    load_chip(loader, stock)
    sectors = install.changed_sectors(stock, packed)
    assert sectors[-1] == 0 and sectors[:-1] == sorted(sectors[:-1]) and len(sectors) > 10
    sha = hashlib.sha256(packed).hexdigest()
    result = install.install(flash, packed, sha, tmp / 'before.bin', tmp / 'journal.json')
    assert result['status'] == 'verified' and result['verified_sectors'] == sectors
    assert result['boot_header_committed'] and bytes(loader.nor) == packed
    assert (tmp / 'before.bin').read_bytes() == stock
    assert json.loads((tmp / 'journal.json').read_text())['readback_sha256'] == sha
    assert loader.lib.model_command_count(0x20) == len(sectors)
    assert loader.lib.model_command_count(1) == 0  # no status-register / protection writes
    # The same writer restores the verified stock image from the installed one.
    again = install.install(flash, stock, install.STOCK_SHA256, tmp / 'before2.bin', tmp / 'journal2.json')
    assert again['status'] == 'verified' and bytes(loader.nor) == stock
    assert (tmp / 'before2.bin').read_bytes() == packed
    # Already equal: nothing is written, no backup is made.
    count = loader.lib.model_command_count(0x20)
    done = install.install(flash, stock, install.STOCK_SHA256, tmp / 'b3.bin', tmp / 'j3.json')
    assert done['status'] == 'already_target' and loader.lib.model_command_count(0x20) == count
    assert not (tmp / 'b3.bin').exists()


def test_restores_from_a_blank_or_corrupt_chip(setup, stock):
    flash, loader, tmp = setup
    chip = bytearray(stock)
    chip[:4096] = bytes(4096)                 # boot header sector gone, as after an interrupted write
    chip[0x123000:0x124000] = b'\x00' * 4096   # damage elsewhere
    load_chip(loader, bytes(chip))
    result = install.install(flash, stock, install.STOCK_SHA256, tmp / 'before.bin', tmp / 'journal.json')
    assert result['status'] == 'verified' and bytes(loader.nor) == stock
    assert result['changed_sectors'] == [0x123000, 0]


def test_target_refusals_happen_before_any_camera_command(setup, stock, packed):
    flash, loader, tmp = setup
    load_chip(loader, stock)
    sha = hashlib.sha256(packed).hexdigest()
    other = bytearray(packed); other[0x2000] ^= 1
    bad_header = bytearray(packed); bad_header[510] = 0
    hook = bytearray(packed); hook[0x540:0x544] = bytes(4)
    wild = bytearray(packed); struct.pack_into('<I', wild, 0x20, 0x10000000)
    cases = [(b'short', sha, '4 MiB'), (packed, '0' * 64, 'does not match'), (packed, 'xyz', 'SHA-256'),
             (bytes(other), hashlib.sha256(other).hexdigest(), 'reviewed Silvergear bootstrap'),
             (bytes(bad_header), hashlib.sha256(bad_header).hexdigest(), 'BLDR header|bootstrap'),
             (bytes(hook), hashlib.sha256(hook).hexdigest(), 'hook|bootstrap'),
             (bytes(wild), hashlib.sha256(wild).hexdigest(), 'outside the reviewed range')]
    with patch.object(loader, 'execute') as execute:
        for target, expected, message in cases:
            with pytest.raises(ValueError, match=message):
                install.install(flash, target, expected, tmp / 'x.bin', tmp / 'x.json')
        execute.assert_not_called()
    assert not (tmp / 'x.bin').exists()
    # An Action camera image (2 MiB) can never be written.
    with pytest.raises(ValueError):
        install.validate_target(bytes(0x200000), '0' * 64)


def test_wrong_flash_or_protection_refuse_before_erase(tmp_path, stock):
    lib = build(tmp_path, 'full-w', ['-DSILVERGEAR_BOOT_RESTORE', '-DSILVERGEAR_FULL_RESTORE'])
    wrong = LoaderModel(lib, ddr_alive=False, jedec=bytes.fromhex('5e4016'))
    with pytest.raises(UnsafeOperation, match='944016'):
        install.install(PayloadFlash(wrong, CODE, SRAM, spi_init='sdk'), stock, install.STOCK_SHA256,
                        tmp_path / 'a.bin', tmp_path / 'a.json')
    loader = LoaderModel(lib, ddr_alive=False, jedec=bytes.fromhex('944016'))
    chip = bytearray(stock); chip[0x2000] ^= 1; load_chip(loader, bytes(chip))
    loader.lib.model_set_flash_status(0x0c)
    with pytest.raises(UnsafeOperation, match='protection'):
        install.install(PayloadFlash(loader, CODE, SRAM, spi_init='sdk'), stock, install.STOCK_SHA256,
                        tmp_path / 'b.bin', tmp_path / 'b.json')
    assert loader.lib.model_command_count(6) == 0 and not (tmp_path / 'b.bin').exists()


def test_existing_backup_is_never_overwritten(setup, stock):
    flash, loader, tmp = setup
    chip = bytearray(stock); chip[0x2000] ^= 1; load_chip(loader, bytes(chip))
    (tmp / 'before.bin').write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        install.install(flash, stock, install.STOCK_SHA256, tmp / 'before.bin', tmp / 'j.json')
    assert (tmp / 'before.bin').read_bytes() == b'keep' and loader.lib.model_command_count(6) == 0


def test_lost_erase_ack_stops_without_committing_header(setup, stock, packed):
    flash, loader, tmp = setup
    load_chip(loader, stock)
    execute = loader.execute
    erases = []

    def fail(cdb, **kwargs):
        command = int.from_bytes(bytes(loader.sram)[0x1908:0x190c], 'little')
        result = execute(cdb, **kwargs)
        if command == 3 and int.from_bytes(cdb[9:13], 'little') != 0xffffffff:
            erases.append(1)
            if len(erases) == 3:
                raise ApxError('lost erase acknowledgement')
        return result

    sha = hashlib.sha256(packed).hexdigest()
    with patch.object(loader, 'execute', side_effect=fail):
        with pytest.raises(ApxError, match='lost erase'):
            install.install(flash, packed, sha, tmp / 'before.bin', tmp / 'journal.json')
    journal = json.loads((tmp / 'journal.json').read_text())
    assert journal['status'] == 'interrupted' and not journal['boot_header_committed']
    assert bytes(loader.nor)[:4096] == stock[:4096]  # old header still present
    assert (tmp / 'before.bin').read_bytes() == stock


def test_write_request_rejects_unplanned_addresses_and_bytes_before_transport(setup, stock, packed):
    flash, loader, tmp = setup
    allowed = set(install.changed_sectors(stock, packed))
    first = min(a for a in allowed if a)
    unplanned = next(a for a in range(0x300000, 0x400000, 4096) if a not in allowed)
    with patch.object(loader, 'execute') as execute:
        for command, address, data in [(3, unplanned, b''), (3, first + 4, b''), (2, unplanned, bytes(256)),
                                       (2, first, b'bad'), (2, first, bytes(256)),
                                       (2, first + 1, packed[first:first + 256]),
                                       (7, first, b''), (1, 0, b'')]:
            with pytest.raises(ValueError):
                install.write_request(flash, packed, allowed, command, address, data)
        execute.assert_not_called()


def _send(loader, flash, flags, command, sector):
    from apxtool.flasher import HEADER, MAGIC, PENDING
    request = HEADER.pack(MAGIC, 1, command, sector, 0, PENDING, flags)
    flash._write(flash.region.control, request)
    loader.execute(flash._cdb(flash.region.control, flash.region.entry), data_in_length=len(request))
    return struct.unpack_from('<I', flash._read(flash.region.control, 28), 20)[0]


def test_the_two_restore_payloads_refuse_each_others_protocol(tmp_path, stock):
    """The three-sector recovery binary and the full-chip binary use different request flags on purpose."""
    chip = bytes(stock)
    for name, defines, own, other in [('incident', ['-DSILVERGEAR_BOOT_RESTORE'], recovery.WRITE_FLAGS, install.WRITE_FLAGS),
                                      ('full', ['-DSILVERGEAR_BOOT_RESTORE', '-DSILVERGEAR_FULL_RESTORE'],
                                       install.WRITE_FLAGS, recovery.WRITE_FLAGS)]:
        lib = build(tmp_path, name, defines)
        loader = LoaderModel(lib, ddr_alive=False, jedec=bytes.fromhex('944016'))
        load_chip(loader, chip)
        flash = PayloadFlash(loader, CODE, SRAM, spi_init='sdk')
        # The other binary's flags are refused: status 3 and no erase reaches the chip.
        assert _send(loader, flash, other, 3, 0x3ff000) == 3
        assert lib.model_command_count(0x20) == 0 and bytes(loader.nor) == chip
        # Positive control: the binary's own flags are accepted (so the refusal above is the flags, not the harness).
        assert _send(loader, flash, own, 3, 0x3ff000) == 0
        assert lib.model_command_count(0x20) == 1
        assert bytes(loader.nor)[0x3ff000:0x400000] == b'\xff' * 4096
    # Only the full binary reaches an arbitrary sector; the incident binary still refuses it even with its own flags.
    lib = build(tmp_path, 'incident2', ['-DSILVERGEAR_BOOT_RESTORE'])
    loader = LoaderModel(lib, ddr_alive=False, jedec=bytes.fromhex('944016'))
    load_chip(loader, chip)
    flash = PayloadFlash(loader, CODE, SRAM, spi_init='sdk')
    assert _send(loader, flash, recovery.WRITE_FLAGS, 3, 0x123000) == 3 and lib.model_command_count(0x20) == 0


def test_cli_preview_is_offline(tmp_path, stock, packed, capsys, monkeypatch):
    target = tmp_path / 'target.bin'; target.write_bytes(packed)
    payload = tmp_path / 'p.bin'; payload.write_bytes(b'payload')
    monkeypatch.setattr('sys.argv', ['x', str(target), '--expect-sha256', hashlib.sha256(packed).hexdigest(),
                                     '--payload', str(payload)])
    with patch('apxtool.silvergear_install.open_loader') as opened, patch('apxtool.silvergear_install.loader_sg_paths') as paths:
        install.main()
        opened.assert_not_called(); paths.assert_not_called()
    out = capsys.readouterr().out
    assert 'Offline preview only' in out and '"header_load_address": "0x2180000"' in out


def test_bring_up_flash_retries_only_the_read_only_probe():
    class Flash:
        def __init__(self, failures):self.failures = failures;self.calls = 0
        def _bring_up_spi(self, mode):
            assert mode == 'sdk';self.calls += 1
            if self.calls <= self.failures:raise TransferError('flasher could not read a valid JEDEC ID')
            return 'sdk-init'
    now = [0.0];sleeps = []
    clock = lambda: now[0]
    def sleep(seconds):sleeps.append(seconds);now[0] += seconds
    flash = Flash(3)
    assert install.bring_up_flash(flash, 30, log=lambda m: None, sleep=sleep, clock=clock) == 'sdk-init'
    assert flash.calls == 4 and sleeps == [1, 1, 1]
    # Out of time: the probe error propagates and no destructive request was ever made.
    now[0] = 0.0;sleeps.clear();flash = Flash(100)
    with pytest.raises(TransferError):install.bring_up_flash(flash, 3, log=lambda m: None, sleep=sleep, clock=clock)
    assert flash.calls == 4
    # Zero wait is a single attempt.
    flash = Flash(1)
    with pytest.raises(TransferError):install.bring_up_flash(flash, 0, log=lambda m: None, sleep=sleep, clock=clock)
    assert flash.calls == 1


def test_loader_is_opened_before_the_flash_is_probed(setup):
    """With spi_init='none' the payload runs (watchdog off) without any flash access, then the probe succeeds later."""
    _, used, _ = setup
    fresh = LoaderModel(used.lib, ddr_alive=False, jedec=bytes.fromhex('944016'))  # resets the model's counters
    from apxtool.flasher import PayloadFlash as PF
    idle = PF(fresh, CODE, SRAM, spi_init='none')
    assert fresh.lib.model_command_count(0x9f) == 0 and idle.jedec_id is None
    assert idle._bring_up_spi('sdk') == 'sdk-init' and idle.jedec_id == install.JEDEC
    assert fresh.lib.model_command_count(0x9f) >= 1
