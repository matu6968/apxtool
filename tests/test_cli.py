import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from apxtool.cli import _run_flash
from apxtool.errors import ApxError
from apxtool.flasher import DDR, SRAM
from apxtool.protocol import FlashGeometry


class FakeFlashClient:
    def __init__(self, jedec=b"\x5e\x40\x15"):
        self.geometry = FlashGeometry(0x200000, 256, 4096, 0x8000)
        self.jedec = jedec
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def hello(self):
        self.calls.append(("hello",))
        return self.geometry

    def flash_id(self):
        self.calls.append(("id",))
        return self.jedec

    def read(self, address, length):
        self.calls.append(("read", address, length))
        return bytes((index & 0xFF for index in range(length)))


def flash_args(directory, **extra):
    payload = Path(directory) / "payload.bin"
    payload.write_bytes(b"flasher")
    return SimpleNamespace(timeout=20, payload=payload, region="ddr", **extra)


class FlashCliTests(unittest.TestCase):
    def test_read_checks_the_jedec_id_then_reads(self):
        client = FakeFlashClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "read.bin"
            args = flash_args(directory, flash_command="read", address=0x100, length=32, output=output)
            with patch("apxtool.cli.PayloadFlash", return_value=client) as opened:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = _run_flash(object(), args, "/dev/sg-test")
            self.assertEqual(opened.call_args.args[2], DDR)
            self.assertEqual(result, 0)
            self.assertEqual(client.calls, [("hello",), ("id",), ("read", 0x100, 32)])
            self.assertEqual(output.read_bytes(), bytes(range(32)))

    def test_region_option_selects_the_pool(self):
        client = FakeFlashClient()
        with tempfile.TemporaryDirectory() as directory:
            args = flash_args(directory, flash_command="id")
            args.region = "sram"
            with patch("apxtool.cli.PayloadFlash", return_value=client) as opened:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    _run_flash(object(), args, "/dev/sg-test")
            self.assertEqual(opened.call_args.args[2], SRAM)
            self.assertIn("5E4015", out.getvalue())

    def test_invalid_id_stops_before_any_read(self):
        client = FakeFlashClient(jedec=b"\xff\xff\xff")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "read.bin"
            args = flash_args(directory, flash_command="read", address=0, length=32, output=output)
            with patch("apxtool.cli.PayloadFlash", return_value=client):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(ApxError):
                        _run_flash(object(), args, "/dev/sg-test")
            self.assertEqual(client.calls, [("hello",), ("id",)])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
