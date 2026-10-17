"""Hash-checked RAM takeover. After takeover, SCSI cleanup is forbidden."""
import hashlib
import json
from pathlib import Path
from .devices import enter_loader_with_device
from .debug import wait_for_sdk
from .errors import ApxError,UnsafeOperation
from .protocol import build_ram_transfer_cdb,RAM_TRANSFER_FUNCTION
from .ram import read_ram,write_ram_experimental
from .transport import require_complete

RESERVATIONS=[[0x02180000,0x20000],[0x02200000,0x4000]]
DISPLAY_RESERVATIONS=RESERVATIONS+[[0x02210000,0x10000]]
FATFS_RESERVATION=[0x02220000,0xc0000]
STORAGE_RESERVATIONS=RESERVATIONS+[FATFS_RESERVATION]
DISPLAY_STORAGE_RESERVATIONS=DISPLAY_RESERVATIONS+[FATFS_RESERVATION]
JPEG_RESERVATION=[0x022e0000,0x120000]
BASE_RESERVATIONS=(RESERVATIONS,DISPLAY_RESERVATIONS,STORAGE_RESERVATIONS,DISPLAY_STORAGE_RESERVATIONS)
ALLOWED_RESERVATIONS=BASE_RESERVATIONS+tuple(r+[JPEG_RESERVATION] for r in BASE_RESERVATIONS)
def scsi_port(path):
    for parent in (Path('/sys/class/scsi_generic')/Path(path).name/'device').resolve().parents:
        if (parent/'idVendor').exists():return parent.name
    raise ApxError('cannot resolve SCSI path to a physical USB port')
def load_manifest(path):
    path=Path(path).resolve();manifest=json.loads(path.read_text())
    reservations=manifest.get('reservations')
    if manifest.get('schema_version')!=1 or reservations not in ALLOWED_RESERVATIONS:
        raise ValueError('unsupported SDK reservations/schema')
    if manifest.get('protocol')!=1:raise ValueError('unsupported SDK protocol')
    for name in ('vid','pid'):
        if type(manifest.get(name))!=int or not 0<=manifest[name]<=0xffff:raise ValueError(f'invalid USB {name}')
    if not isinstance(manifest.get('build_id'),str) or not manifest['build_id']:raise ValueError('missing build identifier')
    if manifest.get('clock_policy') not in ('inherit-loader-96','sdk-pll1-144'):raise ValueError('unsupported SDK clock policy')
    images=[];ranges=[]
    for segment in manifest['segments']:
        name=segment['file']
        if Path(name).name!=name:raise ValueError('segment path must be a sibling filename')
        data=(path.parent/name).read_bytes();address=segment['address'];size=segment['memory_bytes']
        if type(address)!=int or type(size)!=int or address<0 or size<0 or address+size>0x100000000:
            raise ValueError('invalid SDK segment address/size')
        if len(data)!=segment['bytes'] or len(data)>size or hashlib.sha256(data).hexdigest()!=segment['sha256']:
            raise ValueError('SDK segment hash/length mismatch')
        if not any(base<=address and address+size<=base+length for base,length in reservations):raise ValueError('SDK segment outside reservations')
        if any(address<end and address+size>start for start,end in ranges):raise ValueError('overlapping SDK segments')
        ranges.append((address,address+size));images.append((address,data))
    if type(manifest.get('entry'))!=int or manifest['entry']%4 or not any(address<=manifest['entry']<address+len(data) for address,data in images):raise ValueError('entry is outside loaded image')
    if not any(segment.get('flags',1)&1 and segment['address']<=manifest['entry']<segment['address']+segment['bytes'] for segment in manifest['segments']):
        raise ValueError('entry is outside executable segments')
    return manifest,images
def launch(device_path,manifest_path,timeout=20,*,reclaim_jpeg_ram=False):
    manifest,images=load_manifest(manifest_path)
    port=scsi_port(device_path)
    path,device,identity=enter_loader_with_device(device_path,timeout=timeout)
    originals=[];takeover=False
    try:
        for address,length in manifest['reservations']:
            saved=read_ram(device,address,length)
            if any(saved):
                if not reclaim_jpeg_ram or [address,length]!=JPEG_RESERVATION:
                    raise UnsafeOperation(f'SDK reservation at {address:#x} is occupied')
                # Reclaim only the fixed JPEG arena, after making a recoverable
                # copy. Other SDK reservations retain strict zero preflight.
                import time
                backup=Path(manifest_path).resolve().with_name(f'jpeg-ram-before-{time.time_ns()}.bin')
                with backup.open('xb') as output:output.write(saved)
                metadata={'address':address,'bytes':length,'sha256':hashlib.sha256(saved).hexdigest(),
                          'port':port,'build_id':manifest['build_id']}
                backup.with_suffix('.json').write_text(json.dumps(metadata,indent=2)+'\n')
                originals.append((address,saved))
                write_ram_experimental(device,address,bytes(length))
                if any(read_ram(device,address,length)):raise ApxError('JPEG RAM reclaim readback mismatch')
            else:originals.append((address,saved))
        for address,data in images:
            write_ram_experimental(device,address,data)
            if read_ram(device,address,len(data))!=data:raise ApxError('SDK upload readback mismatch')
        read_ram(device,0x02000000,0x2000)
        takeover=True
        cdb=build_ram_transfer_cdb(function_address=RAM_TRANSFER_FUNCTION,memory_address=0x02000000,callback_address=manifest['entry'])
        try:device.execute(cdb,data_in_length=16)
        except (ApxError,OSError):pass # Expected loss of the old USB transport; handshake determines success.
    finally:
        if not takeover:
            for address,data in originals:
                write_ram_experimental(device,address,data)
                if read_ram(device,address,len(data))!=data:raise ApxError('pre-takeover restoration failed')
        device.close()
    transport,info=wait_for_sdk(port,timeout=timeout,build_id=manifest['build_id'],vid=manifest['vid'],pid=manifest['pid'])
    if manifest['clock_policy']=='sdk-pll1-144':
        clock=info.get('clock')
        if (info.get('cpu_hz_estimate')!=144000000 or not isinstance(clock,dict) or
            any(clock.get(k)!=v for k,v in {'requested_mhz':144,'pll1_control':0x1123,
                                          'pll1_coefficient':0x112a8800,'error':0}.items())):
            # Takeover has happened: close only the host handle, never restore
            # callback scratch over the active SDK after a policy mismatch.
            transport.close()
            raise ApxError('SDK handshake does not match the requested 144 MHz clock profile')
    return transport,info
