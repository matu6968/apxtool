from __future__ import annotations

CDB_SIZE = 16
SCSI_PAD_BYTE = 0xFF
ENTER_LOADER_OPCODE = 0xDA
RAM_TRANSFER_OPCODE = 0xCB
RAM_TRANSFER_FUNCTION = 0x00102620
RAM_BASE = 0x02000000
RAM_SIZE = 0x00400000
RAM_LIMIT = RAM_BASE + RAM_SIZE
RAM_READ_CHUNK = 0x10000
RAM_WRITE_CHUNK = 0x200  # conservative candidate; writes remain experimental


def _base_cdb(opcode: int) -> bytearray:
    cdb = bytearray([SCSI_PAD_BYTE] * CDB_SIZE)
    cdb[0] = opcode
    return cdb


def build_enter_loader_cdb() -> bytes:
    return bytes(_base_cdb(ENTER_LOADER_OPCODE))


def build_ram_transfer_cdb(
    *,
    function_address: int,
    memory_address: int,
    callback_address: int = 0xFFFFFFFF,
) -> bytes:
    for name, value in (
        ("function address", function_address),
        ("memory address", memory_address),
        ("callback address", callback_address),
    ):
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f"{name} must fit in 32 bits")
    cdb = _base_cdb(RAM_TRANSFER_OPCODE)
    cdb[1:5] = function_address.to_bytes(4, "little")
    cdb[5:9] = memory_address.to_bytes(4, "little")
    cdb[9:13] = callback_address.to_bytes(4, "little")
    return bytes(cdb)


DEFAULT_FLASH_SIZE = 0x200000
DEFAULT_PAGE_SIZE = 256
DEFAULT_SECTOR_SIZE = 4096
DEFAULT_FLASH_CHUNK = 0x10000


class FlashGeometry:
    __slots__ = ("size", "page_size", "sector_size", "max_transfer", "major", "minor")

    def __init__(
        self,
        size: int,
        page_size: int,
        sector_size: int,
        max_transfer: int,
        major: int = 1,
        minor: int = 0,
    ) -> None:
        self.size = size
        self.page_size = page_size
        self.sector_size = sector_size
        self.max_transfer = min(max_transfer, DEFAULT_FLASH_CHUNK)
        self.major = major
        self.minor = minor

    def check_range(self, address: int, length: int) -> None:
        if address < 0 or length < 0 or address + length > self.size:
            raise ValueError(
                f"flash range 0x{address:x}+0x{length:x} exceeds 0x{self.size:x} bytes"
            )


