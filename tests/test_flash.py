from pathlib import Path
import tempfile
import unittest

from apxtool.errors import ApxError, ProtocolUnavailable, UnsafeOperation
from apxtool.flash import erase_sector, require_flash_id, verify_file, write_file
from apxtool.protocol import FlashGeometry


class FakeFlash:
    def __init__(self, size=0x20000, sector_size=0x1000, page_size=0x100):
        self.geometry = FlashGeometry(size, page_size, sector_size, 0x10000)
        self.bytes = bytearray([0xFF] * size)
        self.erases: list[int] = []
        self.programs: list[tuple[int, bytes]] = []

    def hello(self):
        return self.geometry

    def flash_id(self):
        return b"\xef\x40\x15"

    def read(self, address, length):
        self.geometry.check_range(address, length)
        return bytes(self.bytes[address : address + length])

    def erase_sector(self, address):
        self.erases.append(address)
        self.bytes[address : address + self.geometry.sector_size] = b"\xff" * self.geometry.sector_size

    def program_page(self, address, data):
        self.programs.append((address, data))
        for i, value in enumerate(data):
            self.bytes[address + i] &= value


class FlashSafetyTests(unittest.TestCase):
    def test_invalid_jedec_error_includes_all_returned_bytes(self):
        with self.assertRaisesRegex(ApxError, "EF4049"):
            require_flash_id(bytes.fromhex("ef 40 49"))

    def test_write_requires_backup_and_confirm_before_erase(self):
        flash = FakeFlash()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "patch.bin"
            backup = root / "backup.bin"
            source.write_bytes(b"\x00\x01")
            with self.assertRaises(UnsafeOperation):
                write_file(
                    flash,
                    address=0x100,
                    input_path=source,
                    backup_path=backup,
                    confirmed=False,
                )
            self.assertTrue(backup.exists())
            self.assertEqual(backup.stat().st_size, flash.geometry.size)
            self.assertEqual(flash.erases, [])

    def test_partial_update_preserves_neighbor_bytes_and_verifies(self):
        flash = FakeFlash()
        sector = 0x1000
        initial = bytes((i * 17) & 0xFF for i in range(flash.geometry.sector_size))
        flash.bytes[sector : sector + len(initial)] = initial
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "patch.bin"
            backup = root / "backup.bin"
            source.write_bytes(b"\xAA\x55\x00")
            result = write_file(
                flash,
                address=sector + 0x103,
                input_path=source,
                backup_path=backup,
                confirmed=True,
            )
            expected = bytearray(initial)
            expected[0x103 : 0x106] = source.read_bytes()
            self.assertEqual(flash.bytes[sector : sector + len(expected)], expected)
            self.assertEqual(flash.erases, [sector])
            self.assertEqual(result.path, backup.resolve())
            self.assertEqual(backup.read_bytes()[sector : sector + len(initial)], initial)

    def test_full_image_write_skips_unchanged_sectors(self):
        flash = FakeFlash()
        source_image = bytearray(flash.bytes)
        source_image[0x1123] = 0x12
        source_image[0x5321] = 0x34
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "full-image.bin"
            backup = root / "backup.bin"
            source.write_bytes(source_image)

            result = write_file(
                flash,
                address=0,
                input_path=source,
                backup_path=backup,
                confirmed=True,
            )

            self.assertEqual(flash.erases, [0x1000, 0x5000])
            self.assertEqual(len(flash.programs), 2)
            self.assertEqual(bytes(flash.bytes), bytes(source_image))
            self.assertEqual(backup.read_bytes(), b"\xff" * flash.geometry.size)
            self.assertEqual(result.path, backup.resolve())

    def test_insufficient_callback_capacity_fails_before_sector_erase(self):
        class LimitedFlash(FakeFlash):
            def ensure_command_capacity(self, command_count):
                self.requested_commands = command_count
                if command_count > 2:
                    raise ProtocolUnavailable("callback slots exhausted")

        flash = LimitedFlash()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "patch.bin"
            backup = root / "backup.bin"
            source.write_bytes(b"\x00")
            with self.assertRaisesRegex(UnsafeOperation, "no flash sectors were changed"):
                write_file(
                    flash,
                    address=0x100,
                    input_path=source,
                    backup_path=backup,
                    confirmed=True,
                )
            self.assertEqual(flash.requested_commands, 3)
            self.assertTrue(backup.exists())
            self.assertEqual(flash.erases, [])
            self.assertEqual(flash.programs, [])

    def test_verify_detects_mismatch(self):
        flash = FakeFlash()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.bin"
            path.write_bytes(b"\x00")
            matches, expected, actual = verify_file(flash, 0, path)
            self.assertFalse(matches)
            self.assertNotEqual(expected, actual)

    def test_sector_erase_requires_backup_and_verifies_erased_state(self):
        flash = FakeFlash()
        flash.bytes[0x1000 : 0x2000] = b"\x00" * 0x1000
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / "backup.bin"
            result = erase_sector(
                flash,
                address=0x1000,
                backup_path=backup,
                confirmed=True,
            )
            self.assertEqual(result.size, flash.geometry.size)
            self.assertEqual(flash.erases, [0x1000])
            self.assertEqual(flash.read(0x1000, 0x1000), b"\xff" * 0x1000)

    def test_sector_verification_failure_keeps_backup(self):
        class CorruptAfterRead(FakeFlash):
            def read(self, address, length):
                data = bytearray(super().read(address, length))
                if address == 0:
                    data[0] ^= 1
                return bytes(data)

        flash = CorruptAfterRead()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "patch.bin"
            backup = root / "backup.bin"
            source.write_bytes(b"\x00")
            with self.assertRaises(ApxError):
                write_file(
                    flash,
                    address=0,
                    input_path=source,
                    backup_path=backup,
                    confirmed=True,
                )
            self.assertTrue(backup.exists())


if __name__ == "__main__":
    unittest.main()
