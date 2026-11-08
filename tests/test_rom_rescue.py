import hashlib
import json
import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from apxtool import cli, flasher, rom_rescue
from apxtool.errors import ApxError, TransferError
from apxtool.sdk_flash import CAPACITY
from apxtool.transport import SCSITransfer

PAYLOAD = bytes(range(256)) * 4


def test_open_loader_waits_for_node_and_permissions():
    device = object()
    with patch.object(rom_rescue, "LinuxSCSITransport", side_effect=[
        FileNotFoundError(), PermissionError(), device,
    ]) as opened, patch.object(rom_rescue.time, "sleep") as sleep:
        assert rom_rescue.open_loader("/dev/sg0") is device
    assert opened.call_count == 3
    assert sleep.call_count == 2


def test_open_loader_node_wait_is_bounded():
    with patch.object(rom_rescue, "LinuxSCSITransport", side_effect=FileNotFoundError()), \
            patch.object(rom_rescue.time, "monotonic", side_effect=[0, 2]):
        with pytest.raises(FileNotFoundError):
            rom_rescue.open_loader("/dev/sg0", grace=1)


class FakeROM:
    """Models the forced BootROM 0xcb command plus the SRAM rescue payload."""

    def __init__(self, *, rom_state_ok=True, sdk_init_ok=True, watchdog_stays_on=False, jedec=flasher.JEDEC):
        self.sram = bytearray(0x4000)
        self.jedec = jedec
        self.flash = bytearray(b"\xff" * flasher.READ_CAPACITIES.get(jedec[2], CAPACITY))
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
            out = self.jedec if ok else b"\xff\xff\xff"
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
    with pytest.raises(TransferError, match="valid JEDEC"):
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


@pytest.mark.parametrize("jedec", [flasher.JEDEC, bytes.fromhex("5e4016"), bytes.fromhex("ef4016"), bytes.fromhex("944016")])
def test_full_dump_metadata_and_independent_verification(tmp_path, jedec):
    rom = FakeROM(jedec=jedec)
    capacity = len(rom.flash)
    rom.flash[-1] = 0x42
    if capacity == 0x400000:
        rom.flash[0x200000:0x200004] = b"HIGH"
    client = flasher.PayloadFlash(rom, PAYLOAD, spi_init="rom")
    output = tmp_path / "stock.bin"
    result = rom_rescue.action_dump(client, 1, output)
    assert output.read_bytes() == bytes(rom.flash) and output.stat().st_size == capacity
    metadata = json.loads((tmp_path / "stock.bin.json").read_text())
    assert metadata == result and metadata["jedec"] == jedec.hex() and metadata["capacity"] == capacity
    assert metadata["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert metadata["payload_sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert not metadata["boot_header"]["boot_header_valid"]  # invalid headers never block a raw backup
    assert not list(tmp_path.glob("*.part"))

    before = rom.runs
    verified = rom_rescue.action_verify(client, 1, output.read_bytes())
    assert verified["verified"] and verified["first_mismatch"] is None
    assert verified["expected_sha256"] == verified["actual_sha256"] == result["sha256"]
    assert rom.runs - before == capacity // 512  # complete second read, never just cached bytes
    rom.flash[-1] ^= 1
    failed = rom_rescue.action_verify(client, 1, output.read_bytes())
    assert not failed["verified"] and failed["first_mismatch"] == capacity - 1
    assert failed["expected_sha256"] != failed["actual_sha256"] and not rom.erases


def test_identification_reports_unknown_geometry_without_reading_flash():
    client = flasher.PayloadFlash(FakeROM(jedec=bytes.fromhex("ab4017")), PAYLOAD)
    result = rom_rescue.action_jedec(client, 1)
    assert result["jedec"] == "ab4017" and result["capacity"] is None and not result["read_supported"]
    assert result["host_read_only"] and "boot_header_valid" not in result
    assert client.spi_path == "rom-state" and client.device.runs == 3  # snapshot, two raw ID probes only
    with pytest.raises(ApxError, match="ab4017"):
        client.read(0, 1)


def test_legacy_payload_rejection_never_triggers_sdk_init_for_a_valid_id():
    rom = FakeROM(jedec=bytes.fromhex("ef4016"))
    original = rom._payload

    def old_payload(base):
        original(base)
        if struct.unpack_from("<I", rom.sram, base + 8)[0] == flasher.CMD_JEDEC:
            assert struct.unpack_from("<I", rom.sram, base + 12)[0] == flasher.JEDEC_ROM_STATE
            struct.pack_into("<I", rom.sram, base + 20, 3)

    rom._payload = old_payload
    with pytest.raises(ApxError, match="rebuild"):
        flasher.PayloadFlash(rom, PAYLOAD, spi_init="auto")
    assert rom.runs == 2


def test_partial_id_on_transfer_timeout_is_not_accepted_as_a_chip():
    rom = FakeROM()
    original = rom._payload

    def timed_out_probe(base):
        original(base)
        if struct.unpack_from("<I", rom.sram, base + 8)[0] == flasher.CMD_JEDEC:
            rom.sram[base + 28:base + 31] = bytes.fromhex("ff0000")
            struct.pack_into("<I", rom.sram, base + 20, 2)

    rom._payload = timed_out_probe
    with pytest.raises(TransferError, match=r"ff0000 \(status 2\)"):
        flasher.PayloadFlash(rom, PAYLOAD, spi_init="rom")


def test_interrupted_dump_leaves_no_output_or_metadata(tmp_path):
    rom = FakeROM(jedec=bytes.fromhex("ef4016"))
    client = flasher.PayloadFlash(rom, PAYLOAD)
    rom.drop_after = rom.runs + 2
    with pytest.raises(TransferError, match="disappeared"):
        rom_rescue.action_dump(client, 1, tmp_path / "stock.bin")
    assert not list(tmp_path.iterdir())


def test_interrupted_verification_cannot_report_success():
    rom = FakeROM()
    client = flasher.PayloadFlash(rom, PAYLOAD)
    rom.drop_after = rom.runs + 2
    with pytest.raises(TransferError, match="disappeared"):
        rom_rescue.action_verify(client, 1, bytes(rom.flash))


def test_metadata_failure_preserves_completed_dump(tmp_path):
    client = flasher.PayloadFlash(FakeROM(), PAYLOAD)
    original_write = rom_rescue._write_new_file

    def fail_metadata(path, data):
        if path.suffix == ".json":
            raise OSError("disk full")
        original_write(path, data)

    with patch.object(rom_rescue, "_write_new_file", side_effect=fail_metadata):
        with pytest.raises(ApxError, match="preserved"):
            rom_rescue.action_dump(client, 1, tmp_path / "stock.bin")
    assert (tmp_path / "stock.bin").read_bytes() == bytes(client.device.flash)
    assert not (tmp_path / "stock.bin.json").exists()


@pytest.mark.parametrize("existing", ["stock.bin", "stock.bin.json", "stock.bin.part", "stock.bin.json.part"])
def test_dump_preflight_refuses_any_existing_artifact_without_usb(tmp_path, existing, capsys):
    payload = tmp_path / "p.bin"
    payload.write_bytes(PAYLOAD)
    path = tmp_path / existing
    path.write_bytes(b"preserve")
    with patch.object(rom_rescue, "run_when_caught") as run:
        assert cli.main(["rom-rescue", "--payload", str(payload), "dump", str(tmp_path / "stock.bin")]) == 2
        run.assert_not_called()
    assert path.read_bytes() == b"preserve" and "refusing to overwrite" in capsys.readouterr().err


def test_verify_cli_success_mismatch_and_wrong_capacity(tmp_path, capsys):
    rom = FakeROM(jedec=bytes.fromhex("ef4016"))
    client = flasher.PayloadFlash(rom, PAYLOAD)
    payload, image = tmp_path / "p.bin", tmp_path / "stock.bin"
    payload.write_bytes(PAYLOAD)
    image.write_bytes(rom.flash)
    base = ["rom-rescue", "--payload", str(payload), "--spi-init", "rom", "verify", str(image)]

    def run(action, **kwargs):
        assert kwargs["spi_init"] == "rom"
        return action(client, 1)

    with patch.object(rom_rescue, "run_when_caught", side_effect=run):
        assert cli.main(base) == 0 and json.loads(capsys.readouterr().out)["verified"]
        rom.flash[0x200001] = 0
        assert cli.main(base) == 2
        assert json.loads(capsys.readouterr().out)["first_mismatch"] == 0x200001
        image.write_bytes(b"\xff" * CAPACITY)
        before = rom.runs
        assert cli.main(base) == 2 and "exactly 4194304 bytes" in capsys.readouterr().err
        assert rom.runs == before  # valid file size for the wrong chip is rejected before reads
    image.write_bytes(b"short")
    with patch.object(rom_rescue, "run_when_caught") as run:
        assert cli.main(base) == 2
        run.assert_not_called()


def test_dump_verify_cli_keeps_both_reads_in_one_session(tmp_path, capsys):
    client = flasher.PayloadFlash(FakeROM(jedec=bytes.fromhex("944016")), PAYLOAD)
    payload, output = tmp_path / "p.bin", tmp_path / "stock.bin"
    payload.write_bytes(PAYLOAD)
    before = client.device.runs
    with patch.object(rom_rescue, "run_when_caught", side_effect=lambda action, **kwargs: action(client, 1)) as run:
        args = ["rom-rescue", "--payload", str(payload), "dump", str(output), "--verify"]
        assert cli.main(args) == 0 and run.call_count == 1
    result = json.loads(capsys.readouterr().out)
    assert result["verification"]["verified"] and result["dump"]["jedec"] == "944016"
    assert client.device.runs - before == 2 * 0x400000 // 512
    assert json.loads((tmp_path / "stock.bin.verify.json").read_text()) == result["verification"]
    assert (tmp_path / "stock.bin.json").is_file()


def test_dump_verify_retries_only_verification_after_disconnect(tmp_path, capsys):
    from contextlib import nullcontext

    payload, output = tmp_path / "p.bin", tmp_path / "stock.bin"
    payload.write_bytes(PAYLOAD)
    first, second = FakeROM(), FakeROM()
    first.drop_after = 2 + CAPACITY // 512 + 2  # snapshot + JEDEC, dump, two verify blocks
    with patch.object(rom_rescue, "loader_sg_paths", return_value=["/dev/sg9"]), \
            patch.object(rom_rescue, "open_loader", side_effect=[nullcontext(first), nullcontext(second)]), \
            patch.object(rom_rescue.time, "sleep"):
        assert cli.main(["rom-rescue", "--payload", str(payload), "dump", str(output), "--verify"]) == 0
    assert second.runs == 2 + CAPACITY // 512  # second session does not repeat the dump
    assert output.read_bytes() == bytes(first.flash)
    assert json.loads((tmp_path / "stock.bin.verify.json").read_text())["verified"]


@pytest.mark.parametrize("existing", ["stock.bin.verify.json", "stock.bin.verify.json.part"])
def test_dump_verify_preflight_checks_report_before_usb(tmp_path, existing):
    payload = tmp_path / "p.bin"
    payload.write_bytes(PAYLOAD)
    (tmp_path / existing).write_bytes(b"preserve")
    with patch.object(rom_rescue, "run_when_caught") as run:
        assert cli.main(["rom-rescue", "--payload", str(payload), "dump", str(tmp_path / "stock.bin"), "--verify"]) == 2
        run.assert_not_called()


def test_restore_read_only_chip_is_refused_before_backup_or_reads(tmp_path):
    rom = FakeROM(jedec=bytes.fromhex("ef4015"))
    client = flasher.PayloadFlash(rom, PAYLOAD)
    before = rom.runs
    with pytest.raises(ApxError, match="read-only"):
        rom_rescue.action_restore(client, 1, boot_image(), tmp_path / "backup.bin", "auto")
    assert rom.runs == before and not list(tmp_path.iterdir()) and not rom.erases


def test_rescue_does_not_retry_deterministic_failures(tmp_path):
    from contextlib import nullcontext

    payload = tmp_path / "p.bin"
    payload.write_bytes(PAYLOAD)
    with patch.object(rom_rescue, "loader_sg_paths", return_value=["/dev/sg9"]), \
            patch.object(rom_rescue, "open_loader", return_value=nullcontext(FakeROM())) as opened:
        with pytest.raises(ValueError, match="exactly"):
            rom_rescue.run_when_caught(lambda c, n: rom_rescue.action_verify(c, n, b"short"),
                                      payload=payload, timeout=1, log=lambda *_: None)
        assert opened.call_count == 1


def test_rescue_retries_a_vanished_sg_node(tmp_path):
    from contextlib import nullcontext

    payload = tmp_path / "p.bin"
    payload.write_bytes(PAYLOAD)
    vanished = FileNotFoundError(2, "No such file or directory", "/dev/sg9")
    with patch.object(rom_rescue, "loader_sg_paths", return_value=["/dev/sg9"]), \
            patch.object(rom_rescue, "open_loader", side_effect=[vanished, nullcontext(FakeROM())]) as opened, \
            patch.object(rom_rescue.time, "sleep"):
        result = rom_rescue.run_when_caught(rom_rescue.action_jedec, payload=payload, timeout=1, log=lambda *_: None)
    assert result["jedec"] == "5e4015" and opened.call_count == 2
