from pathlib import Path
import tempfile
import unittest

from apxtool.devices import enter_loader_with_device
from apxtool.transport import Inquiry, SCSITransfer


class FakeIdentityTransport:
    def __init__(self, identity):
        self.identity = identity
        self.closed = False
        self.commands = []

    def execute(self, cdb, *, data_out=None, data_in_length=0):
        self.commands.append((cdb, data_out, data_in_length))
        if data_in_length:
            data = bytearray(data_in_length)
            data[8:16] = self.identity.vendor.encode("ascii").ljust(8)[:8]
            data[16:32] = self.identity.product.encode("ascii").ljust(16)[:16]
            data[32:36] = self.identity.revision.encode("ascii").ljust(4)[:4]
            return SCSITransfer(bytes(data))
        return SCSITransfer()

    def close(self):
        self.closed = True


class LoaderTransitionTests(unittest.TestCase):
    def test_start_and_keep_reenumerated_loader_handle_open(self):
        normal = FakeIdentityTransport(Inquiry("Buildwin", "Media-Player", "1.00"))
        loader = FakeIdentityTransport(Inquiry("BuildWin", "Video050Loader", "1.00"))
        available = [normal, loader]

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sg0"
            path.touch()
            opened = []

            def opener(_path):
                device = available[len(opened)]
                opened.append(device)
                return device

            found_path, found_device, identity = enter_loader_with_device(
                str(path),
                opener=opener,
                paths=lambda: [str(path)],
                clock=lambda: 0.0,
                sleeper=lambda _delay: None,
            )

        self.assertEqual(found_path, str(path))
        self.assertIs(found_device, loader)
        self.assertTrue(identity.is_loader)
        self.assertTrue(normal.closed)
        self.assertFalse(loader.closed)
        self.assertEqual(normal.commands[-1][0], b"\xda" + b"\xff" * 15)
        loader.close()


if __name__ == "__main__":
    unittest.main()
