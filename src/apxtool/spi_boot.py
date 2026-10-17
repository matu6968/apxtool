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

def build_image(original,manifest_path):
    if not isinstance(original,bytes) or len(original)!=CAPACITY:
        raise ValueError('original backup must be exactly 2 MiB')
    if hashlib.sha256(original[:PREFIX_SIZE]).hexdigest()!=PREFIX_SHA256:
        raise ValueError('unrecognized board bootstrap; refusing to patch boot instructions')
    manifest,segments=load_manifest(manifest_path)
    entry=manifest.get('spi_entry')
    if type(entry)!=int or entry%4 or not any(a<=entry<a+len(data) for a,data in segments):
        raise ValueError('manifest lacks a valid SPI entry; rebuild the SDK')
    overlays=sorted((a,data) for a,data in segments if a>=MAX_LOAD_END)
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
    if PREFIX_SIZE+size>CAPACITY:raise ValueError('SDK image exceeds SPI capacity')
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
    # SRAM bootstrap 0x328/0x32c (SPI 0x528/0x52c) loads its final call target.
    struct.pack_into('<II',image,0x528,0x18400000|(entry>>16),0xa8420000|(entry&0xffff))
    changes=[{'offset':a,'before':original[a:a+n].hex(),'after':image[a:a+n].hex()}
             for a,n in ((0x20,4),(0x28,4),(0x528,8))]
    report={'schema_version':1,'application':Path(manifest_path).stem,
        'build_id':manifest['build_id'],'entry':entry,'load_address':LOAD_BASE,
        'load_sectors':size//512,'load_bytes':size,'payload_sha256':hashlib.sha256(flat).hexdigest(),
        'original_sha256':hashlib.sha256(original).hexdigest(),
        'image_sha256':hashlib.sha256(image).hexdigest(),'bytes':len(image),
        'bootstrap':'preserved vendor board RAM/PLL calibration; three patched fields',
        'bootstrap_original_sha256':PREFIX_SHA256,'bootstrap_changes':changes,
        'sdk_manifest_sha256':hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
        'overlays':[{'flash_offset':o,'address':a,'bytes':n,'crc32':c} for o,a,n,c in placed],
        'power_loss_atomic':False,'cold_boot_hardware_verified':False}
    return bytes(image),report

def package(original,manifest,output):
    output=Path(output);metadata=output.with_suffix(output.suffix+'.json')
    if output.exists() or metadata.exists():raise FileExistsError('image/metadata must be new')
    image,report=build_image(Path(original).read_bytes(),manifest)
    with output.open('xb') as stream:stream.write(image)
    with metadata.open('x') as stream:json.dump(report,stream,indent=2);stream.write('\n')
    return report
