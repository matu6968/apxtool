# Operational payload clock defaults — 2026-10-13

> **Historical record.** This report describes bring-up work done with the `apxtool hw` / `payload` commands, the
> `firmware/` payloads and the vendor-callback (APXS) flasher. Those were removed when the flashers were unified onto
> `appotech-sdk/boot/flasher.c` (see the [README](../README.md#flashing)); the measurements and conclusions stand,
> but the commands shown here no longer exist in this tree.

`make -C apxtool/firmware all` now selects the approximately **144 MHz** profile
for apxtool marker and SPI payload sessions. `CPU_MHZ=96` selects the original
approximately **96 MHz** loader baseline. Changing this option refreshes the
hash-bound policy sidecars without requiring a clean rebuild. The final build
was returned to the 144 MHz default after testing.

This changes apxtool session behavior, not the camera's SPI image, BootROM or
stock firmware boot defaults. The original clock is restored when each session
ends. The diagnostic payloads retain their incoming clock.

## Runtime implementation

The clock companion has exactly the accepted aligned instruction bytes from
[sustained acceptance](clock-aligned-validation.md), SHA-256
`9b02950c8a7a18edf8971d3a5cffaaaffc45425fe09cfbc54e8b5567af2d2728`.
It is linked at `0x02170000`, outside operational scratch
`0x02180000..0x021a1fff` and the callback stack at `0x02200000`. Its relative
branches and immediate constants produce the same binary at this aligned
address; the checker requires that exact hash, approved PLL writes, inert slots
and 32-byte critical-block alignment. This relocation was tested live below.

The runtime arena is `0x02170000..0x02174fff`, including 64 fresh response slots
starting at `0x02171000`. It is saved and preflighted as zero before upload,
restored and verified afterward, and excluded from RAM-test witnesses. It
remains candidate scratch within the known RAM window, not a permanent claim
on stock-firmware free memory.

The host validates the analyzed runtime prefix, checked payload/certificate,
branch behavior, and all nine baseline clock/timer registers. At 144 MHz it
first runs the fixed temporary paired transition and verifies rollback plus
the expected throughput ratio, then issues the hold operation and checks the
held profile. Operational code executes while that pair remains active. On
close, identity and the current profile must support one bounded rollback;
the original nine registers are compared before scratch cleanup. Unverified
clocks preserve both clock and operational scratch. No arbitrary register-write
interface is added, and failed clock callbacks are not retried.

The 96 MHz policy checks the same original loader baseline and issues no PLL
writes. It verifies the unchanged profile before cleanup. A camera already in
an unexpected clock configuration is rejected instead of silently retuned.

Every configured session journals its selected profile, session ID, code hash,
fresh slots, progress responses and restoration results under
`apxtool/research/runtime-clock-*.json`. Policy sidecars bind both the operational
image and the clock companion by SHA-256. Images without a sidecar use the
historical incoming-clock behavior.

The 500-count settling loop remains an experimental vendor-derived delay,
not a verified PLL-lock check. No shared routing, USB-owned fields, memory
timings, interrupts, cache controls, PLL2 or timer configuration is written.
Absolute CPU MHz remains inferred; the accepted relative increase is measured.

## Validation

Both profiles passed live marker callbacks and complete 2 MiB SPI reads through
the normal operational paths. The 144 MHz sessions observed `0x1123` /
`0x112a8800` while held, then restored `0x1623` / `0x0b71b000` and all other sampled
clock/timer registers. The 96 MHz sessions dispatched no clock-write operation.
Clock and operational RAM cleanup completed. No SPI erase/program command ran.

The two new full reads compare byte-for-byte with the prior acceptance image:

- 144 MHz read
- 96 MHz read
- Validation record

Each 2,097,152-byte image has SHA-256
`063e10e7df19095f5ee8217cb031e03297941194f460959a47056c12a38dab66`.

**102 host tests passed**, including policy/image tampering, unsupported
frequencies, changed firmware/baselines, slot capacity, disconnects, unknown
clock state, downclocked sessions with no PLL writes, and clock-before-scratch
cleanup. Tests verify that failed rollback leaves marker/SPI recovery RAM intact.
Hardware clock setters remain limited to the two fixed PLL pairs. Broader
peripheral behavior, SPI erase/program at the higher clock and long-duration
reliability were not tested in this change.
