# JT5295 loader clock follow-up — 2026-10-13

> **Historical record.** This report describes bring-up work done with the `apxtool hw` / `payload` commands, the
> `firmware/` payloads and the vendor-callback (APXS) flasher. Those were removed when the flashers were unified onto
> `appotech-sdk/boot/flasher.c` (see the [README](../README.md#flashing)); the measurements and conclusions stand,
> but the commands shown here no longer exist in this tree.

**Later testing:** the owner subsequently authorized exploratory writes with
hard-reset recovery. Following one temporary success and an unaligned failure,
an aligned stock-pair payload passed two 30-second loader sessions, including
200 callbacks, ten guarded RAM restorations and verified rollback. See
[aligned acceptance](clock-aligned-validation.md).
The no-write findings below describe the earlier evidence-gated phase.

**During this earlier phase, stable operation was unverified and no new
clock-write trial was issued.** The evidence gates had not passed:
the captured end states identify the required PLL pair, but not a supported
live source handoff, memory-clock relationship or rollback. Both historical
trials remain disabled at the CLI and Python RPC/API entry points.

## Fresh measurements

All successful profiles used separate, fresh response slots for each SPR and
verified scratch restoration. They are sequential snapshots, not simultaneous
captures of the entire clock tree.

| SPR | Stock MSC | Loader | Observation |
|---|---|---|---|
| `0x8848` | `0x01014003` | `0x01014003` | Same sampled routing/control fields |
| `0x884c` | `0x0b000b01` | `0x80000001` | Different shared clock fields; loader bit 31 is set |
| `0x8858` | `0x00001123` | `0x00001623` | PLL1 control field `[10:8]`: 1 versus 6 |
| `0x885c` | `0x112a8800` | `0x0b71b000` | Supported 144/96 MHz PLL estimates |
| `0x8860` | `0x00001623` | `0x00001623` | Same PLL2 control |
| `0x8864` | `0x0bebc200` | `0x0bebc200` | Same estimated 100 MHz PLL2 coefficient |
| `0x8868` | `0x044df82e` | `0x044df82e` | Same sampled divider/configuration fields |
| `0x9800` | `0x00003809` | `0x00000721` | Different timer profiles |
| `0x9808` | `0x00002bf1` | `0x00003fff` | Different reload periods |
| `0x9840` | `0x300170c5` | `0x300170c5` | Same USB PHY control candidate |
| `0xf860` | `0x00000007` | `0x00000007` | Same memory initialization status candidate |
| `0xf818` | `0x2bab2b05` | `0x2bab2b05` | Same final sampled memory timing/status candidate; an earlier normal sample differed |
| `0xf81c` | `0x00000000` | `0x00000000` | Same memory calibration candidate |

Raw profiles: normal MSC
and loader.
The PLL values retain their previous confidence: estimates, not verified CPU Hz.
Matching memory and PHY values do not establish their input-clock relationships
or prove that they tolerate intermediate PLL states.

The earlier description of a `0x500` control difference was the **arithmetic
difference**, not a mask: `0x1623 - 0x1123 = 0x500`, but
`0x1623 XOR 0x1123 = 0x700`. All three bits in field `[10:8]` differ. Its analog
meaning and live-update behavior remain unresolved; no charge-pump, divider or
frequency-band interpretation is asserted.

## Raw vendor trace and address mapping

The evidence ledger records
image hashes, profile hashes, register comparisons, validated probe encodings
and hashes of each disassembly window. Listings use little-endian input words
converted for the big-endian OpenRISC binutils decoder.

BootROM addresses are based at `0x00100000`. Bootstrap listings use **SPI file
offsets**; their actual execution mapping is not established here. The Ghidra
SPI base `0x01ffde00` correctly maps the application's offset `0x2200` to runtime
`0x02000000`, but must not be used to claim bootstrap execution addresses.
Raw control flow uses the measured skipped instruction after taken jumps.
Call-link behavior has not been independently measured; Ghidra's existing
delay-slot and return-address assumptions are not treated as evidence.

### PLL control and routing

- SPI offsets `0x15f4..0x1618` construct coefficient `0x112a8800` and control
  `0x1123` in the low-frequency-reference branch. The alternative branch uses
  another coefficient and sets control bit 6. PLL2 is subsequently programmed
  separately at offsets `0x163c..0x1654`.
- Offsets `0x1658..0x166c` update `0x884c` as
  `(old & 0xffffff00) | 1`, preserving high fields. Offsets `0x1670..0x1680`
  set low routing bits of `0x8848` and invoke a delay. Memory initialization and
  calibration follow. This is an initialization sequence, not proof of a live
  transition with USB already active.
- BootROM helper `0x001044d8` writes the coefficient in two parts, ORs control
  bits 1 and 0, applies `(old & ~0x1cc) | table_control`, temporarily clears
  routing bits in `0x8848`, later sets control bit 5, and writes the **entire**
  `0x884c` from table byte 6. Its waits are fixed loops containing writes to
  `0x880c`; they are not demonstrated PLL-lock polls.
- That mask leaves bits 9 and 10 untouched. Applying table control `0x0100`
  to loader `0x1623` would produce `0x1723`, not stock `0x1123`.
  The helper cannot directly perform the desired 6 → 1 field replacement.
  The coefficient-only trial also retained the loader control value. Neither
  historical implementation establishes the stock paired configuration.

### USB ownership of `0x884c` bit 31

Loader entry `0x0010385c` performs reference/PLL setup, then calls
`0x00105d20` at `0x001038a8`. The latter reads `0x884c`, ORs `0x80000000`, and
writes it at `0x00105d3c` before a USB PHY sequence at `0x9840`, USB interrupt
setup and controller accesses at `0xd800`. This establishes that bit 31 belongs
to the loader USB initialization dependency; its exact electrical function is
still unknown. The first failed trial's BootROM helper clears that bit through
its whole-register write. This is a concrete incompatibility, not proof of the
failure's precise cause.

Other application code changes distinct fields in `0x884c`: routines around
`0x020279d0/0x02027f90` set bits `[27:24]` to `0xb`, while `0x0202af8c`
computes another peripheral divider in bits `[12:8]`. These references confirm
that `0x884c` is shared. Copying the complete normal-mode word into the loader
is not a supported fix.

### Memory, interrupts and polling

Bootstrap offsets `0x180c..0x1e00` include memory-controller configuration,
status-dependent timing adjustments and multiple initialization cases in the
`0xf8xx` block. At offset `0x1a84` the vendor writes an initialization command
to `0xf800`; the following loop reads **`0xf860`**, with its address established
at `0x1a2c`. The first follow-up normal profile mistakenly annotated that poll
as `0xf800`. Its raw read is retained in
the historical capture,
but the status attribution is withdrawn. The corrected v2 capture and source
validator use `0xf860`. The first capture's `0xf81c` source annotation is also
superseded: its validated read is offset `0x11c0`, not the preceding call site.

Vendor IRQ paths read SPR `0x0018` and clear/set bit 1, with runtime startup also
handling bit 2. These are chip-specific observations, not standard OpenRISC SR
bit definitions. This follow-up did not probe or write that register. Arbitrary
IRQ masking cannot be added on the assumption that standard SR conventions apply.

The timer reference checks at BootROM `0x0010465c` and SPI offset `0x0a30`
explicitly program `0x9800/0x9808/0x9804` before calibration. This reinforces why
normal-mode reload counts cannot be interpreted using the loader timer profile.
No timer configuration was changed by the diagnostics.

## Implementation and gates

`hw clock-profile` adds comparable isolated register captures in either mode.
`--include-dependencies` adds only four vendor-reviewed USB/memory reads;
earlier failed CPUCFGR, SR and clock-gate probes are excluded. The instruction
checker now verifies that host and assembly SPR whitelists agree, in addition
to byte order, preserved callback registers, request-only stores, inert branch
slots and absence of clock/cache/timer writes.

Clock writes remain blocked before device opening/loader entry by the CLI and
before dispatch by the Python API/RPC. Matching profiles cannot open the gates.
Cleanup also refuses to erase diagnostic RAM when clock restoration is
explicitly unverified, preserving recovery code and reporting a required power
cycle instead of claiming successful restoration.

No replacement transition payload was generated because these dependencies
remain unsupported: the live PLL control/source handoff, USB source/divider
relationship, memory-clock and retiming relationship, and an ordered live
rollback. Consequently there was no temporary 144 MHz trial, 30-second hold,
100-callback acceptance run or repeated boosted loader session. These checks
remain required before claiming a stable higher-clock loader.

## Reproduction and integrity

Run from the workspace root; use new output names:

```sh
make -C apxtool/firmware check-hw
PYTHONPATH=apxtool/src python3 -m apxtool --device /dev/sg0 hw clock-profile --normal-mode --include-dependencies --experimental --yes --json normal-profile-new.json
PYTHONPATH=apxtool/src python3 -m apxtool --device /dev/sg0 hw clock-profile --start-loader --include-dependencies --experimental --yes --json loader-profile-new.json
PYTHONPATH=apxtool/src python3 apxtool/tools/trace_clock.py --spi apxtool/research/clock-followup-baseline-spi.bin --rom apxtool/research/jt5295-rom-00100000-64k.bin --normal-profile normal-profile-new.json --loader-profile loader-profile-new.json --output-dir clock-trace-new
```

A SPI handshake immediately after switching from APXH to APXS in the same
loader session returned an invalid zero capability marker. That session stopped
and cleanup returned without exception. A subsequent INQUIRY confirmed stock
MSC identity. Cache visibility/payload switching is an unresolved hypothesis,
not a demonstrated cause. A fresh SPI-only loader session then read the full
chip successfully. See the failure record.
Do not treat successful RAM readback as proof of executable instruction visibility.

The exact 1308-byte second failed trial binary has been
archived, SHA-256
`1434bb7ac7f85ee93435996124e7b84c651ae56b04087a1a4bf8484d9aee866a`.
The first trial journal retains its hash; its original binary is not available.
Neither historical trial is enabled by the current tools.

Full SPI captures and the final acceptance state are recorded in
the follow-up integrity record.
No SPI erase/program or hardware clock-setting operation was issued in this
follow-up. Host tests exercise isolated capture failures, profile comparison,
disabled clock entry points, unknown-clock cleanup, and raw source/byte-order
validation; they do not simulate a successful silicon transition.

The initial generated trace directory omitted the ROM windows because their
file offsets were treated as absolute addresses. It is superseded by the v2
ledger above; current tooling rejects empty or incorrectly based listings, and
an integration test checks every requested start address. The findings above
also agree with the independently decoded raw ROM instructions.
