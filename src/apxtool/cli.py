from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import __version__, rom_rescue
from .devices import enter_loader, enter_loader_with_device, require_loader, validate_camera_path
from .errors import ApxError, UnsafeOperation
from .flash import erase_sector, read_to_file, require_flash_id, verify_file, write_file
from .flasher import REGIONS, PayloadFlash, default_payload
from .protocol import (
    RAM_BASE,
    RAM_LIMIT,
    RAM_READ_CHUNK,
)
from .ram import dump_ram_file
from .transport import (
    Inquiry,
    LinuxSCSITransport,
    SCSITransport,
    inquiry,
    require_camera,
    require_complete,
)


def _integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value}") from exc


def _cdb_bytes(value: str) -> bytes:
    compact = value.replace(":", " ").replace(",", " ")
    try:
        data = bytes.fromhex(compact)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("CDB must be hexadecimal bytes") from exc
    if len(data) != 16:
        raise argparse.ArgumentTypeError("raw-cdb requires exactly 16 bytes (32 hex digits)")
    return data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apxtool",
        description="Experimental SCSI and loader utility for the AppoTech JT5295 camera",
    )
    parser.add_argument("--version", action="version", version=f"apxtool {__version__}")
    parser.add_argument(
        "--device",
        required=False,
        help="explicit Linux SCSI generic path, for example /dev/sg0",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    rom_rescue.add_arguments(sub)
    trace_cmd = sub.add_parser("trace", help="decode the SDK cold-boot flight recorder from a saved sector or flash dump")
    trace_cmd.add_argument("file", type=Path, help="4 KiB sector, or a flash dump (2 MiB: sector at 0x1ff000; 4 MiB Silvergear: sector at 0x3fe000)")

    sub.add_parser("info", help="read standard SCSI INQUIRY and show camera mode")

    loader = sub.add_parser("loader", help="control the camera's built-in loader")
    loader_sub = loader.add_subparsers(dest="loader_command", required=True)
    start = loader_sub.add_parser("start", help="send 0xda and wait for BLDR re-enumeration")
    start.add_argument("--timeout", type=float, default=20.0)

    ram = sub.add_parser("ram", help="read the known RAM window")
    ram_sub = ram.add_subparsers(dest="ram_command", required=True)
    dump = ram_sub.add_parser("dump", help="read RAM using 0xcb in bounded chunks")
    dump.add_argument("output", type=Path)
    dump.add_argument("--address", type=_integer, default=RAM_BASE)
    dump.add_argument("--length", type=_integer, default=RAM_LIMIT - RAM_BASE)
    dump.add_argument(
        "--start-loader",
        action="store_true",
        help="send 0xda and capture immediately after loader re-enumeration",
    )
    dump.add_argument("--timeout", type=float, default=20.0)

    raw = sub.add_parser("raw-cdb", help="send an exact 16-byte SCSI CDB to the camera")
    raw.add_argument("cdb", type=_cdb_bytes, help="32 hex digits; spaces or ':' are allowed")
    raw.add_argument("--direction", choices=("none", "in", "out"), default="none")
    raw.add_argument("--length", type=_integer, help="data-in length; required for --direction in")
    raw.add_argument("--input", type=Path, help="data-out file; required for --direction out")
    raw.add_argument("--output", type=Path, help="write data-in bytes here instead of printing hex")
    raw.add_argument("--unsafe-write", action="store_true", help="allow a raw data-out transfer")
    raw.add_argument("--yes", action="store_true", help="confirm raw data-out transfer")

    flash = sub.add_parser(
        "flash",
        help="read and update SPI NOR through the flasher payload in the BootROM loader (stock camera: DDR; "
        "forced BootROM: use rom-rescue)",
    )
    flash_sub = flash.add_subparsers(dest="flash_command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--payload", type=Path, default=None, help="flasher payload binary (default: SDK build output)")
    common.add_argument("--region", choices=sorted(REGIONS), default="ddr",
                        help="memory pool the payload runs from: ddr (stock camera in its loader, 32 KiB transfers) "
                        "or sram (512-byte transfers)")
    common.add_argument("--start-loader", action="store_true", help="send 0xda first (camera running its firmware)")
    common.add_argument("--timeout", type=float, default=20.0)

    flash_sub.add_parser("id", parents=[common], help="read the SPI NOR JEDEC ID")
    flash_read = flash_sub.add_parser("read", parents=[common], help="read bytes to a new file")
    flash_read.add_argument("output", type=Path)
    flash_read.add_argument("--address", type=_integer, default=0)
    flash_read.add_argument("--length", type=_integer, required=True)
    flash_write = flash_sub.add_parser("write", parents=[common], help="sector-preserving flash update")
    flash_write.add_argument("input", type=Path)
    flash_write.add_argument("--address", type=_integer, required=True)
    flash_write.add_argument("--backup", type=Path, required=True)
    flash_write.add_argument("--yes", action="store_true", required=True)
    flash_erase = flash_sub.add_parser("erase-sector", parents=[common], help="erase one sector after a full backup")
    flash_erase.add_argument("address", type=_integer)
    flash_erase.add_argument("--backup", type=Path, required=True)
    flash_erase.add_argument("--yes", action="store_true", required=True)
    verify = flash_sub.add_parser("verify", parents=[common], help="compare a file with bytes read from flash")
    verify.add_argument("input", type=Path)
    verify.add_argument("--address", type=_integer, required=True)

    from .debug_cli import configure
    configure(parser, sub)
    return parser


def _raw_data_out(transport: SCSITransport, args: argparse.Namespace) -> int:
    if not args.unsafe_write or not args.yes:
        raise UnsafeOperation("raw data-out requires both --unsafe-write and --yes")
    if args.input is None:
        raise ValueError("--direction out requires --input FILE")
    data = args.input.expanduser().read_bytes()
    if not data:
        raise ValueError("raw data-out file is empty")
    transfer = transport.execute(args.cdb, data_out=data)
    print(f"sent {len(data)} bytes; residual={transfer.residual}")
    return 0 if transfer.residual == 0 else 2


def _raw_data_in(transport: SCSITransport, args: argparse.Namespace) -> int:
    if args.length is None or args.length <= 0:
        raise ValueError("--direction in requires a positive --length")
    transfer = transport.execute(args.cdb, data_in_length=args.length)
    data = require_complete(transfer, args.length, "raw CDB data-in")
    if args.output is not None:
        path = args.output.expanduser().resolve()
        if not path.parent.is_dir():
            raise FileNotFoundError(path.parent)
        with path.open("xb") as stream:
            stream.write(data)
        print(f"received {len(data)} bytes; residual=0; wrote {path}")
    else:
        print(f"received {len(data)} bytes; residual=0")
        if len(data) <= 256:
            print(data.hex(" "))
    return 0


def _camera_transport(path: str) -> tuple[LinuxSCSITransport, Inquiry]:
    validate_camera_path(path)
    dev = LinuxSCSITransport(path)
    try:
        identity = inquiry(dev)
        require_camera(identity)
        return dev, identity
    except Exception:
        dev.close()
        raise


def _run_flash(dev: SCSITransport, args: argparse.Namespace, device_path: str) -> int:
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    payload = args.payload or default_payload()
    if not Path(payload).is_file():
        raise ApxError(f"flasher payload not built: {payload} (run make -C appotech-sdk/boot)")
    with PayloadFlash(dev, payload, REGIONS[args.region]) as flash_client:
        geometry = flash_client.hello()
        jedec_id = flash_client.flash_id()
        require_flash_id(jedec_id)
        if args.flash_command == "id":
            print(
                f"JEDEC ID: {jedec_id.hex().upper()}  "
                f"flash={geometry.size} bytes page={geometry.page_size} "
                f"sector={geometry.sector_size} max-transfer={geometry.max_transfer} ({args.region})"
            )
            return 0
        if args.flash_command == "read":
            digest = read_to_file(flash_client, args.address, args.length, args.output)
            print(f"read {args.length} bytes; SHA-256 {digest}")
            return 0
        if args.flash_command == "verify":
            matches, expected, actual = verify_file(flash_client, args.address, args.input)
            print(f"expected SHA-256 {expected}")
            print(f"actual   SHA-256 {actual}")
            print("verified" if matches else "mismatch")
            return 0 if matches else 2
        if args.flash_command == "erase-sector":
            flash_client.require_writable()
            backup = erase_sector(
                flash_client,
                address=args.address,
                backup_path=args.backup,
                confirmed=args.yes,
            )
            print(f"sector erase verified; full-chip backup: {backup.path} ({backup.sha256})")
            return 0
        if args.flash_command == "write":
            flash_client.require_writable()
            backup = write_file(
                flash_client,
                address=args.address,
                input_path=args.input,
                backup_path=args.backup,
                confirmed=args.yes,
            )
            print(f"write verified; full-chip backup: {backup.path} ({backup.sha256})")
            return 0
    raise AssertionError(f"unhandled flash command {args.flash_command} via {device_path}")


def _run(args: argparse.Namespace) -> int:
    if args.command == "rom-rescue":
        return rom_rescue.run_cli(args)
    if args.command == "trace":
        from . import trace as flight_recorder

        data = args.file.read_bytes()
        if len(data) in (2 * 1024 * 1024, 4 * 1024 * 1024):
            start = flight_recorder.sector_for(len(data))
            data = data[start:start + flight_recorder.SECTOR_SIZE]
        print(flight_recorder.format_report(flight_recorder.decode(data)))
        return 0
    if args.command in ("sdk", "debug"):
        from .debug_cli import run
        return run(args)
    validate_camera_path(args.device)

    if args.command == "loader":
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        path = enter_loader(args.device, timeout=args.timeout)
        print(f"camera is in Buildwin Video050Loader mode at {path}")
        return 0

    if args.command == "ram" and args.start_loader:
        path, dev, identity = enter_loader_with_device(args.device, timeout=args.timeout)
        try:
            if args.length <= 0:
                raise ValueError("RAM dump length must be positive")
            dump_ram_file(dev, identity, args.output, args.address, args.length)
            print(
                f"wrote {args.length} bytes from 0x{args.address:08x} to "
                f"{args.output.expanduser().resolve()} via {path}"
            )
            return 0
        finally:
            dev.close()

    if args.command == "flash":
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        if args.start_loader:
            path, dev, identity = enter_loader_with_device(args.device, timeout=args.timeout)
        else:
            path = args.device
            dev, identity = _camera_transport(path)
        try:
            require_loader(identity)
            return _run_flash(dev, args, path)
        finally:
            dev.close()

    if args.command == "raw-cdb":
        dev, _identity = _camera_transport(args.device)
        try:
            if args.direction == "none":
                transfer = dev.execute(args.cdb)
                print(f"no-data CDB completed; residual={transfer.residual}")
                return 0 if transfer.residual == 0 else 2
            if args.direction == "in":
                return _raw_data_in(dev, args)
            return _raw_data_out(dev, args)
        finally:
            dev.close()

    dev = LinuxSCSITransport(args.device)
    try:
        identity = inquiry(dev)
        if args.command == "info":
            print(f"Device:   {args.device}")
            print(f"Vendor:   {identity.vendor}")
            print(f"Product:  {identity.product}")
            print(f"Revision: {identity.revision}")
            if identity.is_loader:
                print("Mode:     Buildwin ROM loader (1908:3319 family)")
            elif identity.is_buildwin_camera:
                print("Mode:     normal camera firmware")
            else:
                print("Mode:     unknown SCSI target")
            return 0

        require_camera(identity)

        if args.command == "ram":
            require_loader(identity)
            if args.length <= 0:
                raise ValueError("RAM dump length must be positive")
            dump_ram_file(dev, identity, args.output, args.address, args.length)
            print(
                f"wrote {args.length} bytes from 0x{args.address:08x} to "
                f"{args.output.expanduser().resolve()} (chunk <= 0x{RAM_READ_CHUNK:x})"
            )
            return 0

        raise AssertionError(f"unhandled command {args.command}")
    finally:
        dev.close()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (ApxError, OSError, ValueError) as exc:
        print(f"apxtool: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
