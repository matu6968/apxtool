# JT5295 clock modes and loader transition failures

> **Historical record.** This report describes bring-up work done with the `apxtool hw` / `payload` commands, the
> `firmware/` payloads and the vendor-callback (APXS) flasher. Those were removed when the flashers were unified onto
> `appotech-sdk/boot/flasher.c` (see the [README](../README.md#flashing)); the measurements and conclusions stand,
> but the commands shown here no longer exist in this tree.

The latest [aligned stock-pair tests](clock-aligned-validation.md) passed two
30-second loader sessions with 200 timed callbacks, ten guarded RAM restorations,
a 1.4997× loop speedup, verified clock/scratch rollback and unchanged full SPI.
Approximately 144 MHz remains inferred. Earlier failures below are historical
evidence; the retained `hw clock-trial` command remains disabled.

Measured on 2026-10-12. The stock firmware in USB MSC mode supports an
**approximately 144 MHz CPU clock estimate**, compared with **96 MHz in the
loader**. Both estimates combine firmware constants, live PLL register values
and loop measurements. Exact PLL-to-CPU routing remains inferred, so JSON keeps
`cpu_hz` null and records the estimate separately. Live preview, recording and
other camera modes have not been measured.

## Mode comparison

| Measurement | Loader | Stock USB MSC |
|---|---|---|
| PLL1 control, SPR `0x8858` | `0x00001623` | `0x00001123` |
| PLL1 coefficient, SPR `0x885c` | `0x0b71b000` | `0x112a8800` |
| Clock fields, SPR `0x8868` | `0x044df82e` | `0x044df82e` |
| Inferred Q16.16 coefficient × 32,768 Hz | 96,000,000 Hz | 144,000,000 Hz |
| Same four-instruction loop throughput | About 13.70 million iterations/s | 20.23 million before trials; 20.07 million after recovery |

The normal-mode loop runs about 1.46–1.48 times as fast, consistent with the
1.5 PLL coefficient ratio and stock background activity. USB timing measures
loop throughput; it is not an independent CPU cycle count. The crystal marking
`32.768` continues to support a **kHz** interpretation through the coefficient
and loader-counter evidence in the [hardware report](hardware-report.md).

The normal timer reload at SPR `0x9808` is `0x2bf1` (11,249), unlike the loader's
`0x3fff`. Its counter can reload repeatedly during a long loop. The
`candidate_timer_hz_observed` field in the original
normal-mode clock capture is therefore
invalid and must not be used. Raw counter samples are retained as evidence.
Current software only calculates a candidate timer rate for the known loader
calibration profile (`0x9800 = 0x721`, reload at least `0x3fff`); other profiles
retain raw samples without a rate estimate.

## Normal-mode RAM execution

Normal-mode `0xcd` now has a verified transfer and callback path. Previous
failed probes used different function addresses. The validated 16-byte CDB is:

| Bytes | Meaning |
|---|---|
| 0 | `0xcd` |
| 1–4 | Primary function `0x0203bdf4`, little-endian |
| 5–8 | RAM address, little-endian |
| 9–12 | Callback address, little-endian; `0xffffffff` disables it |
| 13–15 | `0xff` padding |

Raw dispatch analysis at `0x0203c360/0x0203c574`, registered handler
`0x0203be30`, and CBW parser `0x0203c884` establish the argument layout.
Function `0x0203bdf4` selects the input/output helpers `0x0203bcfc/0x0203bd74`.
Live checks returned the vendor prefix, a full 4 MiB snapshot, uploaded payload
readback, valid assembly callback results and verified scratch restoration.
Taken jumps also skip the following instruction in this mode.

This provides a tested route for small callbacks at the stock higher clock.
Stock tasks continue running, so zero-filled scratch is not a permanent memory
reservation or an allocation guarantee for a port.

From the workspace root, with the device already in stock MSC mode:

```sh
PYTHONPATH=apxtool/src python3 -m apxtool --device /dev/sg0 hw inspect --normal-mode --json normal-inspect-new.json
PYTHONPATH=apxtool/src python3 -m apxtool --device /dev/sg0 hw clock-test --normal-mode --experimental --yes --json normal-clock-new.json
```

These commands do not enter the loader or write clock settings. Clock-test
uploads a separate stackless diagnostic to preflighted scratch, uses fresh
response slots and restores saved bytes. `--normal-mode` is available only for
inspection and clock measurement, and cannot be combined with `--start-loader`.

## Loader boost trials: unsuccessful

Two temporary 96 → 144 → 96 MHz transition implementations were tried. Neither
returned a valid completed result; neither demonstrated a working 144 MHz
loader or verified clock rollback.

1. The BootROM-routine trial called the
   retained, byte-verified PLL setup at `0x001044d8` with coefficients for
   144 MHz and then 96 MHz. That routine also changes shared oscillator routing
   (`0x8848/0x884c`) and PLL control. SCSI returned host status `0x07`. RAM and
   callback-stack restoration was verified, but clock restoration was not. The
   camera subsequently re-enumerated in stock firmware.
2. The coefficient-only trial
   omitted ROM calls, stack use and shared routing changes. It wrote `0x885c`
   and the vendor sequence's enable/update bits at `0x8858`, intending to sample
   loops and restore the baseline before returning. SCSI again returned host
   status `0x07`; the device disappeared. A passive response read and bounded
   RAM restoration failed with `ENODEV`. The owner power-cycled the camera.

There is no trustworthy partial stage/result from the second trial. Its hang
location and root cause are unresolved. The control settings differ by `0x500`
between the stable modes; the meanings of those fields and dependencies on
reference selection, PLL lock, source handoff and peripheral clocks need further
analysis. Copying the coefficient alone has not established a working sequence.

`hw clock-trial` is **disabled before device opening, loader entry or upload**.
Research code is retained to explain the attempts, not as a supported clock
setting command. No further clock-write trial ran after the second failure.
Fake transport tests validate guards and failure handling, not silicon behavior.

The [2026-10-13 follow-up](loader-clock-transition.md) adds fresh paired
USB/memory profiles, identifies the BootROM helper's control-mask and USB
routing incompatibilities, and records the evidence gates that still prevent a
new live transition. The `0x500` arithmetic control difference has XOR mask
`0x700`; field `[10:8]` changes from 6 to 1. No further clock write was issued.

## Comparison with the Action 104 RAM function

The [Action 104 clock implementation](https://github.com/atc1441/Action_104_Games_and_Pacman_DOOM/blob/main/firmware/sdk/clock.c)
marks only `mpi_retune_for_pll()` with
`__attribute__((section(".ramfunc"), noinline, noclone))`. That function keeps the
external-flash interface divider at 2 and sets receive-control bit `0x100` before
the PLL source switch. Its [linker script](https://github.com/atc1441/Action_104_Games_and_Pacman_DOOM/blob/main/firmware/link_doom.ld)
places `.ramfunc` in SRAM with a flash load address; the
[reset handler](https://github.com/atc1441/Action_104_Games_and_Pacman_DOOM/blob/main/firmware/sdk/startup.c)
copies it before initialization. The remaining oscillator, lock, source and
divider sequence runs from flash. The code also masks interrupts across the
transition. These are observations of that project's implementation, not a
register-map match for the JT5295.

The JT5295 coefficient-only trial already had the relevant execution placement:
the generated ELF has one executable `.text` section at `0x02180000`, size
`0x51c`; `clock_trial` is at `0x02180368`. Its disassembly has no `l.jal` or
`l.jalr`, and the routine does not use a stack. Its callback, loops and intended
rollback are uploaded to RAM together. The first trial called the BootROM PLL
routine instead. A `.ramfunc` annotation by itself would therefore not move the
second trial away from SPI instruction fetching any further.

RAM execution does not establish that this RAM is independent of the changed
PLL. The JT5295 RAM type/controller and clock relationship remain unresolved;
if its memory clock or timing changes, a RAM routine and response marker could
both become inaccessible. Interrupt code was not explicitly masked by the
coefficient-only routine, and loader USB operation also needs a valid peripheral
clock. The useful follow-up is to trace the vendor's complete stable transition,
including memory timing, reference/source handoff, lock indications and
peripheral clocks. Those are candidate dependencies, not established causes of
either failure. No additional hardware clock trial was made for this comparison.

## Recovery and flash integrity

After the owner power cycle, the camera returned to stock `Buildwin Media-Player`
MSC identity. A fresh full-chip read was identical to the pre-trial 2 MiB read:

```text
SHA-256: 063e10e7df19095f5ee8217cb031e03297941194f460959a47056c12a38dab66
```

The integrity record accompanies
the baseline and
recovery read. No SPI writes were
issued. The post-recovery clock report
again records `0x112a8800`, the 144 MHz estimate, valid branch/loop callbacks and
verified scratch restoration. This establishes recovery to stock MSC behavior;
camera capture functions were not exercised in this acceptance check.
