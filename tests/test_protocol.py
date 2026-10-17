import unittest

from apxtool.protocol import (
    FlashGeometry,
    build_enter_loader_cdb,
    build_ram_transfer_cdb,
)


class ProtocolTests(unittest.TestCase):
    def test_enter_loader_cdb_is_16_bytes_and_padded(self):
        self.assertEqual(build_enter_loader_cdb(), b"\xda" + b"\xff" * 15)

    def test_ram_read_cdb_matches_observed_callback_fields(self):
        cdb = build_ram_transfer_cdb(
            function_address=0x00102620,
            memory_address=0x02000000,
            callback_address=0x02180000,
        )
        self.assertEqual(len(cdb), 16)
        self.assertEqual(cdb[0], 0xCB)
        self.assertEqual(cdb[1:5], bytes.fromhex("20 26 10 00"))
        self.assertEqual(cdb[5:9], bytes.fromhex("00 00 00 02"))
        self.assertEqual(cdb[9:13], bytes.fromhex("00 00 18 02"))
        self.assertEqual(cdb[13:], b"\xff" * 3)

    def test_flash_geometry_bounds(self):
        geometry = FlashGeometry(0x200000, 256, 4096, 0x10000)
        geometry.check_range(0x1FFFFF, 1)
        with self.assertRaises(ValueError):
            geometry.check_range(0x1FFFFF, 2)


if __name__ == "__main__":
    unittest.main()
