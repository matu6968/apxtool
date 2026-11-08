"""Host client for the position-independent SPI flasher payload (``appotech-sdk/boot/flasher.c``).

One payload image serves both memory pools of the BootROM loader (the same SCSI loader answers a forced BootROM
and a stock camera sent the ``0xda`` command):

* ``SRAM``: on-chip SRAM at 0x2000. The only memory a forced BootROM (USB 1908:3319, no DDR) has. 512-byte
  transfers, uncached, and the BootROM watchdog is disabled by the first call.
* ``DDR``: the 0x02180000 scratch window. Available once DDR is initialised: a stock camera after ``loader
  start``, or an SDK that returned to the loader with ``debug bootrom``. 32 KiB transfers; the payload writes
  back the data-cache lines of every request, and the loader's watchdog stays armed so the camera reboots into
  its firmware when the session ends. The scratch window must be zero beforehand and is zeroed again on exit.

The payload carries its own SPI driver (SPR level, no vendor routines), so it also works after the vendor
firmware has been replaced.
"""
from __future__ import annotations

import os
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .errors import ApxError, TransferError, UnsafeOperation
from .protocol import RAM_BASE, RAM_TRANSFER_FUNCTION, FlashGeometry, build_ram_transfer_cdb
from .sdk_flash import FlashClient
from .transport import inquiry, require_complete

HEADER = struct.Struct("<7I")  # magic, sequence, command, address, length, status, flags
MAGIC = 0x52585041
FLAG_KEEP_WATCHDOG = 1
PENDING = 0xFFFFFFFF
JEDEC = bytes.fromhex("5e4015")
READ_CAPACITIES = {0x15: 0x200000, 0x16: 0x400000}
WATCHDOG_SPR = 0x880C
SNAPSHOT_SPRS = (0x880C, 0x8858, 0x885C, 0x8864, 0x9000, 0x9244, 0x9248, 0x924C)

CMD_JEDEC, CMD_READ, CMD_PROGRAM, CMD_ERASE, CMD_SPR, CMD_RAM, CMD_SNAPSHOT, CMD_PM_ENABLE, CMD_PM_READ = range(9)
PM_REGISTERS = tuple(range(0x40, 0x5C, 2))  # read addresses; writes go to address + 1
JEDEC_ROM_STATE = 1  # command 0 address bit: probe with the BootROM's SPI setup
RAM_WRITE_CHUNK = 512
DDR_EVICTION_READ = 0x2000  # see Region.preflight


@dataclass(frozen=True)
class Region:
    name: str
    entry: int  # payload upload address and callback entry
    control: int  # request block; its data follows the 28-byte header. The payload's stack sits just below it
    end: int  # first address past the reservation
    capacity: int  # largest data transfer per command (must match the payload's window for this pool)
    code_limit: int  # payload size limit
    keep_watchdog: bool
    preflight: bool  # DDR is shared with the stock firmware: demand zeros first, restore them afterwards

    def check(self, address: int, length: int) -> None:
        if not (self.entry <= address and address + length <= self.end):
            raise ValueError(f"flasher {self.name} access 0x{address:x}+{length} outside its reservation")


# 0x2f00 upward holds the SDK's boot marks and post-mortem records, which a recovery must be able to read back.
SRAM = Region("sram", 0x2000, 0x3900, 0x3C00, 512, 0xF00, False, False)
DDR = Region("ddr", 0x02180000, 0x02182000, 0x02190000, 0x8000, 0x1000, True, True)
REGIONS = {region.name: region for region in (SRAM, DDR)}


def default_payload() -> Path:
    override = os.environ.get("APXTOOL_FLASHER")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "appotech-sdk" / "build" / "flasher" / "flasher.bin"


class PayloadFlash(FlashClient):
    """SPI NOR access through the flasher payload running in the loader's SRAM or DDR."""

    def __init__(self, device, image, region: Region = SRAM, *, spi_init: str = "auto"):
        if spi_init not in ("auto", "rom", "sdk", "none"):
            raise ValueError("spi_init must be auto, rom, sdk or none")
        self.device = device
        self.debug = SimpleNamespace(transport=device)
        self.region = region
        self.sequence = 0
        self.notes: list[str] = []
        self.chunk = region.capacity
        self.geometry: FlashGeometry | None = None
        self.jedec_id: bytes | None = None
        self._jedec_probe = JEDEC_ROM_STATE
        self._high_water = region.control  # highest address the payload may have written, for the DDR restore
        self._uploaded = False
        code = Path(image).read_bytes() if not isinstance(image, bytes) else image
        if not code or len(code) > region.code_limit:
            raise ValueError(f"flasher exceeds its {region.name} code reservation")
        self.payload_sha256 = hashlib.sha256(code).hexdigest()
        if not inquiry(device).is_loader:
            raise ValueError("BootROM loader (Buildwin Video050Loader) required")
        try:
            if region.preflight:
                # The window is free scratch only while it reads as zeros; anything else belongs to someone.
                if any(self._read(region.entry, region.end - region.entry)):
                    raise UnsafeOperation(
                        f"flasher scratch 0x{region.entry:08x}..0x{region.end - 1:08x} is not all zero; "
                        "refusing to overwrite it (power-cycle the camera, or use rom-rescue from the forced BootROM)"
                    )
            # Verify the upload before running it; the first call disables the watchdog (SRAM), and the whole
            # sequence is a few milliseconds of traffic.
            self._uploaded = True
            self._write(region.entry, code)
            if self._read(region.entry, len(code)) != code:
                raise TransferError("flasher upload readback failed")
            if region.preflight:
                # A related Buildwin camera needed this read to push recently written instructions out of its
                # data cache before they run; unverified on the JT5295 but harmless.
                self._transfer_in(RAM_BASE, DDR_EVICTION_READ)
            self.snapshot = self._take_snapshot()
            self.watchdog_off = self.snapshot[WATCHDOG_SPR] == 0
            if not self.watchdog_off and not region.keep_watchdog:
                self.notes.append(
                    f"watchdog SPR 0x880c reads {self.snapshot[WATCHDOG_SPR]:#x} after disable; "
                    "relying on continuous host traffic to keep the ROM alive"
                )
            self.spi_path = self._bring_up_spi(spi_init)
        except BaseException as error:
            try:
                self.close()
            except Exception as restore_error:
                raise ApxError(f"flasher start failed: {error}; scratch restore also failed: {restore_error}") from error
            raise

    # -- lifetime --------------------------------------------------------------------
    def __enter__(self) -> "PayloadFlash":
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        try:
            self.close()
        except Exception as restore_error:
            if exc is not None:
                raise ApxError(f"flash operation failed: {exc}; scratch restore also failed: {restore_error}") from exc
            raise
        return False

    def close(self) -> None:
        """Zero the DDR scratch the payload used (SRAM needs nothing: BootROM owns it)."""
        if not self.region.preflight or not self._uploaded:
            return
        span = self._high_water - self.region.entry
        zeros = bytes(span)
        self._write(self.region.entry, zeros)
        if self._read(self.region.entry, span) != zeros:
            raise ApxError("flasher scratch readback mismatch after restore; power cycle required")
        self._uploaded = False

    # -- low level -----------------------------------------------------------------
    @staticmethod
    def _cdb(address, callback=0xFFFFFFFF):
        return build_ram_transfer_cdb(
            function_address=RAM_TRANSFER_FUNCTION, memory_address=address, callback_address=callback
        )

    def _transfer_in(self, address, n):
        out = bytearray()
        while len(out) < n:
            part = min(0x10000, n - len(out))
            out.extend(require_complete(self.device.execute(self._cdb(address + len(out)), data_in_length=part), part, "flasher read"))
        return bytes(out)

    def _read(self, address, n):
        self.region.check(address, n)
        return self._transfer_in(address, n)

    def _write(self, address, data):
        self.region.check(address, len(data))
        for offset in range(0, len(data), RAM_WRITE_CHUNK):
            part = data[offset:offset + RAM_WRITE_CHUNK]
            # A data-out transfer carries no response bytes; only the residual says whether it completed.
            transfer = self.device.execute(self._cdb(address + offset), data_out=part)
            if transfer.residual:
                raise TransferError(f"flasher upload left residual {transfer.residual} of {len(part)} bytes")

    def _transact(self, cmd, address, length, data=b""):
        """Run one payload command; returns (status, response data)."""
        if cmd in (CMD_PROGRAM, CMD_ERASE):
            self.require_writable()
        if not 0 <= length <= self.region.capacity:
            raise ValueError(f"flasher {self.region.name} transfer exceeds {self.region.capacity} bytes")
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        flags = FLAG_KEEP_WATCHDOG if self.region.keep_watchdog else 0
        request = HEADER.pack(MAGIC, self.sequence, cmd, address, length, PENDING, flags) + data
        control = self.region.control
        self._write(control, request)
        if cmd in (CMD_PROGRAM, CMD_ERASE) and self._read(control, len(request)) != request:
            raise TransferError("flasher request readback mismatch")
        self.device.execute(self._cdb(control, self.region.entry), data_in_length=HEADER.size)
        reply = 3 if cmd == CMD_JEDEC else length
        self._high_water = max(self._high_water, control + HEADER.size + max(reply, len(data)))
        result = self._read(control, HEADER.size + reply)
        magic, seq, operation, a, n, status, _flags = HEADER.unpack_from(result)
        if (magic, seq, operation, a, n) != (MAGIC, self.sequence, cmd, address, length):
            raise TransferError("stale flasher response")
        return status, result[HEADER.size:]

    def command(self, cmd, address, length, data=b""):
        status, result = self._transact(cmd, address, length, data)
        if status == PENDING:
            raise TransferError(f"flasher did not answer command {cmd}: the request is still pending (payload not running, or it rejected the header)")
        if status:
            raise TransferError(f"flasher status {status} for command {cmd}")
        return result

    # -- bring-up --------------------------------------------------------------------
    def _take_snapshot(self):
        data = self.command(CMD_SNAPSHOT, 0, 32)
        return dict(zip(SNAPSHOT_SPRS, struct.unpack("<8I", data)))

    def _bring_up_spi(self, mode):
        if mode == "none":  # PM-chip work needs no flash bus
            return "not used"
        attempts = []
        if mode in ("auto", "rom"):
            status, data = self._transact(CMD_JEDEC, JEDEC_ROM_STATE, 0)
            if status in (0, 3) and self._accept_jedec(data):
                if status:
                    raise ApxError(f"payload rejected JEDEC {data.hex()} with status {status}; rebuild appotech-sdk/boot")
                return "rom-state"
            attempts.append(f"ROM state read {data.hex()} (status {status})")
        if mode in ("auto", "sdk"):
            status, data = self._transact(CMD_JEDEC, 0, 0)
            if status in (0, 3) and self._accept_jedec(data):
                if status:
                    raise ApxError(f"payload rejected JEDEC {data.hex()} with status {status}; rebuild appotech-sdk/boot")
                self._jedec_probe = 0
                return "sdk-init"
            attempts.append(f"SDK init read {data.hex()} (status {status})")
        raise TransferError(
            "flasher could not read a valid JEDEC ID ("
            + "; ".join(attempts)
            + "). Is the SPI short released and the flash powered?"
        )

    def _accept_jedec(self, data):
        if len(data) != 3 or data in (b"\x00" * 3, b"\xff" * 3):
            return False
        self.jedec_id = data
        capacity = READ_CAPACITIES.get(data[2])
        self.geometry = FlashGeometry(capacity, 256, 4096, self.chunk) if capacity else None
        return True

    @property
    def capacity(self):
        return self.hello().size

    def require_writable(self):
        if self.jedec_id != JEDEC:
            identity = self.jedec_id.hex() if self.jedec_id is not None else "not probed"
            raise UnsafeOperation(f"flash {identity} is read-only; erase/program require JEDEC {JEDEC.hex()}")

    # -- FlashClient / flash.FlashBackend interface ----------------------------------------
    def _request(self, command, payload=b""):
        if not command:
            return json.dumps({
                "jedec": int.from_bytes(self.jedec_id, "big") if self.jedec_id is not None else None,
                "capacity": self.geometry.size if self.geometry else None,
                "host_read_only": self.jedec_id != JEDEC,
                "write_protocol": 1 if self.jedec_id == JEDEC else 0,
                "backend": "flasher-" + self.region.name,
            }).encode()
        if command == 1:
            address, length = struct.unpack("<II", payload)
            self.hello().check_range(address, length)
            result = bytearray()
            while len(result) < length:
                n = min(self.region.capacity, length - len(result))
                result.extend(self.command(CMD_READ, address + len(result), n))
            return bytes(result)
        if command in (2, 3) and payload[:4] == b"APXF":
            address = struct.unpack_from("<I", payload, 4)[0]
            self.command(command, address, len(payload) - 8, payload[8:])
            return b""
        raise ValueError("unsupported flasher command")

    def hello(self) -> FlashGeometry:
        if self.geometry is None:
            identity = self.jedec_id.hex() if self.jedec_id is not None else "not probed"
            raise ApxError(f"unsupported flash capacity for JEDEC {identity}; reads require capacity code 0x15 or 0x16")
        return self.geometry

    def flash_id(self) -> bytes:
        data = self.command(CMD_JEDEC, self._jedec_probe, 0)
        if not self._accept_jedec(data):
            raise TransferError(f"invalid SPI NOR JEDEC ID: {data.hex()}")
        return data

    def read_sram(self, address, n):
        """Plain BootROM RAM read (no payload command): SRAM survives resets, e.g. the SDK's boot marks."""
        return self._read(address, n)

    def pm_read(self):
        """Registers 0x40..0x5a of the always-powered PM chip, as {address: value}."""
        data = self.command(CMD_PM_READ, 0, len(PM_REGISTERS))
        return dict(zip(PM_REGISTERS, data))

    def pm_enable(self):
        """Apply the stock start-up power sequence; returns the registers read back afterwards."""
        data = self.command(CMD_PM_ENABLE, 0, len(PM_REGISTERS))
        return dict(zip(PM_REGISTERS, data))

    def spr(self, address):
        return struct.unpack("<I", self.command(CMD_SPR, address, 4))[0]
