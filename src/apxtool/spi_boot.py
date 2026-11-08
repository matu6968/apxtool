"""Board-specific BLDR image packaging; preserves the proven SRAM bootstrap."""
import hashlib
import json
import struct
import zlib
from pathlib import Path
from .sdk import load_manifest
from .sdk_flash import CAPACITY

PREFIX_SIZE=0x2200
PREFIX_SHA256='67d86cda9d16c86a9fba418cfd984b0369bdbf860864fae617bd1e9f5e24464c'
LOAD_BASE=0x02180000
MAX_LOAD_END=0x022e0000
# Segments above MAX_LOAD_END (the codec-RAM bank, used by the LVGL port) are not loaded by the BootROM: it
# would need a 2.4 MB image and the vendor bootstrap's DDR calibration owns that range. The packer stores them
# in free flash instead, with a table at OVERLAY_TABLE that SDK code (apx_spi_overlay_load) copies into place.
OVERLAY_END=0x02400000
OVERLAY_TABLE=0x1fc000
OVERLAY_MAGIC=0x4f585041 # 'APXO'
OVERLAY_MAX=15

def overlay_table(entries):
    body=b''.join(struct.pack('<IIII',*e) for e in entries)
    return struct.pack('<IIII',OVERLAY_MAGIC,1,len(entries),zlib.crc32(body))+body

# Silvergear 3222882 (4 MiB, JEDEC 944016). The BootROM is byte-identical to the Action camera's and the BLDR header
# differs only in stock sizes and two hook constants, so the same scheme applies. Its bootstrap calls the app at
# SRAM 0x340 (SPI 0x540: `l.movhi r2,0x200; l.ori r2,r2,0x5ac`, the stock startup 0x020005ac) after an extra UART/GPIO
# setup call, so the patch site differs. Overlays are not supported: the Action table sector (0x1fc000) holds stock
# resources on this flash.
SILVERGEAR_CAPACITY=0x400000
SILVERGEAR_PREFIX_SHA256='626073ae3ed84a0bb6fc7c67305799d74f2bac8f1ba98a20fe51b455edc087dd'
SILVERGEAR_ENTRY_PATCH=0x540
SILVERGEAR_ENTRY_STOCK=bytes.fromhex('00024018ac0542a8')
BOARDS=('action','silvergear')

def _profile(board):
    if board=='action':
        return dict(capacity=CAPACITY,prefix_sha=PREFIX_SHA256,entry_patch=0x528,entry_stock=None,overlays=True,label='2 MiB')
    if board=='silvergear':
        return dict(capacity=SILVERGEAR_CAPACITY,prefix_sha=SILVERGEAR_PREFIX_SHA256,entry_patch=SILVERGEAR_ENTRY_PATCH,
                    entry_stock=SILVERGEAR_ENTRY_STOCK,overlays=False,label='4 MiB')
    raise ValueError(f'unknown board {board!r}; expected one of {BOARDS}')

def build_image(original,manifest_path,board='action'):
    profile=_profile(board);capacity=profile['capacity']
    if not isinstance(original,bytes) or len(original)!=capacity:
        raise ValueError(f"original backup must be exactly {profile['label']}")
    if hashlib.sha256(original[:PREFIX_SIZE]).hexdigest()!=profile['prefix_sha']:
        raise ValueError('unrecognized board bootstrap; refusing to patch boot instructions')
    stock_hook=profile['entry_stock']
    patch=profile['entry_patch']
    if stock_hook is not None and original[patch:patch+len(stock_hook)]!=stock_hook:
        raise ValueError('bootstrap application hook is not the reviewed stock instruction pair')
    manifest,segments=load_manifest(manifest_path)
    entry=manifest.get('spi_entry')
    if type(entry)!=int or entry%4 or not any(a<=entry<a+len(data) for a,data in segments):
        raise ValueError('manifest lacks a valid SPI entry; rebuild the SDK')
    overlays=sorted((a,data) for a,data in segments if a>=MAX_LOAD_END)
    if overlays and not profile['overlays']:
        raise ValueError(f'{board} SPI images do not support overlay segments')
    segments=[(a,data) for a,data in segments if a<MAX_LOAD_END]
    if not segments or min(a for a,_ in segments)!=LOAD_BASE or any(a+len(data)>MAX_LOAD_END for a,data in segments):
        raise ValueError('SPI image supports the camera/JPEG RAM layout only')
    declared={s.get('address'):s for s in manifest.get('segments',[]) if isinstance(s,dict)}
    for a,data in overlays:
        info=declared.get(a,{})
        if not data or a+len(data)>OVERLAY_END or len(overlays)>OVERLAY_MAX or info.get('memory_bytes',len(data))!=len(data):
            raise ValueError('SPI overlay segments must be fully initialised data inside the codec-RAM bank')
    for (a,d),(b,_) in zip(overlays,overlays[1:]):
        if a+len(d)>b:raise ValueError('SPI overlay segments overlap')
    end=max(a+len(data) for a,data in segments)
    size=(end-LOAD_BASE+511)&~511
    if PREFIX_SIZE+size>capacity:raise ValueError('SDK image exceeds SPI capacity')
    flat=bytearray(size)
    for address,data in segments:flat[address-LOAD_BASE:address-LOAD_BASE+len(data)]=data
    image=bytearray(original)
    image[PREFIX_SIZE:PREFIX_SIZE+size]=flat
    placed=[];cursor=(PREFIX_SIZE+size+4095)&~4095
    for address,data in overlays:
        if cursor+len(data)>OVERLAY_TABLE:raise ValueError('SPI overlay exceeds the free flash below the overlay table')
        image[cursor:cursor+len(data)]=data
        placed.append((cursor,address,len(data),zlib.crc32(data)));cursor=(cursor+len(data)+4095)&~4095
    if placed:
        if any(b!=0xff for b in original[OVERLAY_TABLE:OVERLAY_TABLE+4096]):
            raise ValueError('overlay table sector of the backup is not erased')
        table=overlay_table(placed);image[OVERLAY_TABLE:OVERLAY_TABLE+len(table)]=table
    # Keep the header hook, media parameters and SRAM bootstrap entry at zero.
    # The ROM reads sector 17 onwards into DDR, then executes the SRAM entry.
    struct.pack_into('<I',image,0x20,LOAD_BASE)
    struct.pack_into('<I',image,0x28,size//512)
    # Action: SRAM bootstrap 0x328/0x32c (SPI 0x528/0x52c) loads its final call target (`movhi r2` / `ori r2,r2`).
    # Silvergear: the same instruction pair at SRAM 0x340/0x344 (SPI 0x540/0x544).
    struct.pack_into('<II',image,patch,0x18400000|(entry>>16),0xa8420000|(entry&0xffff))
    changes=[{'offset':a,'before':original[a:a+n].hex(),'after':image[a:a+n].hex()}
             for a,n in ((0x20,4),(0x28,4),(patch,8))]
    report={'schema_version':1,'application':Path(manifest_path).stem,
        'build_id':manifest['build_id'],'entry':entry,'load_address':LOAD_BASE,
        'load_sectors':size//512,'load_bytes':size,'payload_sha256':hashlib.sha256(flat).hexdigest(),
        'original_sha256':hashlib.sha256(original).hexdigest(),
        'image_sha256':hashlib.sha256(image).hexdigest(),'bytes':len(image),
        'bootstrap':'preserved vendor board RAM/PLL calibration; three patched fields',
        'board':board,'bootstrap_original_sha256':profile['prefix_sha'],'bootstrap_changes':changes,
        'sdk_manifest_sha256':hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
        'overlays':[{'flash_offset':o,'address':a,'bytes':n,'crc32':c} for o,a,n,c in placed],
        'power_loss_atomic':False,'cold_boot_hardware_verified':False}
    return bytes(image),report

def package(original,manifest,output,board='action'):
    output=Path(output);metadata=output.with_suffix(output.suffix+'.json')
    if output.exists() or metadata.exists():raise FileExistsError('image/metadata must be new')
    image,report=build_image(Path(original).read_bytes(),manifest,board)
    with output.open('xb') as stream:stream.write(image)
    with metadata.open('x') as stream:json.dump(report,stream,indent=2);stream.write('\n')
    return report
