# Silvergear printer camera: verified stock backup

Target: [Action Silvergear article 3222882](https://www.action.com/pl-pl/p/3222882/aparat-z-mozliwoscia-natychmiastowego-drukowania-silvergear/),
JT5295, thermal printer, two cameras and an LED flash. Supplied PCB photos show
board `D-426_V03`, dated `2026 04 02`. The owner subsequently confirmed the flash marking as
`NM25Q32A` (the earlier photograph was misread as `PN25Q32A`). Hardware probing
confirmed JEDEC `94 40 16` and 4 MiB capacity.

The supplied `printer-cam.log` and `printer-cam-plugged-in.log` both report:

- firmware build `2026/04/20 16:22:35`;
- UART initially 115200 baud, then 460800 baud;
- internal photo storage at `0x382000`, length `0x7d000`, ending at `0x3ff000`;
- chip UUID checks passing, followed by `OpenImage Credit Check Failed!`.

These are observations, not evidence of a BootROM lock or encrypted SPI image.
The charging log reaches sensor probing. It does not establish the cause of the
reported shutdown. Forced loader entry works with the battery attached and the
short subsequently removed. The loader can disconnect after host traffic stops;
perform the backup and verification in one continuous session.

## Procedure

Run from the directory containing the sibling `apxtool/` and `appotech-sdk/`
checkouts. Use checkout sources explicitly so an older installed apxtool cannot
silently run instead. Build the current payload first:

```sh
make -C appotech-sdk/boot
```

With the battery attached, use the already-tested SPI-short/power-on sequence to
enter BuildWin loader mode, with USB connected to the computer. **Release the
short once loader mode is entered, before accessing the flash.** The expected
loader USB identity is `1908:3319`, SCSI `BuildWin Video050Loader`. Auto-discovery
is available; when selecting a device explicitly, put `--device /dev/sgN` before
`rom-rescue`.

```sh
PYTHONPATH=apxtool/src python3 -m apxtool rom-rescue --spi-init sdk dump printer-cam-stock.bin --verify
```

The initial ROM-only diagnostic found SPI disabled (`0x9000=0`, `0x9244=0`),
returning zeros or a timeout instead of a usable JEDEC ID. After user approval,
the existing `--spi-init sdk` sequence initialized the SPI pins/controller and
read `944016`; that setup was used for the successful backup. It does not apply
the older board's PM sequence or issue any flash erase/program command. The
payload runs in on-chip SRAM, so this procedure does not require working DDR
or a running stock USB mass-storage implementation. It disables the BootROM
watchdog on each payload entry; the stock loader entry command is unnecessary.

Standalone diagnostics and verification remain available after re-entering the
loader if it has disconnected:

```sh
PYTHONPATH=apxtool/src python3 -m apxtool rom-rescue --spi-init sdk jedec
PYTHONPATH=apxtool/src python3 -m apxtool rom-rescue --spi-init sdk verify printer-cam-stock.bin
```

`--spi-init rom` still explicitly reuses an already-configured SPI bus. No
automatic initialization fallback occurs just because the manufacturer is new.

Identification prints the actual three-byte JEDEC ID. Capacity codes `0x15` and
`0x16` permit reads of 2,097,152 and 4,194,304 bytes respectively. Other capacity
codes are reported with `read_supported: false` and cannot be dumped. An
unfamiliar manufacturer does not cause a second SPI initialization attempt.
Blank/invalid bus responses fail with the returned bytes in the diagnostic.

The dump contains **every byte**, including device identity, calibration,
firmware, resources and any internal photos. Its adjacent `printer-cam-stock.bin.json`
records the actual ID, capacity, dump SHA-256, payload SHA-256, SPI setup path and
boot-header diagnostics. An invalid boot header is recorded without blocking
the backup. Neither existing outputs, metadata nor `.part` files are overwritten.
An interrupted flash read publishes no dump; if saving metadata fails after the
binary was saved, the complete binary is preserved and the command fails.

`dump --verify` immediately follows the saved dump with an independent full-chip
read in the same loader session and saves `printer-cam-stock.bin.verify.json`.
If verification loses the connection, it can retry against the preserved backup
after reconnecting; the chip ID/capacity and backup hash must still match. A
verification mismatch exits 2 without replacing the original image.

Standalone `verify` requires a file exactly as large as the detected chip and performs a
fresh full-chip read. Success prints `verified: true` with matching expected and
actual SHA-256 values and exits 0. A mismatch prints both hashes and the first
mismatching offset, exits 2, and leaves the saved backup intact. A malformed file
or a file for the wrong capacity is rejected. Keep the dump, its metadata, and
the verify output together as the evidence for the backup. Returning to stock
operation and confirming it still works completes the hardware check.

## Hardware verification, 2026-11-02

The full chip was saved and compared byte-for-byte with an independent full
reread. Both passes produced:

```text
JEDEC:   944016
Bytes:   4194304
SHA-256: 4874af74051372363e8c36ff99e369657648eb353950b41fe2a5a00d35acdb9e
Payload: e1464a457d0947e5044d462eb07a5cc28a360e15056d730152ec3da7ffed92cb
```

The valid boot header loads at `0x02000000`, with 1,319 load sectors and flags
`0x05`. The captured 64 KiB BootROM is byte-identical to the original camera's
reference ROM (SHA-256 `f5b68ff45bff5f4f21c49008ed0a4bcbab01f2a2cb5aeff8f832ee41be460e39`).
There were invalid ID responses during entry and one rejected read; the retry
completed the dump and full verification. These failed attempts published no
partial flash image. No flash erase/program or PM sequence was performed. The
user confirmed normal stock camera operation afterward.

Workspace evidence is in `stock-backups/silvergear-3222882-20261102/`:
`stock.bin`, `stock.bin.json`, `stock.verify.json`, `loader-registers.json`,
`bootrom.bin` and the capture logs. The original capture used the dump and
verify actions consecutively in one session; the CLI's `dump --verify` option
now provides the same continuous workflow.

## Boundaries and software validation

Only the original `5e4015` device supports program/erase. Other IDs, including
all newly supported 4 MiB chips, remain read-only in both the host and payload.
Do not use the original camera's 2 MiB stock image as this camera's backup.

The model tests execute the real C payload behind the Python host client. They
cover distinct flash halves, the final byte, full 4 MiB backup and independent
verification, unsupported geometry, interrupted reads, file collisions and
write rejection before WREN. The build checks the existing SRAM size limit and
requires byte-identical SRAM/DDR payloads.

Restore support for this camera, charging behavior, SDK launch, display, both
cameras, printing and the flash LED are later work.

## Subsequent RAM-only power/DDR milestone

The display was subsequently verified with an independent SRAM PIO probe.
Power hold and all 4 MiB DDR then passed a separate RAM-only diagnostic,
including independent USB readback, SRAM restoration, loader recovery and an
owner-confirmed normal stock startup. See the workspace
[power/DDR evidence](../../appotech-sdk/research/silvergear/power-ddr.md) for
exact hashes, commands and limits. Build both `silvergear-bringup` and
`silvergear-stage` in `appotech-sdk/boot` for the current launch path.
`silvergear.prepare()` now uses the dedicated SPI-free power payload, with
PM44 read-and-echo and masked PM5a updates, rather than the SPI rescue payload.
Full SDK camera/printer hardware acceptance remains separate.
