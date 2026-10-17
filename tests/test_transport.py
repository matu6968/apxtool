import unittest

from apxtool.errors import TransferError
from apxtool.transport import Inquiry, LinuxSCSITransport, SCSITransfer, require_complete


class FakeSGDevice:
    def __init__(self, residual=0):
        self.residual = residual
        self.calls = []
        self.closed = False

    def execute(self, cdb, data_out, data_in):
        self.calls.append((cdb, data_out, data_in))
        if data_in is not None:
            data_in[:] = b"R" * len(data_in)
        return self.residual

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def make_transport(self, residual=0):
        transport = LinuxSCSITransport.__new__(LinuxSCSITransport)
        transport._device = FakeSGDevice(residual)
        transport._scsi_exception = RuntimeError
        return transport

    def test_data_in_uses_requested_buffer_size_and_direction(self):
        transport = self.make_transport()
        result = transport.execute(b"\x12" + b"\x00" * 5, data_in_length=36)
        cdb, data_out, data_in = transport._device.calls[0]
        self.assertEqual(cdb[0], 0x12)
        self.assertIsNone(data_out)
        self.assertEqual(len(data_in), 36)
        self.assertEqual(result, SCSITransfer(b"R" * 36, 0))

    def test_data_out_and_no_data_keep_direction_unambiguous(self):
        transport = self.make_transport()
        payload = b"firmware"
        transport.execute(b"\xcb" + b"\xff" * 15, data_out=payload)
        transport.execute(b"\xda" + b"\xff" * 15)
        self.assertEqual(transport._device.calls[0][1:], (payload, None))
        self.assertEqual(transport._device.calls[1][1:], (None, None))

    def test_rejects_ambiguous_or_invalid_transfer_sizes(self):
        transport = self.make_transport()
        with self.assertRaises(ValueError):
            transport.execute(b"\x12", data_out=b"x", data_in_length=1)
        with self.assertRaises(ValueError):
            transport.execute(b"\x12", data_in_length=-1)
        with self.assertRaises(ValueError):
            transport.execute(b"\x12" * 17)

    def test_nonzero_residual_is_rejected(self):
        transport = self.make_transport(residual=1)
        transfer = transport.execute(b"\x12", data_in_length=4)
        with self.assertRaisesRegex(TransferError, "residual 1"):
            require_complete(transfer, 4, "fake transfer")

    def test_close_reaches_sg_device(self):
        transport = self.make_transport()
        transport.close()
        self.assertTrue(transport._device.closed)

    def test_camera_and_loader_inquiry_names(self):
        camera = Inquiry("Buildwin", "Media-Player", "1.00")
        loader = Inquiry("BuildWin", "Video050Loader", "1.00")
        self.assertTrue(camera.is_buildwin_camera)
        self.assertFalse(camera.is_loader)
        self.assertTrue(loader.is_buildwin_camera)
        self.assertTrue(loader.is_loader)


if __name__ == "__main__":
    unittest.main()
