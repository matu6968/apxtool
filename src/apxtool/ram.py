from __future__ import annotations

from pathlib import Path
import os

from .errors import DeviceError, TransferError
from .protocol import (
    RAM_BASE,
    RAM_LIMIT,
    RAM_READ_CHUNK,
    RAM_TRANSFER_FUNCTION,
    RAM_TRANSFER_OPCODE,
    RAM_WRITE_CHUNK,
    build_ram_transfer_cdb,
)
from .transport import Inquiry, SCSITransport, require_complete


def validate_ram_range(address: int, length: int) -> None:
    if length < 0 or address < RAM_BASE or address + length > RAM_LIMIT:
        raise ValueError(
            f"RAM range must stay within 0x{RAM_BASE:08x}..0x{RAM_LIMIT:08x}"
        )


def read_ram(
    transport: SCSITransport,
    address: int,
    length: int,
    *,
    chunk_size: int = RAM_READ_CHUNK,
    function_address: int = RAM_TRANSFER_FUNCTION,
    callback_address: int = 0xFFFFFFFF,
) -> bytes:
    validate_ram_range(address, length)
    if not 1 <= chunk_size <= RAM_READ_CHUNK:
        raise ValueError(f"RAM read chunk must be between 1 and 0x{RAM_READ_CHUNK:x}")
    output = bytearray()
    while len(output) < length:
        count = min(chunk_size, length - len(output))
        cdb = build_ram_transfer_cdb(
            function_address=function_address,
            memory_address=address + len(output),
            callback_address=callback_address,
        )
        transfer = transport.execute(cdb, data_in_length=count)
        output.extend(require_complete(transfer, count, "0xcb RAM read"))
    return bytes(output)


def write_ram_experimental(
    transport: SCSITransport,
    address: int,
    data: bytes,
    *,
    chunk_size: int = RAM_WRITE_CHUNK,
    function_address: int = RAM_TRANSFER_FUNCTION,
) -> None:
    """Observed JT5295 0xcb data-out writer, conservatively capped at 512 bytes."""
    validate_ram_range(address, len(data))
    if not 1 <= chunk_size <= RAM_WRITE_CHUNK:
        raise ValueError(f"experimental RAM writes are limited to {RAM_WRITE_CHUNK} bytes")
    offset = 0
    while offset < len(data):
        block = data[offset : offset + chunk_size]
        cdb = build_ram_transfer_cdb(
            function_address=function_address,
            memory_address=address + offset,
        )
        transfer = transport.execute(cdb, data_out=block)
        if transfer.residual:
            raise TransferError(
                f"0xcb RAM write returned residual {transfer.residual} for {len(block)} bytes"
            )
        offset += len(block)


def dump_ram_file(
    transport: SCSITransport,
    identity: Inquiry,
    output_path: Path,
    address: int = RAM_BASE,
    length: int = RAM_LIMIT - RAM_BASE,
) -> None:
    if not identity.is_loader:
        raise DeviceError("RAM dumps require Buildwin Video050Loader mode (SCSI INQUIRY)")
    validate_ram_range(address, length)
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {output_path}")
    if not output_path.parent.is_dir():
        raise FileNotFoundError(output_path.parent)
    temp_path = output_path.with_name(output_path.name + ".part")
    if temp_path.exists():
        raise FileExistsError(temp_path)
    try:
        with temp_path.open("xb") as stream:
            offset = 0
            while offset < length:
                count = min(RAM_READ_CHUNK, length - offset)
                data = read_ram(transport, address + offset, count)
                stream.write(data)
                offset += len(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic create-if-absent; replace() would overwrite a file created
        # while the RAM dump was in progress.
        os.link(temp_path, output_path)
        temp_path.unlink()
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
