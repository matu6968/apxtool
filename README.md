# apxtool

apxtool, also known as **Ap**poTech A**X** **Tool** is an experimental loader and flash tooling for the Action Vision camera built around the AppoTech JT5295.

Observed stock-firmware behavior—including JPEG asset compatibility, SD formatting, USB mass-storage disconnects, internal-flash image deletion, what Sys. Reset does not clear, and flash padding—is documented in [stock firmware quirks](docs/stock-firmware-quirks.md).

## Setup

The SDK examples below (`make -C ../appotech-sdk ...`, `../appotech-sdk/build/...`) assume the two repositories are cloned side by
side as `apxtool/` and `appotech-sdk/`: [appotech-sdk](https://github.com/matu6968/appotech-sdk).

```sh
cd apxtool
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

The SCSI pass-through backend (`scsiio`, from [jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool/)) is vendored in `src/apxtool/scsiio`, so no other checkout is needed. V1 targets Linux SCSI generic devices and requires an explicit `/dev/sgN` path.

```sh
apxtool --device /dev/sg0 info
apxtool --device /dev/sg0 loader start
apxtool --device /dev/sg0 ram dump ram.bin --start-loader
apxtool --device /dev/sg0 raw-cdb 'cb 20 26 10 00 00 00 02 ff ff ff ff ff ff ff ff' --direction in --length 512 --output block.bin
```

`loader start` sends the observed no-data `0xda` command and waits for the camera to re-enumerate as `BuildWin Video050Loader`. The SCSI path can change after re-enumeration; `ram dump --start-loader` performs the transition and capture in one process. RAM reads use `0xcb` in chunks of at most 64 KiB and reject short transfers or nonzero residuals.

## Ghidra language support

The sibling [`ghidra-appotech-jt5295`](https://github.com/matu6968/ghidra-appotech-jt5295) repository adds `OpenRISC:LE:32:default` for the camera's little-endian ORBIS32 instruction words. Build and install it for the Ghidra version in use, then restart Ghidra before importing raw binaries. The current Ghidra 12.1.2 installation and this project's BootROM and SPI image have been checked with that language.

## Flashing

Reading and rewriting the SPI NOR outside a running SDK goes through one payload,
[`appotech-sdk/boot/flasher.c`](https://github.com/matu6968/appotech-sdk/blob/main/boot/flasher.c). It carries its own
SPI driver (controller registers only, no vendor routines), so it works on a stock
camera and after the vendor firmware has been replaced. Build it once:

```sh
make -C ../appotech-sdk/boot     # -> ../appotech-sdk/build/flasher/flasher.bin
```

The image is position independent: the build links it at both load addresses and
fails unless the two binaries are identical. The same file therefore runs from
either memory pool of the BootROM loader, and apxtool picks the pool:

| Pool | Loader state | Transfers | Watchdog | Commands |
| --- | --- | --- | --- | --- |
| SRAM `0x2000` | forced BootROM (USB `1908:3319`, no DDR); also works in any loader | 512 bytes | disabled by the payload, the host keeps traffic flowing | `rom-rescue`, `flash --region sram` |
| DDR `0x02180000` | DDR initialised: a stock camera after `loader start` (or `--start-loader`), or an SDK after `debug bootrom` | 32 KiB | left armed, so the camera reboots into its firmware when the session ends | `flash` (default) |

```sh
apxtool --device /dev/sg0 flash id --start-loader
apxtool --device /dev/sg0 flash read spi-backup.bin --length 0x200000 --start-loader
apxtool --device /dev/sg0 flash verify spi-backup.bin --address 0
apxtool --device /dev/sg0 flash write patch.bin --address 0x150000 --backup backup.bin --yes
apxtool --device /dev/sg0 flash erase-sector 0x152000 --backup backup.bin --yes
```

`flash read`/`write`/`erase-sector` refuse to overwrite existing files. `write` and
`erase-sector` take a fresh full-chip backup first (it must be a new path), preserve
the neighbouring bytes of every touched 4 KiB sector, skip sectors that already
match, and verify each sector by readback. `flash` checks the JEDEC ID and detected
capacity before accessing the chip. Reads support capacity codes `0x15`/`0x16`
(2/4 MiB); writes still require the original `5E4015` 2 MiB device.
The DDR pool is shared with the stock firmware,
so the payload window `0x02180000..0x0218ffff` must read as zeros before a session
and is zeroed again afterwards; anything else aborts the session untouched. A
changed RAM layout still needs a power cycle between sessions.

From a camera stuck in the forced BootROM (hold the SPI-flash short, see
[SPI boot recovery](https://github.com/matu6968/appotech-sdk/blob/main/research/spi-boot-recovery.md)) use `rom-rescue`,
which waits for the BootROM, runs the payload from SRAM and survives its repeated
resets:

```sh
apxtool rom-rescue jedec                 # read-only: JEDEC, boot header, diagnostic SPRs
apxtool rom-rescue dump chip.bin         # read the whole detected 2/4 MiB chip + chip.bin.json
apxtool rom-rescue dump chip.bin --verify # save, then independently reread in the same session
apxtool rom-rescue verify chip.bin       # independent full-chip reread, exact capacity required
apxtool rom-rescue trace                 # decode the SDK's cold-boot flight recorder
apxtool rom-rescue marks                 # the production build's SRAM boot breadcrumbs
apxtool rom-rescue pm                    # PM chip registers (read-only)
apxtool rom-rescue --yes pm --apply      # run the stock power sequence (no flash access)
apxtool rom-rescue --yes restore image.bin --backup pre-restore.bin
```

The [Silvergear printer-camera backup procedure](docs/silvergear-printer-backup.md)
uses battery-powered forced loader entry and `--spi-init sdk`: hardware inspection
found SPI disabled in the initial loader state. Its JEDEC ID is `944016`, and a
full 4 MiB dump matched an independent reread. Newly supported chips are read-only. Dump
metadata records the chip ID, capacity, image/payload hashes and boot header.
Program/erase and restore remain restricted to the original `5e4015` 2 MiB chip.

Status: the SRAM pool runs the same SPI sequence as the earlier BootROM recovery
payload, which was verified on hardware. The DDR pool and the payload-side
data-cache maintenance are covered by host models only (`tests/test_flasher.py`,
`appotech-sdk/tools/test_flasher_model.py`) and have not yet been exercised on a
camera; run `flash id --start-loader` before relying on it.

Measurements of the camera's memory and clocks, with the evidence files they cite,
are in [docs/hardware-report.md](docs/hardware-report.md) and
[docs/loader-research.md](docs/loader-research.md). The other `docs/` reports are the
historical record of the clock experiments; the `hw` and `payload` commands they use
were removed with the vendor-callback flasher.

The tool needs no OpenRISC toolchain of its own: the SDK's `or1k-appotech-elf`
toolchain builds the payload.

### SDK SD files and formatting

Build the sibling SDK with `make -C ../appotech-sdk EXAMPLE=sd-demo`, launch
its manifest, then access the card through the independent SD/FatFs driver:

```sh
apxtool --device /dev/sg0 sdk launch ../appotech-sdk/build/sd-demo.manifest.json
apxtool debug --port 7-1 sd volume
apxtool debug --port 7-1 sd list 0:/
apxtool debug --port 7-1 sd get 0:/PHOTO/PHO00000.JPG photo.jpg
apxtool debug --port 7-1 sd read-sector 0 --count 8 --output sectors.bin
apxtool debug --port 7-1 sd put notes.txt 0:/notes.txt
apxtool debug --port 7-1 sd put notes.txt 0:/notes.txt --overwrite
apxtool debug --port 7-1 sd mkdir 0:/Documents
apxtool debug --port 7-1 sd rename 0:/notes.txt 0:/Documents/notes.txt
apxtool debug --port 7-1 sd label "APX SDK"
apxtool debug --port 7-1 sd sync
```

Paths are UTF-8, including long filenames. Downloads create new host files;
uploads create new card files unless `--overwrite` is specified. `sd unlink`
removes a file or empty directory. `sd format --filesystem fat` destroys the
existing volume and creates FAT12/16; `fat32`, `exfat` and `auto` are supported.
Formatting defaults to a partition table; `--superfloppy` omits it and
`--cluster-bytes` sets the allocation unit. Format timeout is extended to two
minutes. Raw `sd write-sector SECTOR INPUT` unmounts first and writes complete
512-byte sectors in bounded chunks. `sd info` reports reads, writes, errors and write protection,
and `sd codepage 932` selects an OEM short-name codepage. See the sibling
[SDK documentation](https://github.com/matu6968/appotech-sdk/blob/main/README.md) for configuration and coverage.

### SDK buttons and blue status LED

The sibling `gpio-demo` includes GPIO, labeled button tiles, SD read/write/format and
USB debugging. Build with `make -C ../appotech-sdk EXAMPLE=gpio-demo`, unmount
camera volumes, then launch and inspect the controls:

```sh
apxtool --device /dev/sg0 sdk launch ../appotech-sdk/build/gpio-demo.manifest.json
apxtool debug --port 7-1 gpio info
apxtool debug --port 7-1 gpio watch --seconds 30
apxtool debug --port 7-1 gpio led on
apxtool debug --port 7-1 gpio led off
```

Events report SHUTTER, DOWN, UP, LEFT, RIGHT and POWER. GPIO operation 6 adds
status, LED control and queued press/release events without changing the APXD
header. `gpio-demo` powers off after a two-second Power hold; applications choose
their own policy. The board extension requires an example that
initializes `apx_buttons_init`; USB-only/display-only builds return unsupported.

### SDK watchdog and power control

All SDK examples expose the SYSTEM extension. Automatic mode feeds during SDK
polling; manual mode requires explicit application/host feeds. Selectors 4–7
expire at approximately 0.46/1.82/7.31/29.10 seconds on the tested camera.

```sh
apxtool debug --port 7-1 system info
apxtool debug --port 7-1 system watchdog start --selector 6 --manual
apxtool debug --port 7-1 system watchdog feed
apxtool debug --port 7-1 system watchdog stop
apxtool debug --port 7-1 system watchdog start --selector 5
apxtool debug --port 7-1 system reboot
# Relaunch the SDK before the next command.
apxtool debug --port 7-1 system power-off
```

Reboot and power-off sync/unmount SD before ACK and refuse open file/directory
handles. The host reports acknowledgement; firmware tears down peripherals
and USB afterward. Reboot starts the stock SPI firmware. Power-off follows the
board's stock power-domain sequence, and Power can boot it again. USB/charging
standby power may remain. See [SDK evidence](https://github.com/matu6968/appotech-sdk/blob/main/research/watchdog-power.md).

### SDK JPEG and image preview

Build the sibling `jpeg-demo` for SD preview and sample encoding. The engine
supports baseline 4:2:0/4:2:2 decode to display-sized NV21 and baseline 4:2:0
encode up to 2048×1536. The SD preview service uses bundled stb_image for
eight-bit progressive, 4:4:4 and grayscale JPEG, plus PNG, BMP, TGA, GIF first
frame and binary PNM. Large JPEGs can use temporary SD coefficient files;
other formats must fit the bounded workspace. Preview
preserves aspect ratio; sample encoding creates a new file and refuses existing
names. Commands wait for asynchronous completion while USB stays responsive.

```sh
make -C ../appotech-sdk EXAMPLE=jpeg-demo
apxtool --device /dev/sg0 sdk launch ../appotech-sdk/build/jpeg-demo.manifest.json --reclaim-jpeg-ram
apxtool debug --port 5-1 jpeg info
apxtool debug --port 5-1 jpeg preview 0:/SDKJPEG/BASELINE/jpeg-test-220x176.jpg
apxtool debug --port 5-1 jpeg preview 0:/jpeg-test-220x176.jpg
apxtool debug --port 5-1 jpeg preview 0:/picture.png
apxtool debug --port 5-1 jpeg encode-sample 0:/SDKJPEG/ENCODER/new-3m.jpg 2048 1536
apxtool debug --port 5-1 sd get 0:/SDKJPEG/ENCODER/new-3m.jpg new-3m.jpg
apxtool debug --port 5-1 jpeg cancel
```

Replace the port/path with the current device. From a fresh boot,
`--reclaim-jpeg-ram` backs up occupied vendor RAM in the fixed codec workspace
beside the manifest before clearing it. Other RAM reservations still require
zero preflight; cleanup stops at takeover. The sample uses a native 220×176
NV21 pattern scaled in hardware; the current encoder rejects downscaling.
See [SDK JPEG usage](https://github.com/matu6968/appotech-sdk/blob/main/README.md#hardware-jpeg) and
[hardware evidence](https://github.com/matu6968/appotech-sdk/blob/main/research/jpeg.md).

### SDK UART

The exposed camera TX pad uses PA7 / route 6. Physical transmit has been
verified at 9600, 115200 and 230400 baud, 8N1; RX is model-tested only because
the exposed pad is TX-only. UART stays disabled until explicitly configured.

```sh
apxtool debug --port 7-1 uart configure --baud 115200 --tx-route 6
apxtool debug --port 7-1 uart write message.bin
apxtool debug --port 7-1 uart console on
apxtool debug --port 7-1 uart info
apxtool debug --port 7-1 uart console off
apxtool debug --port 7-1 uart stop
```

`--rx-route N` explicitly enables receive sampling on a board-specific mux
route; `uart read output.bin --length 4096` drains the receive queue. The
optional stdout/stderr mirror preserves the USB log ring and reports dropped
bytes. See [implementation and validation](https://github.com/matu6968/appotech-sdk/blob/main/research/progressive-uart.md).

### SDK speaker output

The sibling `audio-demo` adds independent speaker DMA output while retaining
JPEG/display, SD, GPIO, USB debugging and power control. PCM is signed 16-bit
little-endian mono, at 8/16/32 kHz. WAV playback accepts the same uncompressed
format and up to 65504 data bytes; existing card files are only read.

```sh
make -C ../appotech-sdk EXAMPLE=audio-demo
apxtool --device /dev/sg0 sdk launch ../appotech-sdk/build/audio-demo.manifest.json --reclaim-jpeg-ram
apxtool debug --port 5-1 audio volume 60
apxtool debug --port 5-1 audio tone --frequency 440 --milliseconds 1000
apxtool debug --port 5-1 audio wav 0:/SDKAudio/example.wav
apxtool debug --port 5-1 audio pcm sample.s16le --rate 16000
apxtool debug --port 5-1 audio mute on
apxtool debug --port 5-1 audio mute off
apxtool debug --port 5-1 audio stop
apxtool debug --port 5-1 audio info
```

Use the actual device path/port and a fresh boot for a changed RAM layout.
PCM upload uses bounded APXD chunks with exclusive staging-buffer ownership;
playback commands wait for completion while device polling continues. See
[SDK audio usage](https://github.com/matu6968/appotech-sdk/blob/main/README.md#speaker-audio).

The `microphone-demo` adds independent PCM16 mono capture at 8/16/32 kHz,
with simultaneous speaker output in separate DMA buffers. Captures use a
guarded 64 KiB workspace and remain available until stopped or replaced.

```sh
apxtool --device /dev/sg0 sdk launch ../appotech-sdk/build/microphone-demo.manifest.json --reclaim-jpeg-ram
apxtool debug --port 5-1 microphone capture --bytes 64000 --rate 16000 --output recording.wav
apxtool debug --port 5-1 microphone save 0:/SDKAudio/recording.wav
apxtool debug --port 5-1 microphone get another-copy.wav
apxtool debug --port 5-1 microphone play
apxtool debug --port 5-1 microphone info
apxtool debug --port 5-1 microphone stop
```

Capture sizes are 32..65536 bytes, aligned to 32. Existing WAV files are
refused on the host and card. This is finite capture, with no continuous
streaming, stereo, gain/AGC controls or compressed recording. See
[SDK microphone usage](https://github.com/matu6968/appotech-sdk/blob/main/README.md#microphone-capture).


## SDK camera

The `camera-demo` RAM image provides independent sensor capture and hardware-scaled
DMA preview using two guarded buffers. At the default 144 MHz clock, preview
sustained 12.39 frames/s with speaker, microphone and SD activity; INFO exposes
DMA mode, buffer guards and the PLL profile.
Use `apxtool debug camera info`, `camera capture --output photo.jpg --save 0:/photo.jpg`,
`camera raw frame.nv21`, `camera preview on/off`, or `camera stop`.
Capture/get refuse existing host files, and SD saves create new files only.
See [camera API, protocol and evidence](https://github.com/matu6968/appotech-sdk/blob/main/research/camera.md) for
supported sizes, native VGA limitations and launch instructions.

SDK RAM images now default to the accepted approximately 144 MHz PLL1 profile,
selected inside SDK startup. `make -C appotech-sdk EXAMPLE=camera-demo CPU_MHZ=96`
retains the loader baseline. The manifest records the fixed policy and SDK INFO
reports its measured relative loop timing. SDK recovery restores the loader pair
before BootROM entry. See [SDK clocks](https://github.com/matu6968/appotech-sdk/blob/main/research/clock.md).

The SDK camera example also provides calibrated battery indication and an
independent SPI NOR reader (the existing SCSI flash workflows are retained):

```sh
apxtool debug --port 5-1 battery
apxtool debug --port 5-1 flash info
apxtool debug --port 5-1 flash read spi-backup.bin
```

SDK flash reads refuse existing output files. Native SDK C page-program and
sector-erase routines are model-tested; APXD exposes reads only and the original
SPI firmware was preserved during bring-up. See
[SDK battery/SPI evidence](https://github.com/matu6968/appotech-sdk/blob/main/research/battery-spi.md).

## License

MIT. `scsiio` is part of [jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool/) (MIT, Andrey Grigoryev) and is vendored in this project with its imports made package-relative; its license is preserved in [`src/apxtool/scsiio/LICENSE`](src/apxtool/scsiio/LICENSE).
