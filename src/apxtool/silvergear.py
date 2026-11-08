"""RAM-only DDR initialization for the verified Silvergear D-426 V03 board."""
import hashlib
import struct
from pathlib import Path
from .errors import ApxError,TransferError
from .protocol import RAM_TRANSFER_FUNCTION, build_ram_transfer_cdb, RAM_WRITE_CHUNK
from .transport import LinuxSCSITransport, inquiry, require_complete

STOCK_SHA256='4874af74051372363e8c36ff99e369657648eb353950b41fe2a5a00d35acdb9e'
ROM_SHA256='f5b68ff45bff5f4f21c49008ed0a4bcbab01f2a2cb5aeff8f832ee41be460e39'
STAGE_SHA256='02dc8e97b21c688c8b361893a185c6052f3e090ff1759c05a74f736b3953ebca'
LOW, END, SAVED, ENTRY, CONTROL = 0x3e8, 0x2000, 0x2000, 0x3c20, 0x3e00

def _cdb(address,callback=0xffffffff):
    return build_ram_transfer_cdb(function_address=RAM_TRANSFER_FUNCTION,memory_address=address,callback_address=callback)

def _read(device,address,length):
    return b''.join(require_complete(device.execute(_cdb(address+i),data_in_length=min(512,length-i)),min(512,length-i),'Silvergear SRAM read') for i in range(0,length,512))

def _write(device,address,data):
    for offset in range(0,len(data),RAM_WRITE_CHUNK):
        part=data[offset:offset+RAM_WRITE_CHUNK]
        transfer=device.execute(_cdb(address+offset),data_out=part)
        if transfer.residual:raise ApxError('Silvergear SRAM upload residual')
    if _read(device,address,len(data))!=data:raise ApxError('Silvergear SRAM upload mismatch')

def prepare(device_path,stock_backup,stage=None,timeout=20,*,power_configured=False):
    import time
    from .rom_rescue import open_loader,loader_sg_paths
    from .sdk import scsi_port
    stock=Path(stock_backup).read_bytes()
    if len(stock)!=0x400000 or hashlib.sha256(stock).hexdigest()!=STOCK_SHA256:
        raise ValueError('Silvergear launch requires its verified 4 MiB stock backup')
    stage_path=Path(stage) if stage else Path(__file__).resolve().parents[3]/'appotech-sdk/build/flasher/silvergear-stage.bin'
    code=stage_path.read_bytes()
    if not code or len(code)>CONTROL-ENTRY or len(code)%4:
        raise ValueError('invalid Silvergear SRAM stage size')
    if hashlib.sha256(code).hexdigest()!=STAGE_SHA256:raise ValueError('unrecognized Silvergear SRAM stage binary')
    port=scsi_port(device_path)
    with open_loader(device_path) as device:
        identity=inquiry(device)
        if not identity.is_loader:raise ValueError('Silvergear RAM initialization requires forced BootROM mode')
        # Dedicated SRAM power payload contains no SPI driver. In particular,
        # preserve PM44 and unrelated PM5a bits, as this board's stock code does.
        if hashlib.sha256(_read(device,0x100000,0x10000)).hexdigest()!=ROM_SHA256:
            raise ValueError('Silvergear BootROM hash mismatch')
        from .silvergear_bringup import PowerSession
        with PowerSession(device) as power:
            if power_configured:
                pm=power.last['pm']
                if pm[0x40]&0x14!=0x14 or not pm[0x58]&8 or not pm[0x5a]&8:
                    raise ApxError('Silvergear DDR stage requires verified power/rail setup')
            else:
                power.call(1)
                power.call(2)
            if power.last['spr'][0x1800]&1 or power.last['spr'][0x2000]&1:
                raise ApxError('Silvergear DDR stage requires a cold uncached loader')
        original=_read(device,LOW,END-LOW)
        scratch=_read(device,SAVED,0x4000-SAVED)
        invoked=False
        try:
            _write(device,SAVED,original)
            _write(device,ENTRY,code)
            _write(device,CONTROL,struct.pack('<5I',0x47535841,0xffffffff,0,0,0))
            _write(device,LOW,stock[LOW+0x200:END+0x200])
            invoked=True
            try:device.execute(_cdb(CONTROL,ENTRY),data_in_length=16)
            except (ApxError,OSError):pass  # Stage deliberately detaches and re-enters the ROM USB initializer.
        finally:
            if not invoked:
                _write(device,LOW,original);_write(device,SAVED,scratch)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        for path in loader_sg_paths():
            try:
                if scsi_port(path)!=port:continue
                with open_loader(path) as device:
                    if not inquiry(device).is_loader:continue
                    words=struct.unpack('<5I',_read(device,CONTROL,20))
                    magic,status,control,coefficient,calibration=words
                    if (magic,status,control,coefficient)!=(0x47535841,0,0x1623,0x0b71b000):continue
                    if not 737<=calibration<=864:raise ApxError('invalid stage ADC calibration')
                    expected=bytearray(original)
                    struct.pack_into('<I',expected,0x1e14-LOW,calibration)
                    if _read(device,LOW,END-LOW)!=expected:raise ApxError('stage SRAM restoration mismatch')
                    _write(device,SAVED,scratch)
                    return dict(device=path,stock_sha256=STOCK_SHA256,stage_sha256=hashlib.sha256(code).hexdigest(),
                                ddr_initialized=True,adc_calibration=calibration)
            except (OSError,TransferError):continue
        time.sleep(.05)
    raise ApxError('DDR stage was invoked but ROM recovery was not verified; reset into BootROM before retrying')
