"""Decode the SDK cold-boot flight recorder (appotech-sdk/include/appotech/trace.h)."""
from __future__ import annotations

import struct

SECTOR = 0x1FF000
SECTOR_SIZE = 4096
REGS_OFFSET = 0x100
CRUMBS_OFFSET = 0x800
SNAPSHOTS = 4
ENTRIES = 32

CRUMB_NAMES = {
    0x01: "SPI entry reached (arg: entry stack pointer)",
    0x02: "saved ROM stack page valid (arg: saved stack pointer)",
    0x03: "NOLOAD buffers cleared",
    0x04: "incoming clock profile accepted",
    0x05: "runtime ready (arg: watchdog control)",
    0x06: "clock profile applied",
    0x07: "SHUTTER recovery window passed",
    0x08: "main() entered",
    0x09: "recovery window: buttons/ADC initialised",
    0x0A: "recovery window: first poll done (arg: ADC reading, 0xffff = none)",
    0x0B: "recovery window: 4096 polls done (arg: tick counter)",
    0x0C: "recovery window: SHUTTER seen (arg: button state)",
    0x0D: "apx_bootrom() entered while SPI-booted",
    0x18: "USB: first apx_usb_poll() (arg: ticks, 32768 = 1 s)",
    0x19: "USB: first control request from the host handled (arg: ticks)",
    0x1D: "USB: host set the configuration, enumeration complete (arg: ticks)",
    0x1E: "caches after SDK start (arg: data-cache ctl << 12 | instruction-cache ctl; bit 0 = enabled)",
    0x1A: "PM chip regs 0x40<<16|0x44<<8|0x58 before power-domain init",
    0x1B: "PM chip regs after power-domain init (bit 23: init failed)",
    0x1C: "PM chip reg 0x5a before<<8 | after (0x08 = main supply keep-alive)",
    0x50: "ADC probe after PM init, 2nd conversion (ch9<<12 | ch11)",
    0x58: "ADC probe after PM init, 1st conversion (ch9<<12 | ch11)",
    0x51: "ADC probe after USB init, 2nd conversion (ch9<<12 | ch11)",
    0x59: "ADC probe after USB init, 1st conversion (ch9<<12 | ch11)",
    0x52: "ADC probe after buttons init, 2nd conversion (ch9<<12 | ch11)",
    0x5A: "ADC probe after buttons init, 1st conversion (ch9<<12 | ch11)",
    0x53: "ADC probe after display init, 2nd conversion (ch9<<12 | ch11)",
    0x5B: "ADC probe after display init, 1st conversion (ch9<<12 | ch11)",
    0x54: "ADC probe after battery/SPI init, 2nd conversion (ch9<<12 | ch11)",
    0x5C: "ADC probe after battery/SPI init, 1st conversion (ch9<<12 | ch11)",
    0x55: "ADC probe after audio+mic init, 2nd conversion (ch9<<12 | ch11)",
    0x5D: "ADC probe after audio+mic init, 1st conversion (ch9<<12 | ch11)",
    0x56: "ADC probe after camera init, 2nd conversion (ch9<<12 | ch11)",
    0x5E: "ADC probe after camera init, 1st conversion (ch9<<12 | ch11)",
    0x70: "clock init entered (arg bit 20 set; bit 4 = 96 MHz pair, bit 0 = 144 MHz pair)",
    0x71: "clock init: 96 MHz baseline measurement finished (arg: ticks)",
    0x72: "clock init: PLL1 switch returned (arg: PLL1 control)",
    0x40: "lvgl-demo: main entered",
    0x41: "lvgl-demo: USB initialised",
    0x42: "lvgl-demo: SPI overlay load returned (arg: apx_spi_error, 0 = ok)",
    0x43: "lvgl-demo: LVGL port initialised (arg: result)",
    0x44: "lvgl-demo: demo created (arg: result)",
    0x45: "lvgl-demo: first port poll returned (arg: result)",
    0x74: "timer control SPR 0x9800 right after the SPI-boot normalization (expected 0x721)",
    0x75: "timer control SPR 0x9800 just before clock init (expected 0x721)",
    0x0E: "clock: 1M-iteration loop at 96 MHz, 32.768 kHz ticks (RAM launch ~2389)",
    0x0F: "clock: same loop at the SDK clock (RAM launch ~1593)",
    0x10: "app: USB initialised (arg: ticks)",
    0x11: "app: buttons initialised",
    0x12: "app: display initialised",
    0x13: "app: battery/SPI initialised (arg: battery<<12 | spi)",
    0x14: "app: audio files initialised",
    0x15: "app: microphone files initialised",
    0x16: "app: camera files initialised",
    0x17: "app: main loop reached (arg: ticks)",
    0x2F: "battery init: ADC control<<12 | 8840>>20",
    0x20: "camera: sensor powered and clocked",
    0x21: "camera: sensor probe finished (arg: error, 7 = I2C NACK)",
    **{0x30 + n: f"battery sample pair {n} (arg: ch9<<12 | ch11)" for n in range(16)},
    0x80: "FAIL: saved ROM stack page invalid (arg: stack pointer)",
    0x81: "FAIL: incoming clock state refused (arg: reason<<16 | SPR 0x9800; 1 PLL pair, 2 timer ctl, 3 period, 4 write never read back)",
    0x82: "FAIL: clock init error (arg: error)",
    0x83: "handoff to BootROM loader (arg: failure id)",
}
FAILURES = {0x80, 0x81, 0x82}

SPR_NAMES = {
    0x0018: "vendor IRQ/status", 0x880C: "watchdog", 0x8848: "clock gate/ctl", 0x884C: "clock ctl",
    0x8850: "clock select", 0x8858: "PLL1 control", 0x885C: "PLL1 coefficient", 0x8860: "PLL2 control",
    0x8864: "PLL2 coefficient", 0x8868: "DDR/PLL calibration", 0x8340: "pin mux", 0x8100: "clock gate",
    0x8040: "clock gate", 0x88E0: "power-bus control", 0x9800: "timer control", 0x9804: "timer count",
    0x9808: "timer period", 0x9840: "interrupt ctl", 0x9870: "interrupt ctl", 0x9080: "UART ctl",
    0x908C: "UART status", 0x9000: "SPI control", 0x924C: "SPI clock", 0xA004: "USB ctl",
    0x4800: "misc ctl", 0xD804: "misc ctl", 0x9244: "SPI command", 0x9248: "SPI status",
}


def _pll_profile(registers: dict[int, int]) -> str | None:
    pair = (registers.get(0x8858), registers.get(0x885C))
    if pair == (0x1123, 0x112A8800):
        return "144 MHz profile"
    if pair == (0x1623, 0x0B71B000):
        return "96 MHz profile"
    if None in pair:
        return None
    return f"unrecognised PLL1 pair {pair[0]:#x}/{pair[1]:#x}"


def decode(sector: bytes) -> dict:
    if len(sector) != SECTOR_SIZE:
        raise ValueError("trace sector must be exactly 4096 bytes")
    result: dict = {"valid": False, "snapshots": [], "crumbs": [], "verdict": ""}
    if sector[:4] != b"APXT":
        result["verdict"] = (
            "no trace header: erased sector means a trace build never reached its entry code"
            if sector == b"\xff" * SECTOR_SIZE
            else "sector holds something other than a trace"
        )
        return result
    result["valid"] = True
    result["version"] = sector[4]
    result["build_id"] = sector[8:24].rstrip(b"\xff\x00").decode("ascii", "replace")
    for n in range(SNAPSHOTS):
        page = sector[REGS_OFFSET * (n + 1):REGS_OFFSET * (n + 1) + 256]
        registers = {}
        for i in range(ENTRIES):
            spr, value = struct.unpack_from("<II", page, 8 * i)
            if spr != 0xFFFFFFFF:
                registers[spr] = value
        if registers:
            result["snapshots"].append({"index": n, "registers": registers, "pll": _pll_profile(registers)})
    for ident in range(1, 255):
        slot = sector[CRUMBS_OFFSET + 4 * ident:CRUMBS_OFFSET + 4 * ident + 4]
        if slot == b"\xff\xff\xff\xff":
            continue
        arg = slot[1] | slot[2] << 8 | slot[3] << 16
        name = CRUMB_NAMES.get(ident, f"id {ident:#04x}")
        if 0xA0 <= ident <= 0xBF:  # raw 32.768 kHz timer when crumb i of the crashed run was recorded
            name = f"previous run, crumb {ident - 0xA0}: timer"
        if ident == 0xE0:
            name = "previous run: progress of the last heartbeat (measurement-loop iteration)"
        if ident == 0xE1:
            name = "previous run: raw timer at the last heartbeat"
        if 0x90 <= ident <= 0x99:
            name = {0x91: 'USB bus event (arg: INTRUSB<<16 | POWER<<8 | INTRTX&ff)', 0x92: 'USB setup packet (arg: dir|request|wValue)', 0x93: 'USB SET_ADDRESS accepted (arg: address)', 0x94: 'USB address applied to FADDR (arg: address)', 0x95: 'USB configured (arg: value)', 0x99: 'USB enumeration timeout, resetting'}.get(ident, 'USB event')
        if 0xC0 <= ident <= 0xDF:  # crumb imported from the SRAM ring of a run that crashed
            stage = arg >> 16
            name = f"previous run, crumb {ident - 0xC0}: stage {stage:#04x} ({CRUMB_NAMES.get(stage, 'unnamed')}), low 16 bits of arg"
            arg &= 0xFFFF
        result["crumbs"].append({"id": slot[0], "slot": ident, "arg": arg, "name": name, "consistent": slot[0] == ident})
    crumbs = result["crumbs"]
    failures = [c for c in crumbs if c["slot"] in FAILURES]
    if failures:
        result["verdict"] = f"failed: {failures[0]['name']}"
    elif not crumbs:
        result["verdict"] = "header written but no breadcrumb: stalled immediately after trace start"
    else:
        last = max(crumbs, key=lambda c: c["slot"] if c["slot"] < 0x80 else -1)
        result["verdict"] = f"last stage reached: {last['name']} (no failure recorded; next stage hung or reset)"
    return result


def format_report(result: dict) -> str:
    lines = []
    if not result["valid"]:
        return result["verdict"]
    lines.append(f"trace v{result['version']} build {result['build_id']}")
    for crumb in result["crumbs"]:
        flag = "" if crumb["consistent"] else "  (slot/id mismatch)"
        lines.append(f"  [{crumb['slot']:#04x}] {crumb['name']}  arg={crumb['arg']:#08x}{flag}")
    for snap in result["snapshots"]:
        label = ("at entry", "(unused)", "after USB init", "at failure/before ROM handoff")[snap["index"]]
        lines.append(f"registers {label}" + (f" - {snap['pll']}" if snap["pll"] else ""))
        for spr, value in snap["registers"].items():
            lines.append(f"  {spr:#06x} {value:#010x}  {SPR_NAMES.get(spr, '')}")
    lines.append("verdict: " + result["verdict"])
    return "\n".join(lines)


# -- production boot marks: src/boot_marks.c keeps 31 (id << 24 | arg, 32 kHz timer) pairs at SRAM 0x2f00 ----
MARKS_ADDRESS = 0x2F00
MARKS_BYTES = 256
MARKS_MAGIC = 0x4B52414D


def decode_marks(data: bytes) -> dict:
    if len(data) < MARKS_BYTES:
        raise ValueError("boot marks need 256 bytes")
    magic, count = struct.unpack_from("<II", data)
    if magic != MARKS_MAGIC:
        return {"valid": False, "marks": [], "magic": magic}
    marks = []
    for i in range(min(count, 31)):
        word, timer = struct.unpack_from("<II", data, 8 + 8 * i)
        ident, arg = word >> 24, word & 0xFFFFFF
        marks.append({"id": ident, "arg": arg, "timer": timer & 0x3FFF, "name": CRUMB_NAMES.get(ident, f"id {ident:#04x}")})
    return {"valid": True, "marks": marks, "magic": magic}


def format_marks(result: dict) -> str:
    if not result["valid"]:
        return f"no boot marks in SRAM (magic {result['magic']:#010x}): this image does not record them, or SRAM was cleared"
    lines = [f"boot marks, {len(result['marks'])} recorded (timer = raw 32.768 kHz counter, 14 bits):"]
    for m in result["marks"]:
        lines.append(f"  [{m['id']:#04x}] {m['name']}  arg={m['arg']:#08x}  t={m['timer']:#06x}")
    if result["marks"]:
        lines.append(f"last mark: {result['marks'][-1]['name']}")
    return "\n".join(lines)
