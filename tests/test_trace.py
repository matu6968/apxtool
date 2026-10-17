import struct

import pytest

from apxtool import cli, trace


def sector(crumbs=(), snapshots=(), build=b"deadbeef12345678"):
    data = bytearray(b"\xff" * 4096)
    data[0:8] = b"APXT\x01\x00\x00\x00"
    data[8:24] = build
    for n, regs in enumerate(snapshots):
        page = bytearray(b"\xff" * 256)
        for i, (spr, value) in enumerate(regs.items()):
            struct.pack_into("<II", page, 8 * i, spr, value)
        data[0x100 * (n + 1):0x100 * (n + 1) + 256] = page
    for ident, arg in crumbs:
        data[0x800 + 4 * ident:0x800 + 4 * ident + 4] = bytes([ident, arg & 255, arg >> 8 & 255, arg >> 16 & 255])
    return bytes(data)


def test_erased_and_foreign_sectors_are_explained():
    assert "never reached" in trace.decode(b"\xff" * 4096)["verdict"]
    assert "other than a trace" in trace.decode(b"\x00" * 4096)["verdict"]
    with pytest.raises(ValueError):
        trace.decode(b"short")


def test_last_stage_and_hang_verdict():
    result = trace.decode(sector([(1, 0x4EF0), (2, 0x4E00), (3, 0), (4, 0), (5, 0x10)]))
    assert result["valid"] and result["build_id"] == "deadbeef12345678"
    assert [c["slot"] for c in result["crumbs"]] == [1, 2, 3, 4, 5]
    assert result["crumbs"][0]["arg"] == 0x4EF0
    assert "runtime ready" in result["verdict"] and "hung or reset" in result["verdict"]


def test_failure_wins_and_reports_pll():
    result = trace.decode(sector(
        [(1, 0), (0x81, 0), (0x83, 0x81)],
        [{0x8858: 0x1123, 0x885C: 0x112A8800}, {}, {0x8858: 0x1623, 0x885C: 0x0B71B000}]))
    assert result["verdict"].startswith("failed: FAIL: incoming clock state refused")
    assert [s["pll"] for s in result["snapshots"]] == ["144 MHz profile", "96 MHz profile"]
    text = trace.format_report(result)
    assert "registers after USB init - 96 MHz profile" in text and "PLL1 control" in text


def test_header_without_crumbs():
    assert "stalled immediately" in trace.decode(sector())["verdict"]


def test_cli_decodes_sector_and_full_image(tmp_path, capsys):
    raw = sector([(1, 7)])
    path = tmp_path / "sector.bin"
    path.write_bytes(raw)
    assert cli.main(["trace", str(path)]) == 0
    assert "SPI entry reached" in capsys.readouterr().out
    image = bytearray(b"\xff" * (2 * 1024 * 1024))
    image[trace.SECTOR:trace.SECTOR + 4096] = raw
    full = tmp_path / "full.bin"
    full.write_bytes(image)
    assert cli.main(["trace", str(full)]) == 0
    assert "SPI entry reached" in capsys.readouterr().out


def test_boot_marks_decode_and_format():
    import struct
    data=bytearray(256);struct.pack_into("<II",data,0,trace.MARKS_MAGIC,2)
    struct.pack_into("<II",data,8,0x01<<24|0x3ffffc,0x1234);struct.pack_into("<II",data,16,0x82<<24|1,0x3fff)
    result=trace.decode_marks(bytes(data))
    assert result["valid"] and [m["id"] for m in result["marks"]]==[1,0x82]
    assert result["marks"][1]["arg"]==1 and result["marks"][1]["timer"]==0x3fff
    text=trace.format_marks(result)
    assert "clock init error" in text and "last mark" in text
    assert not trace.decode_marks(bytes(256))["valid"] and "no boot marks" in trace.format_marks(trace.decode_marks(bytes(256)))
    struct.pack_into("<I",data,4,999);assert len(trace.decode_marks(bytes(data))["marks"])==31
    with pytest.raises(ValueError):trace.decode_marks(b"short")
