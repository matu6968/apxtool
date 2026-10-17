# JT5295 loader evidence and limits

> **Historical record.** This report describes bring-up work done with the `apxtool hw` / `payload` commands, the
> `firmware/` payloads and the vendor-callback (APXS) flasher. Those were removed when the flashers were unified onto
> `appotech-sdk/boot/flasher.c` (see the [README](../README.md#flashing)); the measurements and conclusions stand,
> but the commands shown here no longer exist in this tree.

This note distinguishes observations on the Action Vision camera from behavior inferred from the normal firmware image or borrowed from other Buildwin devices.

## Confirmed on this camera

- Normal firmware reports `Buildwin Media-Player`; loader mode reports `BuildWin Video050Loader` (`1908:3319`). Sending a no-data `0xda` CDB enters loader mode.
- In loader mode, a `0xcb` CDB with primary function address `0x00102620`, callback `0xffffffff`, and a RAM address returns data from `0x02000000` through `0x023fffff`. Transfers up to 64 KiB completed with zero residual.
- A 4 MiB capture was taken (SHA-256 `495528c2ead889008157c1b4ac3e6eeebfaaad6dd210a0dc1df90024be8b5e44`; the file was not kept). Its first `0x90334` bytes matched an earlier capture and the verified SPI image at file offset `0x2200`. The candidate payload and stack areas were zero in this capture; this is not a general guarantee that the ranges are free after every loader start.
- A 68-byte `0xcb` data-out to `0x02180000` completed with zero residual, and an immediate `0xcb` read returned identical bytes.
- The BootROM's `0xcb` handler at `0x00102620` calls transfer helpers at `0x00102530` and `0x00102584`. They invoke the optional callback pointer from context `0x4f74+0x74` with `jalr`, passing a buffer, length, and opaque word in `r3`, `r4`, and `r5`. The candidate callback enters with `r1=0x4f1c`; using that ROM stack from C overwrote loader state. A stackless entry stub saved `r1` and the return register at `0x02181014..0x0218101b`, switched to a checked zeroed stack at `0x02200000..0x02200fff` with its top at `0x02201000`, and returned. A C leaf that writes `APXT` and a forward assembly helper call returned on hardware.
- The normal firmware's runtime code bytes at the candidate API addresses match the expected signatures when read through `0xcb` in loader mode. `flash probe --start-loader` checks these five signatures without uploading a payload or accessing SPI: setup at `0x02032c08`, ID at `0x02032cfc`, read at `0x02032e8c`, page program at `0x02032f54`, and sector erase at `0x02033070`.
- The chip marking is `25Q016ETJG`, consistent with 2 MiB. The verified comparison image is `flash-icon-overlay-postwrite-v2.bin` in the workspace root.
- A 64 KiB BootROM read from `0x00100000` is preserved at [../research/jt5295-rom-00100000-64k.bin](../research/jt5295-rom-00100000-64k.bin), SHA-256 `f5b68ff45bff5f4f21c49008ed0a4bcbab01f2a2cb5aeff8f832ee41be460e39`.

## Ghidra language module

The [`ghidra-appotech-jt5295`](https://github.com/matu6968/ghidra-appotech-jt5295) module (a fork of [`ghidra-or1k`](https://github.com/smaeul/ghidra-or1k)) builds a little-endian variant of standard 32-bit ORBIS32, registered as `OpenRISC:LE:32:default`. It was rebuilt with Ghidra 12.1.2's SLEIGH compiler; the resulting `.sla` matches the installed copy. Ghidra scripted inspection reports little-endian byte order and decodes BootROM bytes `00 00 0f 8d` at `0x001008b0` as `l.lbz r8,0x0(r15)`. Both the retained BootROM and 2 MiB SPI image are loaded with this language.

## SPI routine analysis

The Ghidra project now uses the OpenRISC language and its inline script runner to disassemble the previously undefined application routines. Radare2 analysis of the word-swapped RAM capture independently shows the same code.

- `0x02032c08` is a short setup wrapper. It calls `0x02025750(0x005b8d80, 1)`.
- `0x02032cfc` sends the JEDEC ID opcode `0x9f`, reads three bytes, and packs them into the return value.
- `0x02032e8c` implements flash read using opcode `0x03`.
- `0x02032f54` programs one 256-byte page with opcode `0x02`.
- `0x02033070` erases a 4 KiB sector with opcode `0x20` and aligns the address down to a sector boundary.
- The low-level byte-transfer helper at `0x02025970` polls SPR `0x9248`, bit 0, in an unbounded loop. Ghidra's decompilation confirms the wait loop.
- `0x02025750` enables clock gate 5, configures pin-function/direction registers through helpers in `0x02023874..0x02023e44`, and writes controller SPRs in `0x9000..0x927c`. This call path does not call the known SPI transfer helpers at `0x02025970` or `0x020259c4`; the earlier theory that setup itself timed out in the `0x9248` status loop was incorrect.
- The setup core calls `0x02023004(5, 1)`. This helper reads and writes SPR `0x8840` for bit indices below 32, or `0x8844` for indices 32–63. A Ghidra script and decompilation confirmed that the isolated `0x8840` probe targets a register used by the normal firmware. The exact bit polarity and hardware effect remain unresolved.
- Startup evidence is incomplete. The imported image has a direct call from `0x02002808` to `0x02031d08`; current Ghidra analysis finds no direct references to `0x02032004`, `0x02032c08`, or `0x02032cfc`. This does not exclude indirect calls through runtime tables, but it does not support the earlier claim that both setup helpers directly precede the SPI path. The function's initialization role and any dependency on it remain unresolved.

## Hardware SPI attempts (2026-10-12)

- `flash probe --start-loader` completed: all five API signatures matched.
- The first APXS implementation reused one control block at `0x02181000`, triggered callbacks with a data-in request at `0x02000000`, then read the response separately. `flash id` returned the prior `APX1` marker. A 512-byte bulk read and an 8-byte byte-at-a-time read both left the staging buffer at zero. These are not flash results: the response could be stale and the SPI read routine was not shown to have run.
- Static analysis of BootROM helper `0x00102530` shows that data-in callback dispatch passes the CDB memory address in `r3`, invokes the callback, then transfers data from that same address. The host now writes each APXS request to a fresh 256-byte-spaced slot, passes that address to the callback, and receives the response in the same `0xcb` data-in transfer. It clears response fields and rejects a callback that leaves them untouched.
- `flash stage-probe --start-loader` passed using the fresh-slot protocol. The callback wrote `APXD` to the staging buffer; the target-side sample and a `0xcb` RAM read both returned `41 50 58 44`. This confirms callback request/response and CPU-to-loader staging visibility without accessing SPI.
- With the fresh-slot protocol, `flash init-probe --start-loader` returned the `INI` marker but the APXS status byte was `0x49`; apxtool rejected the response. Control flow reached the post-initializer marker write, but this is not counted as a successful setup result. The status anomaly remains unresolved.
- `flash id --start-loader` with the corrected callback protocol timed out (`host status:03`); volatile scratch restoration then failed because the SCSI device disconnected. The camera re-enumerated into normal `Buildwin Media-Player` mode. The byte-transfer helper polls SPR `0x9248` bit 0 without a timeout; the exact blocking point has not been isolated.
- The `0x49` initializer status and `0x40` ID-probe status match the value left in `r17` when the old compiler output branched to its common status handler. `r17` was set to `0xa5` in that branch's delay slot. If the CPU's CPUCFGR[ND] bit is set, the delay-slot instruction is skipped. This is a strong explanation for the status bytes, not confirmation of the JT5295 configuration. The OpenRISC manual identifies CPUCFGR as SPR `0x0002` and ND as bit 10; see the [OpenRISC 1000 Architecture Manual](https://openrisc.io/or1k.html).
- The firmware build now passes `-fno-delayed-branch`. The rebuilt payload has NOPs after all 84 inspected branch instructions and has a stable SHA-256 of `8f7049cee0e249036723fbfee3c1b3bd826f75fdfc8e2b005f346e485406b91e`. `flash hello --start-loader` passed with that binary and did not issue an SPI command.
- A read-only `flash regs --register 0x0002` probe tried CPUCFGR. It returned an invalid APXS callback block; volatile-state cleanup remained blocked in SCSI I/O until interrupted. The camera then disappeared from `/dev/sg*` and USB, but later re-enumerated normally and subsequent JEDEC/full-read operations succeeded. CPUCFGR was not recovered, and this is not evidence that ND is set. No SPI operation was requested by the register query.
- The first `flash regs` hardware probe uploaded and read back, but the callback disconnected before returning its batch SPR snapshot (`host status:07`); restoration then failed because the SCSI device had disappeared. After re-entering the loader, the 104-byte request/response block was all zero, so no partial values survived. No SPI command was issued.
- The isolated `flash regs --register 0x8840` probe also timed out in the callback (`host status:03`) before returning a value; the camera later enumerated as normal firmware. This narrows the issue to the first `l.mfspr` access or its immediate callback context and shows that the original batch response size was not the only problem. No flash command was issued.
- A later isolated `0x8840` probe again timed out (`host status:03`); this time the volatile scratch restore also failed and the camera temporarily disappeared from USB before re-enumerating as normal firmware. That request used the SPR address and destination in the same register. A Ghidra script scan found 30 such source/destination aliases in the firmware, so the alias alone is not evidence of a cause. A rebuilt diagnostic forced distinct registers to match the setup routine's form, but it produced the same timeout and restore failure. This rules out that register allocation as the immediate cause; callback context or a processor/hardware response remains unresolved.
- An isolated read of setup SPR `0x8850` using the rebuilt distinct-register payload failed with the same `host status:03` and scratch-restore failure. After re-enumeration, the camera answered as normal firmware. These early timeouts were consistent with an SPR/callback issue, though they did not prove its cause.
- A read of standard OpenRISC `SR` (SPR `0x0011`) also timed out with the same callback and scratch-restore failures; the camera re-enumerated as normal firmware. No SR value was returned, so this does not establish whether the callback runs in user or supervisor mode. The standard interpretation uses bit 0 (`SM`) for that distinction; see the [OpenRISC 1000 Architecture Manual](https://openrisc.io/or1k.html).
- With `-fno-delayed-branch`, `flash init-probe --start-loader` returned successfully and JEDEC probes returned `5E4015` both with and without GPIO initialization. This strongly supports the branch-delay explanation for the earlier `0x49` and `0x40` status bytes. A CPUCFGR query at SPR `0x0002` still returned an invalid APXS block, so CPUCFGR[ND] has not been measured directly.
- The first full-chip read attempt failed after callback slots reached the SPI staging buffer at `0x02182000`. Each new command used the next `0x100`-byte slot, so slot 16 overlapped the 64 KiB read buffer and the read overwrote its own request. The host now places control slots at `0x02192000`, immediately after the staging range, and checks/restores `0x02180000..0x021a1fff` before and after payload use. A 17-chunk fake read crosses the old boundary and passes.
- `flash read research/spi-full-read-20261012.bin --address 0 --length 0x200000 --start-loader` completed with SHA-256 `03d9919a02a302b660b4a19eb1a1f3031e53d65b8407e5c4b0e5f0185b89c961`. The dump equals `flash.bin.golden` byte-for-byte and differs from `flash-icon-overlay-postwrite-v2.bin` only in the previously modified icon and overlay slots. The actual final sector `0x1ff000, 0x200000)` is all `0xff`.
- The planned isolated final-sector test passed. `flash write` programmed `APXTEST1` at `0x1ff000` with a fresh pre-program full-chip backup (SHA-256 `03d9919a02a302b660b4a19eb1a1f3031e53d65b8407e5c4b0e5f0185b89c961`); the command's sector readback matched. `flash erase-sector 0x1ff000` then took a second fresh full-chip backup (SHA-256 `e5dcf562925dc2c6d5c0da0607a1d8691cd694a76d6a48865bf20c0c4b4bb54f`) and verified the sector was `0xff`. The erase backup equals the baseline plus the eight marker bytes, and a full-chip `flash verify` after erase matched the original read exactly. No other flash range changed. The external programmer was not attached during this test.
- The earlier fixed-slot `SPI`, `APX`, and `APX1` ID/SPR results are superseded and are not hardware values. The latest fresh-slot read path has returned JEDEC ID `5E4015` and a full SPI image matching the original golden file.

## Current flash state after asset update (2026-10-12)

- Before the asset update, a fresh 2 MiB SPI read matched `flash.bin.golden` byte-for-byte (SHA-256 `03d9919a02a302b660b4a19eb1a1f3031e53d65b8407e5c4b0e5f0185b89c961`). The requested `flash-icon-overlay-postwrite-v2.bin` differs from that image in 13 sectors across the two known artwork regions.
- The first full-image `flash write` saved [the pre-write full-chip backup, then stopped when the APXS arena ran out of 256 callback slots. A full SPI read showed 12 sectors matched the target and only sector `0x152000` was incomplete. No differences outside the two artwork regions were found.
- The incomplete sector was repaired from the target image with a second fresh full-chip backup at flash-pre-asset-sector-152000-repair-20261012.bin. The sector readback matched, then `flash verify` compared all 2 MiB against the modified image and passed. Expected and actual SHA-256 were both `0056baec26a157254e929c381a5e6753ce5ad525f52bb28ee8aa8b361957c616`.
- The camera subsequently answered INQUIRY as normal `Buildwin Media-Player` firmware. The external programmer was available but remained unplugged. The original pre-write backup remains available for external recovery.
- `write_file` now skips unchanged sectors and preflights the APXS callback budget after taking the fresh full-chip backup. If an update cannot fit in the current payload session, it refuses before any sector erase; split large updates into smaller sector ranges. This guard was added after the hardware attempt above.
- SPI JEDEC/read/program/erase behavior is now verified on hardware, including the isolated final-sector program/erase test and this multi-sector asset update. CPUCFGR reads and general-purpose writes to other firmware regions remain unverified.

The payload's `flash regs` diagnostic attempts to read one whitelisted SPR at a time, covering OpenRISC CPUCFGR at `0x0002`, setup registers `0x8840`, `0x8844`, `0x8850`, selected controller registers in `0x9000..0x927c`, and SR at `0x0011`. It does not write SPRs or issue a flash command. The tested SPRs have not returned trustworthy values, so loader-entry state remains unknown. OpenRISC restricts some SPR accesses by privilege level; callback privilege context remains one possible dependency, but the hardware evidence does not establish the current mode.

## Hardware investigation follow-up (2026-10-12)

The [hardware report](hardware-report.md) supersedes the earlier uncertainty
about taken-jump behavior: a stackless assembly callback confirmed that the
following instruction is skipped. The same payload returned valid isolated
reads of SPRs `0x8858`, `0x885c`, `0x8860`, `0x8864`, `0x8868`, and `0x9800`.
The earlier C probes remain historical failures; their cause has not been
isolated, and neither CPUCFGR nor standard SR was re-probed.

PLL1's live coefficient and the matching BootROM table support a 96 MHz
loader-clock estimate using a 32.768 kHz reference. Subsequent normal MSC
measurements support 144 MHz, with successful `0xcd` assembly callbacks. Two
loader boost trials failed, and the second required an owner power cycle;
recovery and unchanged flash were verified. The historical clock-trial CLI is
disabled. A later [aligned stock-pair candidate](clock-aligned-validation.md)
passed two sustained loader sessions with 200 callbacks and unchanged flash.
The higher CPU clock remains an estimate. See the
[clock mode report](clock-modes.md) for logs and limits. Counter timing supports
that reference, but CPU routing and pipeline cycles remain inferred. Two RAM
pattern runs verified 39 distinct guarded addresses with CPU/loader agreement
and restoration. The before/after SPI images matched exactly; no flash write
was issued. Stock-mode free RAM and display/camera buffer ownership remain open.

## Other evidence and open questions

- Normal firmware dispatch begins at `0x0203c360`; the `0xcd` branch at `0x0203c574` calls registered handler `0x0203be30`. The validated primary transfer function is `0x0203bdf4`; CBW parser `0x0203c884` extracts the function, memory and optional callback from CDB bytes 1–4, 5–8 and 9–12. The normal-mode adapter preserves this layout while substituting opcode `0xcd` and that primary function.
- Earlier normal-mode probes used other function addresses and failed. Corrected normal-mode reads, uploads and stackless callbacks now pass, including verified scratch restoration; see the [mode comparison](clock-modes.md).
- Hardware diagnostic uploads use bounded 512-byte chunks. Maximum safe transfer size and arbitrary partial-transfer behavior remain unknown.
- The current stack and payload locations were zero in captured memory and are checked before use, but there is no general memory-ownership map.
- The BootROM implementation is not part of `flash-icon-overlay-postwrite-v2.bin`, so the ROM handler itself must be analyzed from the retained BootROM capture.
- The RAM command interface uses `APXS` in a RAM request block; it does not send the previously considered private `0xe0` SCSI opcode. This framing is an apxtool payload contract, not a built-in loader command.

## Related device report

A report for a different Buildwin children's camera describes `0x0da` loader entry, a function at `0x00102620`, OpenRISC instruction storage, and RAM execution experiments. Those observations motivated these tests but do not establish JT5295 behavior. [Habr report](https://habr.com/ru/companies/ruvds/articles/779626/).
