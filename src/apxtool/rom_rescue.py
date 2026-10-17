"""On-chip SRAM recovery for a camera stuck in the forced BootROM loader.

The forced BootROM (USB 1908:3319, ``BLDR v1.00``) has no DDR and none of the
vendor application's SPI routines. This module uploads the flasher payload from
``appotech-sdk/boot`` into SRAM through the ROM's ``0xcb`` command (see
:mod:`apxtool.flasher`), runs it, and uses it for JEDEC/read/erase/program. The
ROM resets itself about two seconds after the last USB interrupt, so the first
payload call disables its watchdog and the host keeps traffic flowing until then.
"""
from __future__ import annotations

import glob
import hashlib
import os
import struct
import time
from pathlib import Path

from .errors import ApxError, DeviceError
from .flasher import PM_REGISTERS, SRAM, WATCHDOG_SPR, PayloadFlash, default_payload
from .sdk_flash import CAPACITY
from .transport import LinuxSCSITransport

LOADER_ID = ("1908", "3319")

# Images whose provenance is recorded in the repository, for dump identification.
KNOWN_IMAGES = {
    "bc2086e3a8173066fe618788653ffe43019047f63f9f63012355eb0dcc68ebe1": "SDK camera-demo SPI image (failed cold boot)",
    "063e10e7df19095f5ee8217cb031e03297941194f460959a47056c12a38dab66": "vendor image with a saved test photo, pre-SDK-install backup (not shipped)",
    "2d0d864fb3092c28ff174f73a6df7d7a47fb12ccbce1562f2b7bfc66d7d02137": "stock firmware with user photos removed (Action_Digital_Kids_Camera_DOOM/dumps/flash.bin)",
    "03d9919a02a302b660b4a19eb1a1f3031e53d65b8407e5c4b0e5f0185b89c961": "vendor image, flash.bin.golden",
}


def check_boot_image(image: bytes) -> None:
    """Refuse images the BootROM would not accept (checks mirror BootROM 0x100440+)."""
    if len(image) != CAPACITY:
        raise ValueError("boot image must be exactly 2 MiB")
    if struct.unpack_from("<H", image, 2)[0] != 2 or image[4:8] != b"BLDR":
        raise ValueError("image lacks the BLDR boot header")
    if image[510:512] != b"\x55\xaa":
        raise ValueError("image header sector lacks the 0x55aa signature")


# -- device discovery ---------------------------------------------------------------
def loader_sg_paths() -> list[str]:
    """SCSI generic nodes of USB devices identifying as the forced BootROM."""
    found = []
    for device in glob.glob("/sys/bus/usb/devices/*"):
        try:
            ids = (Path(device, "idVendor").read_text().strip(), Path(device, "idProduct").read_text().strip())
        except OSError:
            continue
        if ids != LOADER_ID:
            continue
        for node in glob.glob(f"{device}/*/host*/target*/*/scsi_generic/sg*"):
            found.append("/dev/" + Path(node).name)
    return sorted(found)


def open_loader(path: str, *, grace: float = 1.0) -> LinuxSCSITransport:
    """Open an sg node, riding out the short window before udev grants access."""
    deadline = time.monotonic() + grace
    while True:
        try:
            return LinuxSCSITransport(path)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _flush_print(*args):
    print(*args, flush=True)


def run_when_caught(action, *, payload, timeout, spi_init="auto", device=None, log=_flush_print, poll=0.005, attempt_limit=None):
    """Catch the forced BootROM, bring up the rescue payload and call ``action(client, attempt)``.

    The ROM cycles every few seconds while idle, so a failure after the catch
    simply waits for the next cycle and starts again; ``action`` must be safe to
    repeat (all of this module's actions are).
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    last = None
    log("waiting for the forced BootROM (USB 1908:3319): hold the SPI short, then connect USB power")
    while time.monotonic() < deadline and (attempt_limit is None or attempt < attempt_limit):
        paths = [device] if device else loader_sg_paths()
        if not paths:
            time.sleep(poll)
            continue
        attempt += 1
        started = time.monotonic()
        try:
            with open_loader(paths[0]) as dev:
                client = PayloadFlash(dev, payload, SRAM, spi_init=spi_init)
                log(
                    f"attempt {attempt}: rescue payload up in {time.monotonic() - started:.2f}s, "
                    f"SPI via {client.spi_path}, watchdog SPR {client.snapshot[WATCHDOG_SPR]:#x}"
                )
                for note in client.notes:
                    log("note: " + note)
                return action(client, attempt)
        except (OSError, ApxError, ValueError) as exc:
            last = exc
            log(f"attempt {attempt} failed after {time.monotonic() - started:.2f}s: {exc}")
            # Let the ROM drop off the bus before looking for the next cycle.
            time.sleep(0.5)
    if not attempt:
        raise DeviceError(f"no forced-BootROM device (USB 1908:3319) appeared within {timeout:g}s")
    raise DeviceError(f"no rescue session completed before the timeout (last error: {last})")


# -- actions -------------------------------------------------------------------------
def describe_image(image: bytes) -> str:
    digest = hashlib.sha256(image).hexdigest()
    return f"{digest} ({KNOWN_IMAGES.get(digest, 'unrecognised image')})"


def action_jedec(client, attempt):
    header = client.read(0, 512)
    flags = header[10]
    valid = struct.unpack_from("<H", header, 2)[0] == 2 and header[4:8] == b"BLDR" and header[510:512] == b"\x55\xaa"
    return {
        "jedec": "5e4015",
        "spi_path": client.spi_path,
        "watchdog_off": client.watchdog_off,
        "snapshot": {f"{k:#06x}": f"{v:#010x}" for k, v in client.snapshot.items()},
        "boot_header_valid": valid,
        "header_flags": flags,
        "load_address": struct.unpack_from("<I", header, 0x20)[0],
        "load_sectors": struct.unpack_from("<I", header, 0x28)[0],
        "header_sha256": hashlib.sha256(header).hexdigest(),
    }


def action_pm(client, attempt, apply):
    before = client.pm_read()
    after = client.pm_enable() if apply else None
    return {"before": before, "after": after, "keep_alive": (after or before)[0x5A] == 0x08}


def format_pm(result):
    lines = ["PM chip registers (stock-running camera: 0x40=0x16 0x44=0x00 0x54=0x21 0x58=0x0c 0x5a=0x08):"]
    for address in PM_REGISTERS:
        before = result["before"][address]
        after = result["after"][address] if result["after"] else None
        lines.append(f"  0x{address:02x}: {before:#04x}" + (f" -> {after:#04x}" if after is not None and after != before else ""))
    lines.append("keep-alive register 0x5a = 0x08: " + ("yes" if result["keep_alive"] else "NO"))
    return "\n".join(lines)


def action_marks(client, attempt):
    from . import trace as flight_recorder

    return flight_recorder.decode_marks(client.read_sram(flight_recorder.MARKS_ADDRESS, flight_recorder.MARKS_BYTES))


def action_dump(client, attempt, output):
    output = Path(output)
    image = client.read(0, CAPACITY)
    with output.open("xb") as file:
        file.write(image)
        file.flush()
        os.fsync(file.fileno())
    return describe_image(image)


def action_restore(client, attempt, image, backup, expected_sha256, log=_flush_print):
    """Back up the chip, then restore ``image`` (header sector last, per-sector verify)."""
    backup = Path(backup)
    if attempt > 1:
        backup = backup.with_name(f"{backup.name}.retry{attempt}")
    current = client.read(0, CAPACITY)
    digest = hashlib.sha256(current).hexdigest()
    log(f"chip currently holds {describe_image(current)}")
    from . import trace as flight_recorder

    recorded = current[flight_recorder.SECTOR:flight_recorder.SECTOR + flight_recorder.SECTOR_SIZE]
    if recorded[:4] == b"APXT":  # evidence of the last boot, before the sector is rewritten
        log("flight recorder found on the chip:\n" + flight_recorder.format_report(flight_recorder.decode(recorded)))
    if expected_sha256 not in (None, "auto") and digest != expected_sha256.lower():
        raise ApxError(f"chip hash {digest} differs from --expect-current-sha256; nothing was erased")
    if current == image:
        log("chip already holds the requested image")
        return {"status": "unchanged", "sha256": digest}
    changed = sum(1 for a in range(0, CAPACITY, 4096) if current[a:a + 4096] != image[a:a + 4096])
    log(f"writing {changed} changed 4 KiB sectors; do not disconnect")
    state = client.replace(
        image,
        backup,
        expected_sha256=digest,
        current=current,
        progress=lambda done, total: log(f"  verified sector {done}/{total}") if done % 25 == 0 or done == total else None,
    )
    return state


# -- command line --------------------------------------------------------------------
def add_arguments(sub) -> None:
    rescue = sub.add_parser(
        "rom-rescue",
        help="recover flash through the forced BootROM loader (USB 1908:3319) using an SRAM payload",
    )
    rescue.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for a completed session")
    rescue.add_argument("--payload", type=Path, default=None, help="flasher payload binary (default: SDK build output)")
    rescue.add_argument("--spi-init", choices=("auto", "rom", "sdk", "none"), default="auto",
                        help="SPI setup: reuse the BootROM's, apply the SDK's, or try both (default)")
    rescue.add_argument("--yes", action="store_true", help="acknowledge destructive flash writes")
    actions = rescue.add_subparsers(dest="rescue_command", required=True)
    actions.add_parser("jedec", help="read-only: JEDEC ID, boot header sector and diagnostic SPRs")
    dump = actions.add_parser("dump", help="read the whole 2 MiB chip into a new file")
    dump.add_argument("output", type=Path)
    trace = actions.add_parser("trace", help="read-only: decode the SDK cold-boot flight recorder sector")
    trace.add_argument("--save", type=Path, help="also save the raw 4 KiB sector to a new file")
    actions.add_parser("marks", help="read-only: the production build's always-on SRAM boot breadcrumbs (0x2f00)")
    pm = actions.add_parser("pm", help="read the PM chip registers; with --apply, run the stock power sequence (no flash access)")
    pm.add_argument("--apply", action="store_true", help="write the stock start-up sequence (requires --yes)")
    restore = actions.add_parser("restore", help="back up the chip, then write a boot image over it")
    restore.add_argument("image", type=Path)
    restore.add_argument("--backup", type=Path, required=True, help="new file for the pre-restore chip contents")
    restore.add_argument("--expect-current-sha256", default="auto",
                         help="refuse to erase unless the chip hashes to this value (default: accept and record)")


def run_cli(args) -> int:
    import json

    payload = args.payload or default_payload()
    if not Path(payload).is_file():
        raise ApxError(f"flasher payload not built: {payload} (run make -C appotech-sdk/boot)")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    common = dict(payload=payload, timeout=args.timeout, spi_init=args.spi_init, device=args.device)
    if args.rescue_command == "jedec":
        print(json.dumps(run_when_caught(action_jedec, **common), indent=2))
        return 0
    if args.rescue_command == "pm":
        if args.apply and not args.yes:
            raise ApxError("--apply writes PM chip registers; pass --yes to confirm")
        result = run_when_caught(lambda c, n: action_pm(c, n, args.apply), **{**common, "spi_init": "none"})
        print(format_pm(result))
        return 0
    if args.rescue_command == "marks":
        from . import trace as flight_recorder

        print(flight_recorder.format_marks(run_when_caught(action_marks, **{**common, "spi_init": "none"})))
        return 0
    if args.rescue_command == "trace":
        from . import trace as flight_recorder

        if args.save and args.save.exists():
            raise FileExistsError(f"refusing to overwrite {args.save}")

        def read_trace(client, attempt):
            sector = client.read(flight_recorder.SECTOR, flight_recorder.SECTOR_SIZE)
            if args.save:
                with args.save.open("xb") as file:
                    file.write(sector)
            return sector

        print(flight_recorder.format_report(flight_recorder.decode(run_when_caught(read_trace, **common))))
        return 0
    if args.rescue_command == "dump":
        if args.output.exists():
            raise FileExistsError(f"refusing to overwrite {args.output}")
        print("dumped", run_when_caught(lambda c, n: action_dump(c, n, args.output), **common))
        return 0
    # restore: validate everything locally before touching the device.
    if not args.yes:
        raise ApxError("restore erases and reprograms flash; pass --yes to confirm")
    image = args.image.read_bytes()
    check_boot_image(image)
    if args.backup.exists() or args.backup.with_suffix(args.backup.suffix + ".json").exists():
        raise FileExistsError(f"backup/journal must be new: {args.backup}")
    print(f"restoring {describe_image(image)}")
    state = run_when_caught(
        lambda c, n: action_restore(c, n, image, args.backup, args.expect_current_sha256), **common
    )
    print(json.dumps({k: v for k, v in state.items() if k not in ("verified_sectors", "changed_sectors")}, indent=2))
    return 0
