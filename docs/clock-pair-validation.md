# JT5295 stock PLL pair tests — 2026-10-13

> **Historical record.** This report describes bring-up work done with the `apxtool hw` / `payload` commands, the
> `firmware/` payloads and the vendor-callback (APXS) flasher. Those were removed when the flashers were unified onto
> `appotech-sdk/boot/flasher.c` (see the [README](../README.md#flashing)); the measurements and conclusions stand,
> but the commands shown here no longer exist in this tree.

**Later aligned testing passed sustained loader acceptance in two sessions.**
See [aligned PLL acceptance](clock-aligned-validation.md) for 200 callbacks,
ten RAM pattern/restoration checks and unchanged full SPI contents. This report
preserves the preceding temporary success and failed unaligned follow-up.

The owner explicitly authorized an exploratory frequency test with hard-reset
recovery after the earlier evidence-gated investigation. This allowed a new,
bounded candidate despite unresolved live handoff and memory/USB dependencies.
The two historical `hw clock-trial` implementations remain disabled.

## Successful temporary trial

The new stackless payload was uploaded to `0x02185000`, separately from previous
diagnostic entry points. Firmware-prefix validation, payload readback, a unique
capability callback and isolated reads established the exact loader baseline.
The binary checker permits exactly these writes, with no function calls:

```text
SPR 0x885c = 0x112a8800   stock PLL1 coefficient, estimated 144 MHz
SPR 0x8858 = 0x00001123   stock PLL1 control
... bounded loop measurement ...
SPR 0x885c = 0x0b71b000   original loader coefficient, estimated 96 MHz
SPR 0x8858 = 0x00001623   original loader control
```

The paired writes follow the vendor's coefficient-then-control ordering. They
leave shared routing, PLL2, timer configuration, cache controls and interrupt
enable state untouched. A 500-iteration delay is an experimental use of the
vendor's post-pair count, not a verified PLL-lock wait. Return context is
preserved; all branch slots are inert except the existing branch-behavior test.

Two identical four-instruction million-iteration loops returned:

| Measurement | Result |
|---|---|
| Baseline timer ticks | 2,389 |
| Higher-clock timer ticks | 1,593 |
| Relative loop speed | **1.499686×** |
| Coefficient observed during the trial | `0x112a8800` |
| Control observed during the trial | `0x1123` |
| Rollback | `REST` marker and all nine sampled clock/timer registers match baseline |
| Saved RAM | Restored and verified |

This supports the higher stock PLL profile and its expected 1.5 performance
ratio in that one callback. Absolute CPU MHz remains inferred from the earlier
coefficient/reference analysis; JSON `cpu_hz` remains null. USB transferred the
result after rollback, so this result alone does not establish USB operation
while the higher configuration is active.

Raw report,
attempt receipt,
exact successful payload.
The 1,388-byte payload SHA-256 is
`5801c926948fe7c24c5e538acc43f4746ed28c0e394902cbd2a8359f8b5d810c`.

## Follow-up failed before the hold

A separately checked payload at `0x02188000` adds hold and restore operations.
Before allowing a 30-second hold, its host repeats a temporary transition and
verifies rollback. In that preliminary callback, SCSI returned host status
`0x07` and the device disappeared. A single passive response read returned
`ENODEV`; no valid partial stage or measurement was recovered.

**No hold operation was dispatched.** The 100-callback/guarded-RAM acceptance
and its repeat in a fresh loader session were not reached. No verified clock
rollback exists for this attempt. Cleanup preserved diagnostic RAM instead of
overwriting recovery code while clock state was unknown. A hard-reset request
was sent to the owner. No further clock-write trial ran during that phase;
the later authorized aligned experiment is reported separately above.

Failed report,
failed attempt receipt,
exact follow-up payload.
The 1,520-byte payload SHA-256 is
`8ab229ce223574328206db7a8b7ce731fe9acb69661ff13dc4d2a4acb152abb6`.

## Instruction-layout lead

The layout comparison records
actual write addresses and offsets within hypothetical 32-byte cache blocks:

| Upward transition | Coefficient write | Control write | Following execution |
|---|---|---|---|
| Successful payload | `0x02185480`, offset 0 | `0x02185484`, offset 4 | Settling loop stays in the same 32-byte block |
| Failed follow-up | `0x02188498`, offset 24 | `0x0218849c`, offset 28 | Next instruction crosses the 32-byte boundary |

An instruction fetch across that boundary while the PLL/memory clock is
unsettled is a plausible dependency. The cache-line size has not been measured,
and the payload address, surrounding code and timing also changed. No partial
stage establishes the actual hang location, so this is **not a proven cause**.
The later candidate aligns both writes and the complete settling loop and
passed two sessions. It does not establish cache preload or cache geometry,
and the instruction-boundary explanation remains a hypothesis.

This also refines the Action 104 analogy: executing from RAM was already
achieved, but instruction availability during a clock change may still matter.

## Tool behavior and validation

`hw clock-pair-trial` and `hw clock-pair-hold` require `--experimental --yes`, a
checked dedicated payload and the exact analyzed firmware/baseline. They accept
only the stock pair and original rollback pair, with fresh response slots and
bounded loops. The historical `hw clock-trial` remains disabled.

Before dispatch, an attempt receipt is persisted for the exact payload hash.
Failed or unknown attempts cannot automatically repeat. The temporary-only
payload is limited to one attempt. Hold acceptance permits at most two fresh
sessions, and the second requires completed first-session clock and RAM
restoration. The failed follow-up is therefore blocked before device opening
or loader entry if invoked again.

The planned hold code requires a successful temporary measurement, runs 100
timed callbacks over at least 30 seconds, checks five CPU/host RAM patterns and
48 neighboring guard bytes, and verifies all sampled clocks after explicit
rollback before restoring RAM. These behaviors passed fake-transport tests;
they **have not passed the corresponding hardware hold test**.

Build checks verify instruction byte order, matching host/target SPR lists,
preserved callback registers, response-only stores, internal branch targets,
inert slots, no calls and the exact four permitted PLL writes. **89 host tests
passed**, including bad capability/baseline, payload tampering, rollback
mismatch, held disconnects, cleanup ordering and repeat refusal.

The fresh pre-trial 2 MiB SPI read
matches the previous image exactly, SHA-256
`063e10e7df19095f5ee8217cb031e03297941194f460959a47056c12a38dab66`.
No SPI erase/program command was sent.

## Post-failure recovery and flash integrity

After the owner's first power cycle, standard SCSI INQUIRY identified the camera
as Buildwin / Media-Player / 1.00 in normal firmware mode. A fresh full
post-failure SPI read is byte-for-byte
identical to the pre-trial 2 MiB backup, with the SHA-256 above. This is a
completed post-failure flash integrity check.

An additional normal-mode clock snapshot
returned all nine stock register values, including PLL1 control `0x1123` and
coefficient `0x112a8800`. Its diagnostic RAM cleanup then timed out with host
status `0x03`; the overall capture remains failed and software restoration is
not verified. This optional capture was unnecessary for the flash comparison.
No clock writes occurred during it, and no payload diagnostic was retried.

The owner power-cycled the camera again. Standard SCSI INQUIRY alone confirmed
the same normal-mode identity after that reset. No payload uploads, register
probes or frequency trials ran after the final reset. Recovery is attributed
to the physical reset, not to successful software RAM restoration.

The recovery integrity record
preserves the comparison, cleanup failure and final identity check. At the end
of that phase sustained operation remained unverified; subsequent aligned
acceptance is recorded separately above.
