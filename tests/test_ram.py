import unittest
import tempfile
from pathlib import Path

from apxtool.errors import DeviceError, TransferError
from apxtool.protocol import RAM_BASE, RAM_READ_CHUNK
from apxtool.ram import dump_ram_file, read_ram, validate_ram_range
from apxtool.transport import Inquiry, SCSITransfer


class FakeTransport:
    def __init__(self, short_at: int | None = None):
        self.requests: list[tuple[bytes, int]] = []
        self.short_at = short_at

    def execute(self, cdb, *, data_out=None, data_in_length=0):
        self.requests.append((cdb, data_in_length))
        address = int.from_bytes(cdb[5:9], "little")
        data = bytes((address + index) & 0xFF for index in range(data_in_length))
        if self.short_at == address:
            return SCSITransfer(data, 1)
        return SCSITransfer(data, 0)

    def close(self):
        pass


class RamTests(unittest.TestCase):
    def test_reads_are_chunked_and_addresses_advance(self):
        dev = FakeTransport()
        data = read_ram(dev, RAM_BASE, 41, chunk_size=16)
        self.assertEqual(len(data), 41)
        self.assertEqual([n for _, n in dev.requests], [16, 16, 9])
        self.assertEqual(
            [int.from_bytes(cdb[5:9], "little") for cdb, _ in dev.requests],
            [RAM_BASE, RAM_BASE + 16, RAM_BASE + 32],
        )

    def test_rejects_chunks_larger_than_proven_limit(self):
        with self.assertRaises(ValueError):
            read_ram(FakeTransport(), RAM_BASE, 1, chunk_size=RAM_READ_CHUNK + 1)

    def test_short_transfer_is_rejected(self):
        dev = FakeTransport(short_at=RAM_BASE)
        with self.assertRaises(TransferError):
            read_ram(dev, RAM_BASE, 16, chunk_size=16)

    def test_range_is_bounded_to_captured_window(self):
        validate_ram_range(RAM_BASE, 0x400000)
        with self.assertRaises(ValueError):
            validate_ram_range(RAM_BASE - 1, 1)
        with self.assertRaises(ValueError):
            validate_ram_range(RAM_BASE, 0x400001)

    def test_dump_refuses_to_overwrite_existing_output(self):
        dev = FakeTransport()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ram.bin"
            path.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                dump_ram_file(dev, Inquiry("Buildwin", "Video050Loader", "1"), path, RAM_BASE, 4)
            self.assertEqual(path.read_bytes(), b"keep")
            self.assertEqual(dev.requests, [])

    def test_dump_requires_loader_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(DeviceError):
                dump_ram_file(
                    FakeTransport(),
                    Inquiry("Buildwin", "Media-Player", "1"),
                    Path(temp) / "ram.bin",
                    RAM_BASE,
                    4,
                )


if __name__ == "__main__":
    unittest.main()
