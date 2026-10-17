# JT5295 aligned PLL transition acceptance — 2026-10-13

> **Historical record.** This report describes bring-up work done with the `apxtool hw` / `payload` commands, the
> `firmware/` payloads and the vendor-callback (APXS) flasher. Those were removed when the flashers were unified onto
> `appotech-sdk/boot/flasher.c` (see the [README](../README.md#flashing)); the measurements and conclusions stand,
> but the commands shown here no longer exist in this tree.

**The increased stock PLL pair passed the requested sustained loader acceptance
in two fresh sessions.** Each held the pair for more than 30 seconds, completed
100 timed USB callbacks, and passed five saved-and-restored RAM patterns with
48 adjacent guard bytes. All sampled clocks and diagnostic RAM were restored.
The final full SPI read matches the fresh pre-trial backup exactly. The camera
was then identified as Buildwin / Media-Player / 1.00 in normal firmware mode.

This is a repeatable **approximately 144 MHz** loader configuration with a
measured **1.499686×** loop throughput increase over the approximately 96 MHz
baseline. Absolute CPU MHz remains inferred: the PLL reference/routing and
timer relationship have software evidence but no independent frequency
measurement. `cpu_hz` remains null; this result does not resolve that dependency
or establish reliability beyond the tested sessions.

## Candidate and ordered transition

The [earlier follow-up](clock-pair-validation.md) failed with its upward writes
at offsets 24 and 28 within a hypothetical 32-byte cache block. The successful
temporary payload placed them at offsets 0 and 4. The new assembly explicitly
aligns both upward and downward sequences to 32 bytes and keeps each pair of
writes and its complete 500-iteration settling loop within 28 bytes. Alignment
padding decodes as `l.nop`, including trailing section padding.

The sequence still uses the vendor coefficient-then-control ordering established
in [the raw trace report](loader-clock-transition.md):

| Phase | Address in tested payload | Operation |
|---|---|---|
| Upward coefficient | `0x021884a0` | SPR `0x885c` ← `0x112a8800` |
| Upward control | `0x021884a4` | SPR `0x8858` ← `0x1123` |
| Settle | `0x021884a8..0x021884b8` | Bounded 500-count loop |
| Downward coefficient | `0x02188540` | SPR `0x885c` ← `0x0b71b000` |
| Downward control | `0x02188544` | SPR `0x8858` ← `0x1623` |
| Settle | `0x02188548..0x02188558` | Same bounded loop |

The count is an experimental vendor-derived settling delay, **not a proven PLL
lock check**. No oscillator routing, `0x884c` USB-owned bit 31, PLL2, dividers,
memory timings, interrupt enables, cache controls or timer registers are written.
The code is stackless with immediate constants and rollback resident in
validated RAM, no calls, preserved callback return context, and inert branch
slots apart from the isolated branch-behavior experiment.

Alignment is a supported experimental lead, not an isolated cause of the prior
failure. Cache-line size and cache preload are unverified; padding changes code
placement and timing too. Two successful aligned runs cannot establish which
of those changes mattered.

The exact 1,568-byte payload,
instruction listing,
checker certificate, and
candidate rationale are archived.
Payload SHA-256:
`9b02950c8a7a18edf8971d3a5cffaaaffc45425fe09cfbc54e8b5567af2d2728`.

## Hardware results

| Measurement | First loader session | Fresh second session |
|---|---:|---:|
| Temporary baseline, 1,000,000 iterations | 2,389 ticks | 2,389 ticks |
| Temporary higher profile | 1,593 ticks | 1,593 ticks |
| Sustained duration | 30.0917 s | 30.0944 s |
| Timed callbacks | 100 | 100 |
| Sustained ticks per million iterations | 1,592–1,593 | 1,592–1,593 |
| Median sustained ticks | 1,593 | 1,593 |
| Sustained loop speedup | 1.499686× | 1.499686× |
| Guarded RAM patterns and restorations while held | 5 | 5 |
| Nine clock/timer registers restored | Yes | Yes |
| Witness and full diagnostic scratch restored | Yes | Yes |

Raw reports preserve each response slot, sequence, progress marker, timer
samples and transfer duration: first
and second. The
session transition record
records normal firmware identity between runs; the second command sent `0xda`
to enter a fresh loader session. Both temporary checks succeeded before their
hold operation. USB exchanges while held—not merely after rollback—returned
the higher register pair and the faster loop measurements.

Timer deltas use the observed reload `0x3fff`, hence modulus `0x4000`, and reject
callback durations spanning a whole timer period under the inferred 32,768 Hz
reference. Loops use the same four executed instructions per iteration as the
baseline. Each million-iteration held callback took about 49 ms, consistent
with 1,592–1,593 ticks; USB duration is only a consistency check, not a CPU MHz
measurement. Shared timer configuration stayed unchanged. Raw ticks and the
relative speedup are measured; absolute CPU frequency remains an estimate.

The loader was restored before removing scratch in both sessions. After the
second session, the device briefly re-enumerated during the interval before
the flash check; its SCSI path was absent before that command could open it.
Sysfs then showed the expected normal camera, and the full read completed.
No callback or clock-write sequence was retried after a failed device probe.

## Flash integrity and validation

Fresh pre-trial SPI and
final SPI each contain 2,097,152 bytes
and compare byte-for-byte, SHA-256:
`063e10e7df19095f5ee8217cb031e03297941194f460959a47056c12a38dab66`.
No SPI erase/program command was issued.
The acceptance record audits both
sessions, restoration events, preserved registers and full flash comparison.

**92 host tests passed.** New negative checks reject the archived unaligned
failed layout, a changed critical settling loop, and a stalled held timer.
The stalled-timer test verifies a single rollback while the transport remains
responsive. Build checks also verify little-endian word conversion, approved
SPR writes/values, response-only stores, preserved context and branch slots.

`hw clock-pair-hold` retains `--experimental --yes`, analyzed firmware and exact
baseline guards, a dedicated checked payload, and persisted attempt receipts.
Two successful sessions consume this candidate's test allowance; invoking it
again is refused before device access. Failed historical payloads remain
blocked, and `hw clock-trial` stays disabled. This work establishes a tested
transition candidate; a persistent firmware clock setter and broader peripheral
or long-duration validation remain separate work.
