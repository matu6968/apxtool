"""The host client against the real flasher payload logic (appotech-sdk/boot/flasher.c) compiled for the host.

A fake BootROM loader executes the 0xcb transfers: SRAM and DDR memory are plain buffers, a callback runs the
payload's ``rescue()`` on the request block, and the payload's SPI controller, NOR chip, PM bus and cache are the
model in appotech-sdk/tests/flasher_model.c. Nothing here is hardware evidence; it checks that both halves agree
on the protocol and that the DDR pool's scratch discipline holds."""
import ctypes
import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from apxtool import cli, flasher, rom_rescue
from apxtool.errors import ApxError, TransferError, UnsafeOperation
from apxtool.flash import write_file
from apxtool.protocol import RAM_TRANSFER_FUNCTION
from apxtool.sdk_flash import CAPACITY
from apxtool.transport import SCSITransfer

SDK = Path(__file__).resolve().parents[2] / "appotech-sdk"
CODE = bytes(range(251)) * 8  # the model never executes the payload bytes
SRAM_BASE, SRAM_SIZE, DDR_BASE, DDR_SIZE = 0x2000, 0x2000, 0x02000000, 0x190000

pytestmark = pytest.mark.skipif(
    not (SDK / "boot/flasher.c").is_file() or shutil.which("cc") is None,
    reason="needs the SDK checkout and a host C compiler",
)


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    path = tmp_path_factory.mktemp("flasher") / "flasher_model.so"
    subprocess.run(
        ["cc", "-std=gnu11", "-O1", "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror", "-DFLASHER_HOST",
         str(SDK / "boot/flasher.c"), str(SDK / "tests/flasher_model.c"), "-o", str(path)],
        check=True,
    )
    lib = ctypes.CDLL(str(path))
    lib.model_nor.restype = ctypes.POINTER(ctypes.c_uint8 * CAPACITY)
    lib.model_wdt.restype = ctypes.c_uint32
    lib.model_set_sram_window.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.model_call.argtypes = [ctypes.c_void_p]
    lib.model_sync_count.restype = ctypes.c_uint
    return lib


class LoaderModel:
    """BootROM loader SCSI behaviour for INQUIRY and 0xcb, backed by the compiled payload."""

    def __init__(self, lib, *, ddr_alive=True, jedec=flasher.JEDEC):
        lib.model_reset()
        lib.model_set_flash(int.from_bytes(jedec, "big"))
        self.lib = lib
        self.sram = ctypes.create_string_buffer(SRAM_SIZE)
        self.ddr = ctypes.create_string_buffer(DDR_SIZE)
        lib.model_set_sram_window(ctypes.addressof(self.sram), SRAM_SIZE)
        self.ddr_alive = ddr_alive
        self.callbacks = []
        self.corrupt_next_write = False
        self.skip_callback = False
        capacity = flasher.READ_CAPACITIES.get(jedec[2], CAPACITY)
        self.nor = (ctypes.c_uint8 * capacity).from_address(ctypes.addressof(lib.model_nor().contents))

    def _where(self, address, n):
        if SRAM_BASE <= address and address + n <= SRAM_BASE + SRAM_SIZE:
            return self.sram, address - SRAM_BASE
        if self.ddr_alive and DDR_BASE <= address and address + n <= DDR_BASE + DDR_SIZE:
            return self.ddr, address - DDR_BASE
        raise AssertionError(f"0xcb access to unmapped 0x{address:08x}+{n}")

    def execute(self, cdb, *, data_out=None, data_in_length=0):
        if cdb[0] == 0x12:
            return SCSITransfer(b"\x00" * 8 + b"BuildWin".ljust(8) + b"Video050Loader".ljust(16) + b"1.00", 0)
        assert cdb[0] == 0xCB and cdb[1:5] == RAM_TRANSFER_FUNCTION.to_bytes(4, "little")
        address = int.from_bytes(cdb[5:9], "little")
        callback = int.from_bytes(cdb[9:13], "little")
        if data_out is not None:
            buffer, offset = self._where(address, len(data_out))
            block = bytearray(data_out)
            if self.corrupt_next_write:
                self.corrupt_next_write = False
                block[0] ^= 1
            ctypes.memmove(ctypes.addressof(buffer) + offset, bytes(block), len(block))
            return SCSITransfer(b"", 0)
        buffer, offset = self._where(address, data_in_length)
        if callback != 0xFFFFFFFF and not self.skip_callback:
            self.callbacks.append((address, callback))
            self.lib.model_call(ctypes.addressof(buffer) + offset)
        return SCSITransfer(ctypes.string_at(ctypes.addressof(buffer) + offset, data_in_length), 0)

    def close(self):
        pass

    def ddr_scratch(self):
        return ctypes.string_at(ctypes.addressof(self.ddr) + 0x180000, 0x10000)


def fill_nor(loader, seed=5):
    data = bytes((index * 13 + seed) & 0xFF for index in range(CAPACITY))
    ctypes.memmove(loader.nor, data, CAPACITY)
    return data


@pytest.mark.parametrize("region", [flasher.SRAM, flasher.DDR], ids=lambda r: r.name)
@pytest.mark.parametrize("jedec", [bytes.fromhex("5e4016"), bytes.fromhex("ef4016"), bytes.fromhex("944016")])
def test_four_mib_read_bounds_and_write_guards(library, region, jedec):
    loader = LoaderModel(library, jedec=jedec, ddr_alive=region != flasher.SRAM)
    # Distinct halves catch a two-MiB address wrap, including boundary-spanning reads.
    original = b"\x12" * CAPACITY + b"\x34" * (CAPACITY - 1) + b"\x56"
    ctypes.memmove(loader.nor, original, len(original))
    with flasher.PayloadFlash(loader, CODE, region, spi_init="rom") as client:
        assert client.flash_id() == jedec and client.capacity == 0x400000
        info = client.info()
        assert info["capacity"] == 0x400000 and info["jedec"] == int.from_bytes(jedec, "big")
        assert info["host_read_only"] and info["write_protocol"] == 0
        assert client.read(0x1FFFFF, 2) == b"\x12\x34"
        assert client.read(0x200000, 513) == b"\x34" * 513
        assert client.read(0x3FFFFF, 1) == b"\x56"
        assert client.read(0x400000, 0) == b""
        before = len(loader.callbacks)
        for a, n in ((0x3FFFFF, 2), (0x400000, 1), (0x100000000, 0), (-1, 1), (False, 1)):
            with pytest.raises(ValueError):
                client.read(a, n)
        for write in (lambda: client.erase_sector(0), lambda: client.program_page(0, b"x"),
                      lambda: client.command(flasher.CMD_ERASE, 0, 0)):
            with pytest.raises(UnsafeOperation, match="read-only"):
                write()
        assert len(loader.callbacks) == before  # host guard: no write request reached the loader

        # Bypass the host guard and execute the real C payload with forged writes and bad reads.
        for cmd, a, n, data in ((flasher.CMD_ERASE, 0, 0, b""), (flasher.CMD_PROGRAM, 0, 1, b"x"),
                                (flasher.CMD_READ, 0x3FFFFF, 2, b""), (flasher.CMD_READ, 0x400000, 1, b"")):
            block = flasher.HEADER.pack(flasher.MAGIC, 1, cmd, a, n, flasher.PENDING, 0) + data
            base = ctypes.addressof(loader.sram) + flasher.SRAM.control - SRAM_BASE
            ctypes.memmove(base, block, len(block))
            library.model_call(base)
            assert flasher.HEADER.unpack(ctypes.string_at(base, flasher.HEADER.size))[5] != 0
        assert library.model_command_count(6) == 0  # no WREN, erase or program on the chip
        assert library.model_command_count(2) == 0 and library.model_command_count(0x20) == 0
        assert bytes(loader.nor) == original
        assert library.model_get_spr(0x8850) == 0  # unfamiliar manufacturer did not trigger SDK init


@pytest.mark.parametrize("jedec", [bytes.fromhex("ef4015"), bytes.fromhex("ab4017")])
def test_unfamiliar_chips_identify_without_alternate_initialization(library, jedec):
    loader = LoaderModel(library, jedec=jedec)
    with flasher.PayloadFlash(loader, CODE, spi_init="auto") as client:
        assert client.flash_id() == jedec and client.spi_path == "rom-state"
        assert client.info()["host_read_only"]
        if jedec[2] == 0x15:
            assert client.read(0, 1) == b"\xff"
        else:
            before = library.model_command_count(3)
            with pytest.raises(ApxError, match="ab4017"):
                client.read(0, 1)
            # Even a host which bypasses hello() cannot read unsupported geometry.
            with pytest.raises(TransferError):
                client.command(flasher.CMD_READ, 0, 1)
            assert library.model_command_count(3) == before
        with pytest.raises(UnsafeOperation):
            client.program_page(0, b"x")
        assert library.model_command_count(6) == 0 and library.model_get_spr(0x8850) == 0


def test_four_mib_backup_and_verify_against_real_payload(library, tmp_path):
    loader = LoaderModel(library, jedec=bytes.fromhex("ef4016"), ddr_alive=False)
    image = b"A" * CAPACITY + b"B" * (CAPACITY - 1) + b"C"
    ctypes.memmove(loader.nor, image, len(image))
    with flasher.PayloadFlash(loader, CODE, spi_init="rom") as client:
        output = tmp_path / "stock.bin"
        evidence = rom_rescue.action_dump(client, 1, output)
        assert output.read_bytes() == image
        result = rom_rescue.action_verify(client, 1, output.read_bytes())
        assert result["verified"] and result["actual_sha256"] == evidence["sha256"]
        assert evidence["capacity"] == 0x400000 and library.model_command_count(3) == 2 * 0x400000 // 512
        assert library.model_command_count(6) == 0 and library.model_command_count(2) == 0
        assert library.model_command_count(0x20) == 0


@pytest.mark.parametrize("jedec", [b"\0" * 3, b"\xff" * 3])
def test_invalid_bus_response_preserves_raw_id_in_error(library, jedec):
    loader = LoaderModel(library, jedec=jedec)
    with pytest.raises(TransferError, match=jedec.hex()):
        flasher.PayloadFlash(loader, CODE, spi_init="rom")
    assert library.model_command_count(3) == 0 and library.model_command_count(6) == 0


@pytest.mark.parametrize("region", [flasher.SRAM, flasher.DDR], ids=lambda r: r.name)
def test_id_read_program_erase_in_both_pools(library, region):
    loader = LoaderModel(library)
    expected = fill_nor(loader)
    with flasher.PayloadFlash(loader, CODE, region) as client:
        assert client.hello().size == CAPACITY and client.flash_id() == flasher.JEDEC
        assert client.read(0x1234, 0x9000) == expected[0x1234:0x1234 + 0x9000]
        client.erase_sector(0x4000)
        assert client.read(0x4000, 4096) == b"\xff" * 4096
        client.program_page(0x4100, bytes(range(256)))
        assert client.read(0x4100, 256) == bytes(range(256))
        with pytest.raises(ValueError):
            client.program_page(0x41FF, b"ab")  # crosses a page: refused before any traffic
        with pytest.raises(ValueError):
            client.read(CAPACITY - 1, 2)


def test_ddr_reads_use_the_large_window_sram_does_not(library):
    for region, expected_chunks in ((flasher.DDR, 3), (flasher.SRAM, 129)):
        loader = LoaderModel(library)
        fill_nor(loader)
        with flasher.PayloadFlash(loader, CODE, region) as client:
            before = len(loader.callbacks)
            client.read(0, 0x10001)
            reads = len(loader.callbacks) - before
        assert reads == expected_chunks, (region.name, reads)  # 0x8000 + 0x8000 + 1, or 128 x 512 + 1


def test_ddr_session_leaves_the_scratch_window_zero(library):
    loader = LoaderModel(library)
    fill_nor(loader)
    assert not any(loader.ddr_scratch())
    with flasher.PayloadFlash(loader, CODE, flasher.DDR) as client:
        client.read(0, 0x8000)
        assert any(loader.ddr_scratch())  # payload, request block and response are in there
    assert not any(loader.ddr_scratch())


def test_ddr_refuses_scratch_that_is_not_zero_and_leaves_it_alone(library):
    loader = LoaderModel(library)
    ctypes.memset(ctypes.addressof(loader.ddr) + 0x180100, 0x7F, 1)
    with pytest.raises(UnsafeOperation, match="not all zero"):
        flasher.PayloadFlash(loader, CODE, flasher.DDR)
    assert ctypes.string_at(ctypes.addressof(loader.ddr) + 0x180100, 1) == b"\x7f"  # someone else's data survives
    assert not loader.callbacks


def test_failed_upload_readback_still_restores_ddr(library):
    loader = LoaderModel(library)
    loader.corrupt_next_write = True
    with pytest.raises(TransferError, match="readback"):
        flasher.PayloadFlash(loader, CODE, flasher.DDR)
    assert not any(loader.ddr_scratch())


def test_watchdog_policy_per_pool(library):
    loader = LoaderModel(library)
    with flasher.PayloadFlash(loader, CODE, flasher.SRAM) as client:
        assert client.watchdog_off and library.model_wdt() == 0  # forced BootROM: disabled, host keeps it alive
    loader = LoaderModel(library)
    with flasher.PayloadFlash(loader, CODE, flasher.DDR) as client:
        assert not client.watchdog_off and library.model_wdt() != 0 and not client.notes  # stock loader reboots by itself


def test_payload_maintains_the_cache_for_ddr_requests_only(library):
    loader = LoaderModel(library)
    with flasher.PayloadFlash(loader, CODE, flasher.SRAM):
        assert library.model_sync_count() == 0
    loader = LoaderModel(library)
    with flasher.PayloadFlash(loader, CODE, flasher.DDR):
        assert library.model_sync_count() > 0


def test_unanswered_request_is_detected(library):
    loader = LoaderModel(library)
    with flasher.PayloadFlash(loader, CODE, flasher.DDR) as client:
        loader.skip_callback = True  # the payload does not run: the request is still pending
        with pytest.raises(TransferError, match="did not answer"):
            client.flash_id()


def test_sector_preserving_write_and_full_replace(library, tmp_path):
    loader = LoaderModel(library)
    original = fill_nor(loader)
    patch = b"PATCH" * 300
    with flasher.PayloadFlash(loader, CODE, flasher.DDR) as client:
        backup = write_file(client, address=0x3F80, input_path=_file(tmp_path, "patch.bin", patch),
                            backup_path=tmp_path / "backup.bin", confirmed=True)
        assert backup.sha256 == hashlib.sha256(original).hexdigest()
    expected = bytearray(original)
    expected[0x3F80:0x3F80 + len(patch)] = patch
    assert bytes(loader.nor) == bytes(expected)

    image = bytearray(expected)
    for sector in (0, 7, 300):
        image[sector * 4096:sector * 4096 + 4096] = bytes((sector + i) & 0xFF for i in range(4096))
    with flasher.PayloadFlash(loader, CODE, flasher.DDR) as client:
        state = client.replace(bytes(image), tmp_path / "pre.bin", expected_sha256=hashlib.sha256(expected).hexdigest())
    assert state["status"] == "verified" and bytes(loader.nor) == bytes(image) and state["boot_sector_committed"]


def test_cli_flash_commands_run_on_the_ddr_pool(library, tmp_path, capsys):
    loader = LoaderModel(library)
    data = fill_nor(loader)
    payload = _file(tmp_path, "flasher.bin", CODE)

    def run(command, **extra):
        args = SimpleNamespace(timeout=5, payload=payload, region="ddr", flash_command=command, **extra)
        return cli._run_flash(loader, args, "/dev/sgmodel")

    assert run("id") == 0 and "5E4015" in capsys.readouterr().out
    out = tmp_path / "read.bin"
    assert run("read", address=0x100, length=0x9000, output=out) == 0 and out.read_bytes() == data[0x100:0x9100]
    assert run("verify", address=0x100, input=out) == 0 and "verified" in capsys.readouterr().out
    assert not any(loader.ddr_scratch())
    with pytest.raises(ApxError, match="not built"):
        cli._run_flash(loader, SimpleNamespace(timeout=5, payload=tmp_path / "missing", region="ddr", flash_command="id"), "x")


def test_cli_parser_exposes_only_the_supported_flash_commands(capsys):
    parser = cli._parser()
    args = parser.parse_args(["flash", "id", "--region", "sram"])
    assert args.region == "sram" and args.flash_command == "id"
    for removed in (["flash", "hello"], ["flash", "stage-probe"], ["hw", "inspect"], ["payload", "marker"]):
        with pytest.raises(SystemExit):
            parser.parse_args(removed)


def test_built_payload_fits_both_pools():
    built = SDK / "build" / "flasher" / "flasher.bin"
    if not built.is_file():
        pytest.skip("flasher payload not built (make -C appotech-sdk/boot)")
    code = built.read_bytes()
    assert 0 < len(code) <= min(flasher.SRAM.code_limit, flasher.DDR.code_limit)


def _file(directory, name, data):
    path = directory / name
    path.write_bytes(data)
    return path
