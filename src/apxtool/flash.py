from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Protocol

from .errors import ApxError, UnsafeOperation
from .protocol import FlashGeometry


class FlashBackend(Protocol):
    geometry: FlashGeometry | None

    def hello(self) -> FlashGeometry: ...

    def flash_id(self) -> bytes: ...

    def read(self, address: int, length: int) -> bytes: ...

    def erase_sector(self, address: int) -> None: ...

    def program_page(self, address: int, data: bytes) -> None: ...


@dataclass(frozen=True)
class Backup:
    path: Path
    size: int
    sha256: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_new_file(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    temporary = path.with_name(path.name + ".part")
    if temporary.exists():
        raise FileExistsError(f"temporary output already exists: {temporary}")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Linking is atomic and fails if another process created the target
        # after the preflight check. Path.replace() would silently overwrite it.
        os.link(temporary, path)
        temporary.unlink()
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def save_full_backup(flash: FlashBackend, path: Path) -> Backup:
    geometry = flash.geometry or flash.hello()
    path = path.expanduser().resolve()
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    data = flash.read(0, geometry.size)
    if len(data) != geometry.size:
        raise ApxError(f"full-chip read returned {len(data)} bytes; expected {geometry.size}")
    _write_new_file(path, data)
    return Backup(path, len(data), _sha256(data))


def read_to_file(flash: FlashBackend, address: int, length: int, path: Path) -> str:
    geometry = flash.geometry or flash.hello()
    geometry.check_range(address, length)
    if length <= 0:
        raise ValueError("read length must be positive")
    path = path.expanduser().resolve()
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    data = flash.read(address, length)
    if len(data) != length:
        raise ApxError(f"flash read returned {len(data)} bytes; expected {length}")
    _write_new_file(path, data)
    return _sha256(data)


def verify_file(flash: FlashBackend, address: int, path: Path) -> tuple[bool, str, str]:
    geometry = flash.geometry or flash.hello()
    data = path.expanduser().read_bytes()
    geometry.check_range(address, len(data))
    if not data:
        raise ValueError("verification file is empty")
    actual = flash.read(address, len(data))
    expected_hash = _sha256(data)
    actual_hash = _sha256(actual)
    return actual == data, expected_hash, actual_hash


def _sector_image(original: bytes, offset: int, patch_start: int, patch: bytes) -> bytes:
    result = bytearray(original)
    overlap_start = max(offset, patch_start)
    overlap_end = min(offset + len(original), patch_start + len(patch))
    if overlap_start < overlap_end:
        src_start = overlap_start - patch_start
        dst_start = overlap_start - offset
        count = overlap_end - overlap_start
        result[dst_start : dst_start + count] = patch[src_start : src_start + count]
    return bytes(result)


def write_file(
    flash: FlashBackend,
    *,
    address: int,
    input_path: Path,
    backup_path: Path,
    confirmed: bool,
) -> Backup:
    """Back up the full flash, update sectors, and verify them by readback.

    ``confirmed`` represents the explicit CLI ``--yes`` gate. The fresh backup
    is written before any erase or program command is issued.
    """
    geometry = flash.geometry or flash.hello()
    data = input_path.expanduser().read_bytes()
    if not data:
        raise ValueError("write image is empty")
    geometry.check_range(address, len(data))

    backup_resolved = backup_path.expanduser().resolve()
    input_resolved = input_path.expanduser().resolve()
    if backup_resolved == input_resolved:
        raise UnsafeOperation("backup path must not be the same file as the write image")
    backup = save_full_backup(flash, backup_resolved)
    original = backup.path.read_bytes()
    if not confirmed:
        raise UnsafeOperation(
            f"fresh full-chip backup saved to {backup.path} ({backup.sha256}); "
            "repeat with --yes to erase/program the requested sectors"
        )

    first_sector = address // geometry.sector_size * geometry.sector_size
    end_address = address + len(data)
    last_sector = (end_address - 1) // geometry.sector_size * geometry.sector_size
    sector_offsets = range(first_sector, last_sector + 1, geometry.sector_size)
    updates: list[tuple[int, bytes, list[tuple[int, bytes]]]] = []
    required_commands = 0

    for sector in sector_offsets:
        before = original[sector : sector + geometry.sector_size]
        after = _sector_image(before, sector, address, data)
        if before == after:
            continue
        pages = [
            (page_offset, after[page_offset : page_offset + geometry.page_size])
            for page_offset in range(0, geometry.sector_size, geometry.page_size)
            if any(
                value != 0xFF
                for value in after[page_offset : page_offset + geometry.page_size]
            )
        ]
        # Each changed sector uses one erase, one readback, and one APXS call
        # for each non-erased page. Check the whole plan before the first erase.
        required_commands += 2 + len(pages)
        updates.append((sector, after, pages))

    ensure_capacity = getattr(flash, "ensure_command_capacity", None)
    if ensure_capacity is not None:
        try:
            ensure_capacity(required_commands)
        except ApxError as exc:
            raise UnsafeOperation(
                f"fresh full-chip backup saved to {backup.path} ({backup.sha256}); "
                f"no flash sectors were changed: {exc}"
            ) from exc

    for sector, after, pages in updates:
        flash.erase_sector(sector)
        for page_offset, page in pages:
            flash.program_page(sector + page_offset, page)
        observed = flash.read(sector, geometry.sector_size)
        if observed != after:
            raise ApxError(
                f"flash verification failed in sector 0x{sector:06x}; "
                f"backup is available at {backup.path}"
            )

    return backup


def erase_sector(
    flash: FlashBackend,
    *,
    address: int,
    backup_path: Path,
    confirmed: bool,
) -> Backup:
    geometry = flash.geometry or flash.hello()
    geometry.check_range(address, geometry.sector_size)
    if address % geometry.sector_size:
        raise ValueError("sector erase address must be sector aligned")
    backup = save_full_backup(flash, backup_path)
    if not confirmed:
        raise UnsafeOperation(
            f"fresh full-chip backup saved to {backup.path} ({backup.sha256}); "
            "repeat with --yes to erase the requested sector"
        )
    flash.erase_sector(address)
    observed = flash.read(address, geometry.sector_size)
    if observed != b"\xff" * geometry.sector_size:
        raise ApxError(
            f"sector erase verification failed at 0x{address:06x}; "
            f"backup is available at {backup.path}"
        )
    return backup


def require_flash_id(jedec_id: bytes) -> None:
    if len(jedec_id) != 3 or jedec_id in {b"\x00\x00\x00", b"\xff\xff\xff"}:
        raise ApxError(f"invalid SPI NOR JEDEC ID: {jedec_id.hex(' ')}")
    if jedec_id[2] != 0x15:
        raise ApxError(
            f"SPI NOR returned JEDEC bytes {jedec_id.hex().upper()} with capacity code "
            f"0x{jedec_id[2]:02x}; apxtool is configured for the camera's 2 MiB flash "
            "(capacity code 0x15)"
        )
