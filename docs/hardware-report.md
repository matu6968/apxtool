# JT5295 memory and clock investigation

> **Historical record.** This report describes bring-up work done with the `apxtool hw` / `payload` commands, the
> `firmware/` payloads and the vendor-callback (APXS) flasher. Those were removed when the flashers were unified onto
> `appotech-sdk/boot/flasher.c` (see the [README](../README.md#flashing)); the measurements and conclusions stand,
> but the commands shown here no longer exist in this tree.

Measured on 2026-10-12 using the connected Action Vision camera. The initial
RAM/clock diagnostics below issued no SPI erase/program or clock/cache/timer
setting commands. A subsequent [mode comparison and loader boost investigation](clock-modes.md)
measured normal MSC mode and tried two unsuccessful temporary clock changes.
The second required a power cycle; recovery and unchanged SPI contents were
verified. The clock-trial CLI is disabled.

The [2026-10-13 loader-clock follow-up](loader-clock-transition.md) provides
fresh mode/dependency profiles and reproducible raw traces. A supported live
handoff and rollback remain unresolved; the failed trials are also blocked at
the Python API/RPC entry points. Later [aligned PLL acceptance](clock-aligned-validation.md)
passed two 30-second higher-clock loader sessions with 200 callbacks, ten guarded
RAM restorations, 1.4997× throughput, verified rollback and unchanged full SPI.
The approximately 144 MHz value remains inferred.

Operational apxtool marker/SPI builds now default to that higher session profile;
`make -C apxtool/firmware all CPU_MHZ=96` selects the original loader clock.
Both profiles passed live marker and full SPI-read checks with restoration;
see [payload session clocks](payload-session-clocks.md). The stock loader boot
clock remains approximately 96 MHz, and diagnostics preserve their incoming clock.

## Results for firmware porting

| Property | Result | Evidence and confidence |
|---|---|---|
| Instruction storage | OpenRISC, little-endian instruction words | Vendor code, uploaded instruction readback, and returned assembly callbacks |
| Taken-jump delay slot | **Skipped** in loader callback context | The instruction immediately after a taken `l.j` did not increment the result. Repeated in each diagnostic session; this is a behavioral result, not a CPUCFGR reading |
| RAM window | **4 MiB**, `0x02000000..0x023fffff` | Complete readback; all simple modulo-mirror models from 4 KiB through 2 MiB rejected by snapshot comparisons |
| Writable, distinct RAM witnesses | **39 addresses**, two patterns each, in two runs | Address-specific patterns agreed between target CPU and loader reads; 48 neighboring guard bytes stayed unchanged; all saved bytes restored and verified |
| Physical RAM capacity | **4 MiB is a strong working hypothesis** | Sampled independence and startup bounds agree. This was not an exhaustive memory test, and cache maintenance is not independently verified |
| Loader CPU clock | **Approximately 96 MHz, inferred** | PLL1 coefficient yields exactly 96 MHz under the supported 32.768 kHz reference interpretation; loop timing is consistent with about seven cycles per iteration |
| Tested increased loader clock | **Approximately 144 MHz, inferred; two sustained sessions passed** | Aligned stock PLL pair held for 30 seconds per session; 200 callbacks and ten guarded RAM restorations passed; 1.4997× relative throughput; baseline restored |
| Crystal marking | `32.768`; **kHz is the supported interpretation** | PLL coefficients and a counter advancing near 32.7 kHz agree. Marking alone does not specify units; no external frequency measurement was made |
| Stock USB MSC CPU clock | **Approximately 144 MHz, inferred** | Live coefficient `0x112a8800` matches the bootstrap; loop throughput is about 1.46–1.48× loader. Repeated after recovery; other camera modes unmeasured. See [mode report](clock-modes.md) |
| Stock-mode free memory | **Unmeasured** | An allocator candidate covers about 3.32 MiB, but allocations, hardware buffers, and ownership are not fully mapped |

For an initial port, budget against the 4 MiB window and use the preflighted
volatile loader path. Do not reserve all zero-filled RAM as a permanent heap
while stock camera tasks run. Small assembly callbacks now also work through
the normal-mode `0xcd` path at the stock higher clock. Aligned loader boosting
passed the bounded acceptance above; broader operating reliability remains
unmeasured. Keep `-fno-delayed-branch` and NOPs after branches
until broader instruction and ABI behavior is established.

## Memory evidence

| Range or address | Interpretation |
|---|---|
| `0x02000000..0x02090333` | Captured prefix matches SPI bytes starting at file offset `0x2200`; code and initialized image content, not all executable instructions |
| `0x02093d94..0x023febff` | Application startup clear range at `0x02022e60`; contains globals and allocation space after initialization |
| `0x020ad200..0x023febff` | Allocator candidate from `0x02023330`: base `0x020ad200`, span `0x351a00` (3,480,064 bytes). Bounds are static evidence, not a free-memory measurement |
| `0x02140000..0x023effff` | Candidate loader test region; fresh snapshots and per-address zero checks precede writes |
| `0x02180000..0x021a1fff` | Proven volatile diagnostic arena. Payload begins at `0x02180000`; fresh 256-byte-spaced response slots begin at `0x02192000` |
| `0x02200000..0x02200fff` | Existing C callback stack reservation, excluded from pattern testing; the new assembly payload does not use it |
| `0x023fec00..0x023fffff` | Above startup clear/allocator boundary, treated as reserved; startup stack pointer is `0x023ffffc` |

The 39 witnesses are 16-byte patterns at 64 KiB intervals within the candidate
region, excluding the diagnostic arena and stack reservation. Each has a
64-byte saved block. Writing all witnesses before checking them detects aliasing
between the sampled addresses. Complementary second patterns exercise both bit
polarities. The host reads 1 MiB of unrelated RAM, and the CPU reads the same
1 MiB before every sample. This provides substantial cache eviction pressure,
but is deliberately reported as inferred eviction rather than a verified
cache-control operation. Address ranges outside the known 4 MiB window are
never tested. Display/camera buffer locations and stock allocator occupancy
remain unresolved.

The final CLI also compares the loaded code prefix with the analyzed SPI image
before allowing RAM patterns. A different firmware requires re-analysis of the
ownership bounds.

## Clock evidence and the crystal

Assembly-only SPR probes returned trustworthy completion markers and these
raw values, superseding the previous C register-probe failures:

| SPR | Value | Interpretation |
|---|---|---|
| `0x8858` | `0x00001623` | PLL1 control candidate |
| `0x885c` | `0x0b71b000` | PLL1 coefficient |
| `0x8860` | `0x00001623` | PLL2 control candidate |
| `0x8864` | `0x0bebc200` | PLL2 coefficient |
| `0x8868` | `0x044df82e` | Clock configuration with multiple fields; exact CPU routing remains unresolved |
| `0x9800` | `0x00000721` | Counter/timer control, retained without modification |

BootROM routine `0x00104604` calls the PLL setup at `0x001044d8` with table
`0x0010634c`, whose bytes begin `71 0b 00 b0 00 06 01 00`. That routine constructs
the high and low halves of `0x885c` from the table. They match the observed
`0x0b71b000` exactly. Treating the coefficient as Q16.16 gives:

```text
PLL1: (0x0b71b000 / 65536) × 32768 =  96,000,000 Hz
PLL2: (0x0bebc200 / 65536) × 32768 = 100,000,000 Hz
```

The SPI bootstrap independently constructs `0x112a8800`, which gives exactly
144,000,000 Hz with that same reference; the application's timer configuration
at `0x02024980` uses the constant `0x08954400` (144,000,000). Its alternative
bootstrap coefficients also consistently fit a 128 kHz reference. These exact
relationships support the fixed-point interpretation and low-frequency crystal
reference. Substituting 32.768 MHz directly would yield 96 GHz, requiring an
additional, currently unsupported divider explanation.

The runtime clock helper at `0x02022ec4` dispatches through the table at
`0x0205d0ec`, changing fields in `0x8868`. Callers include initialization at
`0x02023330` and a feature setup path at `0x02042f78/0x02042f94`. This is why
one loader measurement cannot establish every camera mode's frequency. The
subsequent normal MSC measurements are recorded in the [mode report](clock-modes.md).

Final loop medians were about 1.0 ms, 7.5 ms, and 73.2 ms for 10,000, 100,000,
and 1,000,000 iterations. Fitting the long-minus-short slope gives about
13.70 million iterations/s. The counter at `0x9804` advanced about 2,389 ticks
over the million-iteration loop. Including USB overhead, its observed rate was
about 32,646 Hz, consistent with 32,768 Hz. The 96 MHz hypothesis implies about
7.01 cycles/iteration. The loop has four executed instructions per taken
iteration; pipeline and branch costs are not established. Consequently
`cpu_hz` remains JSON `null`, while `cpu_hz_estimate` is 96,000,000.

No generic OpenRISC cycle counter was assumed to exist. CPUCFGR and SR at the
standard addresses were not re-probed. SPR `0x0018` is used by vendor code but
has not been equated to the standard SR. Likewise, `0x880c` is written throughout
vendor polling loops and has **not** been identified as an SDRAM register.
Ghidra's current language assumes delay slots; raw assembly controls these
conclusions, and existing decompilations need review under the measured behavior.
The [OpenRISC architecture manual](https://openrisc.io/or1k.html) describes the
standard registers and ND behavior, not this chip's undocumented peripheral map.

## Reproduce the diagnostics

Run from the workspace root; choose the camera's current SCSI path. Loader
entry may re-enumerate it, so the commands resolve the new path automatically.

```sh
make -C apxtool/firmware check-hw
PYTHONPATH=apxtool/src python3 -m apxtool --device /dev/sg0 hw inspect --start-loader --json inspect-new.json
PYTHONPATH=apxtool/src python3 -m apxtool --device /dev/sg0 hw ram-test --start-loader --experimental --yes --json ram-test-new.json
PYTHONPATH=apxtool/src python3 -m apxtool --device /dev/sg0 hw clock-test --start-loader --experimental --yes --timer-loop --json clock-test-new.json
```

`clock-test` normally measures loops and reads `0x8868`, `0x8858`, and `0x885c`
in separate callbacks. `--loop-only` avoids all SPR access. `--register-only
--register 0x8864` reads one whitelisted register without repeating loops.
`--timer-loop` additionally samples `0x9804` around each loop and leaves its
configuration unchanged. These three options are mutually exclusive.
`inspect --ram-output NEW.bin` also preserves its fresh complete RAM capture.

Output files must be new. JSON contains source image and payload hashes,
mode, tested ranges, raw callback responses, sequence numbers, elapsed times,
confidence, failures, and verified restoration events. Reports are atomically
journaled during the run. Each response uses a fresh slot and must carry the
matching sequence echo, completion marker, and terminal stage. A failed callback
cannot be retried in the same session. Restoration first checks the expected device mode,
has a 20-second budget plus at most one in-flight backend request (5 seconds),
and stops on its first failure. Loss of the expected identity or a failed restoration
requires a power cycle before further probes; logs are retained.

## Validation artifacts

- Fresh 4 MiB baseline, SHA-256 `0510bac4bb99b93606253652318ff041783978bf42b0f761ae14510c779ad1cc`.
- Read-only inspection, first RAM test, and final RAM test.
- Final clock/counter report, plus isolated PLL probes in `research/hw-pll*.json` and timer control.
- Flash integrity result: the full 2 MiB before/after dumps are identical, SHA-256 `063e10e7df19095f5ee8217cb031e03297941194f460959a47056c12a38dab66`. This baseline is the camera's actual image at investigation start, not an assumed historical asset image.
- Initial 828-byte payload SHA-256: `40f8b9f0cd14ef5bd36bac3922be67ca80b1a9fdf14fb0510db1a20da7b81a35`. Later read-only register additions change the build hash, recorded per session. `make check-hw` verifies word order, response-only stores, preserved loader context registers, no clock/cache/timer writes, and exactly one intentional non-NOP branch slot.
- Host tests cover protected/range boundaries, occupied or changing scratch, alias refusal, failed samples, fresh slots, short and stale responses, disconnects, failed/bounded restoration, experimental gates, and measured-versus-inferred clock reporting.

The initial RAM/clock diagnostic callbacks completed and all their restorations
verified without a manual power cycle. This does not apply to the later failed
clock-write trials: see [clock modes and recovery](clock-modes.md) for the failure
logs and final flash comparison. Next investigation steps are CPU clock-source
routing, verified cache maintenance, normal-mode allocator statistics, and
display/camera buffer ownership.
