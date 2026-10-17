import hashlib
import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from apxtool import cli, flasher, rom_rescue
from apxtool.errors import ApxError, TransferError
from apxtool.sdk_flash import CAPACITY
from apxtool.transport import SCSITransfer

PAYLOAD = bytes(range(256)) * 4


class FakeROM:
    """Models the forced BootROM 0xcb command plus the SRAM rescue payload."""

    def __init__(self, *, rom_state_ok=True, sdk_init_ok=True, watchdog_stays_on=False):
        self.sram = bytearray(0x4000)
        self.flash = bytearray(b"\xff" * CAPACITY)
        self.rom_state_ok, self.sdk_init_ok = rom_state_ok, sdk_init_ok
        self.watchdog = 0x1D
        self.watchdog_stays_on = watchdog_stays_on
        self.erases, self.calls_before_watchdog_off = [], 0
        self.drop_after = None  # raise after this many callback runs
        self.runs = 0
        self.pm = {0x40 + 2 * i: 0 for i in range(14)}
        self.pm.update({0x40: 0x12, 0x44: 0x00, 0x54: 0x21, 0x58: 0x04, 0x5A: 0x00})  # the state a bad write left behind

    def execute(self, cdb, *, data_out=None, data_in_length=0):
        if cdb[0] == 0x12:
            return SCSITransfer(b"\x00" * 8 + b"BuildWin".ljust(8) + b"Video050Loader".ljust(16) + b"1.00", 0)
        assert cdb[0] == 0xCB and cdb[1:5] == (flasher.RAM_TRANSFER_FUNCTION).to_bytes(4, "little")
        address = int.from_bytes(cdb[5:9], "little")
        callback = int.from_bytes(cdb[9:13], "little")
        if data_out is not None:
            self.sram[address:address + len(data_out)] = data_out
            return SCSITransfer(b"", 0)
        if callback == flasher.SRAM.entry:
            self.runs += 1
            if self.drop_after is not None and self.runs > self.drop_after:
                raise TransferError("device disappeared")
            self._payload(address)
        return SCSITransfer(bytes(self.sram[address:address + data_in_length]), 0)

    def _payload(self, base):
        if not self.watchdog_stays_on:
            self.watchdog = 0
        magic, seq, cmd, a, n, _, flags = struct.unpack_from("<7I", self.sram, base)
        assert magic == flasher.MAGIC and n <= 512
        data = bytes(self.sram[base + 28:base + 28 + n])
        out, status = b"", 0
        if cmd == flasher.CMD_SNAPSHOT:
            assert n == 32
            out = struct.pack("<8I", self.watchdog, *range(7))
        elif cmd == flasher.CMD_JEDEC:
            ok = self.rom_state_ok if a & 1 else self.sdk_init_ok
            out = flasher.JEDEC if ok else b"\xff\xff\xff"
            status = 0 if ok else 3
        elif cmd in (flasher.CMD_PM_READ, flasher.CMD_PM_ENABLE):
            assert n == 14
            if cmd == flasher.CMD_PM_ENABLE:
                self.pm_enabled = getattr(self, "pm_enabled", 0) + 1
                self.pm.update({0x40: 0x16, 0x42: 0xE0, 0x44: 0x00, 0x58: self.pm[0x58] | 0x08, 0x5A: 0x08})
            out = bytes(self.pm[0x40 + 2 * i] for i in range(14))
        elif cmd == flasher.CMD_READ:
            out = bytes(self.flash[a:a + n])
        elif cmd == flasher.CMD_PROGRAM:
            assert 0 < n <= 256 - a % 256
            for i, b in enumerate(data):
                self.flash[a + i] &= b
        elif cmd == flasher.CMD_ERASE:
            assert a % 4096 == 0
            self.erases.append(a)
            self.flash[a:a + 4096] = b"\xff" * 4096
        else:
            raise AssertionError(cmd)
        out = out.ljust(3 if cmd == flasher.CMD_JEDEC else n, b"\0")
        struct.pack_into("<7I", self.sram, base, magic, seq, cmd, a, n, status, flags)
        self.sram[base + 28:base + 28 + len(out)] = out


def boot_image(fill=b"\x00", marker=0):
    image = bytearray(fill * CAPACITY)
    image[0:8] = b"\x00\x00\x02\x00BLDR"
    image[510:512] = b"\x55\xaa"
    image[0x20:0x24] = marker.to_bytes(4, "little")  # header sector differs between images
    image[0x2200:0x2204] = marker.to_bytes(4, "little")
    return bytes(image)


def test_bring_up_disables_watchdog_and_prefers_rom_state():
    rom = FakeROM()
    client = flasher.PayloadFlash(rom, PAYLOAD)
    assert bytes(rom.sram[flasher.SRAM.entry:flasher.SRAM.entry + len(PAYLOAD)]) == PAYLOAD
    assert client.watchdog_off and client.spi_path == "rom-state" and not client.notes
    assert client.snapshot[flasher.WATCHDOG_SPR] == 0


def test_falls_back_to_sdk_init_and_reports_stuck_watchdog():
    client = flasher.PayloadFlash(FakeROM(rom_state_ok=False, watchdog_stays_on=True), PAYLOAD)
    assert client.spi_path == "sdk-init" and not client.watchdog_off and "watchdog" in client.notes[0]


def test_jedec_failure_names_the_short():
    with pytest.raises(TransferError, match="SPI short released"):
        flasher.PayloadFlash(FakeROM(rom_state_ok=False, sdk_init_ok=False), PAYLOAD)
    with pytest.raises(TransferError, match="expected JEDEC"):
        flasher.PayloadFlash(FakeROM(rom_state_ok=False), PAYLOAD, spi_init="rom")


def test_rejects_oversized_or_wrong_target_payload():
    with pytest.raises(ValueError):
        flasher.PayloadFlash(FakeROM(), b"x" * (flasher.SRAM.code_limit + 1))
    rom = FakeROM()
    rom.execute = lambda cdb, **_: SCSITransfer(b"\0" * 8 + b"Buildwin" + b"Media-Player".ljust(16) + b"1.00", 0)
    with pytest.raises(ValueError, match="BootROM loader"):
        flasher.PayloadFlash(rom, PAYLOAD)


def test_restore_backs_up_commits_header_last_and_verifies(tmp_path):
    rom = FakeROM()
    bad, good = boot_image(marker=1), boot_image(marker=2)
    rom.flash[:] = bad
    client = flasher.PayloadFlash(rom, PAYLOAD)
    state = rom_rescue.action_restore(client, 1, good, tmp_path / "pre.bin", "auto", log=lambda *_: None)
    assert state["status"] == "verified" and bytes(rom.flash) == good
    assert (tmp_path / "pre.bin").read_bytes() == bad
    assert rom.erases == [0x2000, 0] and state["boot_sector_committed"]


def test_restore_honours_expected_hash_and_unchanged(tmp_path):
    rom = FakeROM()
    rom.flash[:] = boot_image(marker=1)
    client = flasher.PayloadFlash(rom, PAYLOAD)
    with pytest.raises(ApxError, match="nothing was erased"):
        rom_rescue.action_restore(client, 1, boot_image(marker=2), tmp_path / "a", "0" * 64, log=lambda *_: None)
    assert not rom.erases and not (tmp_path / "a").exists()
    same = rom_rescue.action_restore(client, 1, boot_image(marker=1), tmp_path / "b", "auto", log=lambda *_: None)
    assert same["status"] == "unchanged" and not rom.erases


def test_retry_uses_a_fresh_backup_name(tmp_path):
    rom = FakeROM()
    rom.flash[:] = boot_image(marker=1)
    client = flasher.PayloadFlash(rom, PAYLOAD)
    rom_rescue.action_restore(client, 2, boot_image(marker=3), tmp_path / "pre.bin", "auto", log=lambda *_: None)
    assert (tmp_path / "pre.bin.retry2").exists() and not (tmp_path / "pre.bin").exists()


def test_boot_image_checks():
    rom_rescue.check_boot_image(boot_image())
    for bad in (b"short", bytes(CAPACITY), boot_image()[:510] + b"\0\0" + boot_image()[512:]):
        with pytest.raises(ValueError):
            rom_rescue.check_boot_image(bad)


def test_catch_loop_retries_next_rom_cycle(tmp_path):
    attempts = []
    devices = []

    class Handle:
        def __init__(self, rom):
            self.rom = rom

        def __enter__(self):
            return self.rom

        def __exit__(self, *_):
            return False

    def opener(path):
        rom = FakeROM()
        rom.flash[:] = boot_image(marker=1)
        rom.drop_after = 3 if not devices else None  # first cycle dies mid-restore
        devices.append(rom)
        return Handle(rom)

    payload = tmp_path / "payload.bin"
    payload.write_bytes(PAYLOAD)
    target = boot_image(marker=9)
    logs = []
    with patch.object(rom_rescue, "loader_sg_paths", return_value=["/dev/sg9"]), \
            patch.object(rom_rescue, "open_loader", opener), patch.object(rom_rescue.time, "sleep"):
        state = rom_rescue.run_when_caught(
            lambda c, n: attempts.append(n) or rom_rescue.action_restore(
                c, n, target, tmp_path / "pre.bin", "auto", log=lambda *_: None),
            payload=payload, timeout=30, log=logs.append)
    assert attempts == [1, 2] and state["status"] == "verified"
    assert bytes(devices[1].flash) == target and any("attempt 1 failed" in line for line in logs)


def test_cli_guards(tmp_path, capsys):
    payload = tmp_path / "p.bin"
    payload.write_bytes(PAYLOAD)
    image = tmp_path / "image.bin"
    image.write_bytes(boot_image())
    backup = tmp_path / "pre.bin"
    base = ["rom-rescue", "--payload", str(payload), "--timeout", "1"]
    assert cli.main(base + ["restore", str(image), "--backup", str(backup)]) == 2  # no --yes
    assert "--yes" in capsys.readouterr().err
    backup.write_bytes(b"x")
    assert cli.main(base + ["--yes", "restore", str(image), "--backup", str(backup)]) == 2
    image.write_bytes(b"not an image")
    assert cli.main(base + ["--yes", "restore", str(image), "--backup", str(tmp_path / "new.bin")]) == 2
    assert cli.main(["rom-rescue", "--payload", str(tmp_path / "missing"), "jedec"]) == 2
    out = tmp_path / "dump.bin"
    out.write_bytes(b"x")
    assert cli.main(base + ["dump", str(out)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_describe_image_names_known_hashes():
    assert "unrecognised" in rom_rescue.describe_image(b"abc")
    assert hashlib.sha256(b"").hexdigest() not in rom_rescue.KNOWN_IMAGES


def test_restore_prints_flight_recorder_before_overwriting(tmp_path):
    rom = FakeROM()
    rom.flash[:] = boot_image(marker=1)
    sector = bytearray(b"\xff" * 4096)
    sector[0:8] = b"APXT\x01\x00\x00\x00"
    sector[0x800 + 4 * 6:0x800 + 4 * 6 + 4] = bytes([6, 0, 0, 0])
    rom.flash[0x1FF000:0x200000] = sector
    client = flasher.PayloadFlash(rom, PAYLOAD)
    logs = []
    rom_rescue.action_restore(client, 1, boot_image(marker=2), tmp_path / "pre.bin", "auto", log=logs.append)
    assert any("flight recorder found" in line and "clock profile applied" in line for line in logs)


def test_pm_read_and_apply_need_no_flash_bus(tmp_path):
    rom = FakeROM(rom_state_ok=False, sdk_init_ok=False)  # SPI unusable: PM work must not care
    client = flasher.PayloadFlash(rom, PAYLOAD, spi_init="none")
    result = rom_rescue.action_pm(client, 1, False)
    assert result["after"] is None and not result["keep_alive"] and result["before"][0x5A] == 0 and not getattr(rom, "pm_enabled", 0)
    result = rom_rescue.action_pm(client, 1, True)
    assert result["keep_alive"] and result["after"][0x40] == 0x16 and result["after"][0x44] == 0 and rom.pm_enabled == 1
    text = rom_rescue.format_pm(result)
    assert "0x5a: 0x00 -> 0x08" in text and "keep-alive register 0x5a = 0x08: yes" in text


def test_pm_apply_requires_confirmation(tmp_path, capsys):
    payload = tmp_path / "p.bin"
    payload.write_bytes(PAYLOAD)
    assert cli.main(["rom-rescue", "--payload", str(payload), "--timeout", "1", "pm", "--apply"]) == 2
    assert "--yes" in capsys.readouterr().err
