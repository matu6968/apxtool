"""SPI-free Silvergear power hold and DDR verification, using SRAM callbacks."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import struct
import time

from .errors import ApxError
from .rom_rescue import loader_sg_paths, open_loader
from .silvergear import ROM_SHA256, STOCK_SHA256, _cdb, _read, _write
from .transport import inquiry, require_complete

ENTRY, CONTROL, END = 0x2000, 0x3900, 0x3c00
MAGIC = 0x50585041
GUARD_ADDRESS = 0x3700
GUARD = bytes((i * 47 + 19) & 255 for i in range(64))
SNAPSHOTS = (0x880c,0x8858,0x885c,0x8860,0x8864,0x8868,
             0x8840,0x8844,0x8848,0x884c,0x8850,0x1800,0x2000,0x88e0,
             0x9000,0x9244,0x924c,0xf800,0xf804,0xf810,0xf818,0xf81c,
             0xf840,0xf844,0xf848,0xf860,0xf88c,0xf894,0xf8a4,0xf8ac)


def default_image():
    return Path(__file__).resolve().parents[3] / 'appotech-sdk/build/flasher/silvergear-bringup.bin'


def load_probe(path=None):
    path = Path(path) if path else default_image()
    code = path.read_bytes()
    manifest = json.loads(path.with_suffix('.json').read_text())
    expected = dict(board='silvergear-3222882',abi=1,entry=ENTRY,control=CONTROL,
                    reservation_end=END,request_bytes=208)
    if any(manifest.get(k)!=v for k,v in expected.items()):
        raise ValueError('unrecognized power/DDR probe layout')
    if not code or len(code)>0xf00 or len(code)!=manifest['bytes'] or \
       hashlib.sha256(code).hexdigest()!=manifest['sha256']:
        raise ValueError('power/DDR probe binary hash/size mismatch')
    return code,manifest


class PowerSession:
    """Saves/restores SRAM; intentional PM latch/rail settings stay enabled."""
    def __init__(self,device,image=None,progress=lambda value:None):
        self.device=device;self.sequence=0;self.uploaded=False;self.last=None;self.guard_armed=False
        code,self.manifest=load_probe(image)
        if not inquiry(device).is_loader:
            raise ValueError('BootROM loader required')
        progress('loader_identity_verified')
        if hashlib.sha256(_read(device,0x100000,0x10000)).hexdigest()!=ROM_SHA256:
            raise ValueError('unrecognized BootROM; no payload uploaded')
        progress('rom_hash_verified')
        self.saved=_read(device,ENTRY,END-ENTRY)
        progress('sram_saved')
        try:
            self.uploaded=True
            _write(device,ENTRY,code)
            progress('probe_upload_verified')
            _write(device,CONTROL,bytes(208))
            progress('request_initialized')
            _write(device,GUARD_ADDRESS,GUARD)
            self.guard_armed=True
            self.call(0)
            progress('build_handshake_verified')
        except BaseException as error:
            try:self.close()
            except Exception as cleanup_error:
                raise ApxError(f'power probe startup failed: {error}; SRAM cleanup failed: {cleanup_error}') from error
            raise

    def call(self,command,*,address=0,length=0,mode=0,seed=0):
        self.sequence+=1
        # Preserve device-owned DDR-unlock and response fields at +40 onward.
        header=struct.pack('<10I',MAGIC,self.sequence,command,0xffffffff,0,0,address,length,seed,mode)
        _write(self.device,CONTROL,header)
        raw=require_complete(self.device.execute(_cdb(CONTROL,ENTRY),data_in_length=208),208,'power/DDR callback')
        words=struct.unpack_from('<48I',raw)
        if words[:3]!=(MAGIC,self.sequence,command) or words[4:6]!=(1,self.manifest['build_id']):
            raise ApxError('power/DDR build or sequence handshake mismatch')
        self.last=dict(status=words[3],bad_address=words[10],expected=words[11],actual=words[12],
                       ddr_unlocked=bool(words[14]),inherited_watchdog=words[15],
                       spr=dict(zip(SNAPSHOTS,words[16:46])),
                       pm=dict(zip(range(0x40,0x5c,2),raw[192:206])))
        if words[3]:
            raise ApxError(f'power/DDR command {command} failed: status={words[3]}, '
                           f'address={words[10]:#x}, expected={words[11]:#x}, actual={words[12]:#x}')
        if words[13]!=len(SNAPSHOTS):
            raise ApxError('power/DDR snapshot ABI mismatch')
        return self.last

    def close(self):
        if not self.uploaded:return
        try:
            if self.guard_armed and _read(self.device,GUARD_ADDRESS,len(GUARD))!=GUARD:
                raise ApxError('power/DDR callback stack guard changed')
        finally:
            _write(self.device,ENTRY,self.saved)
            self.uploaded=False

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def memory_test(session,progress=lambda value:None):
    """Separate full-range writes/reads detect aliases between blocks/halves."""
    session.call(6,seed=0x44445231)
    passes=[]
    for mode,seed in [(0,0),(0,0xffffffff),(1,0xa5a5a5a5),(1,0x5a5a5a5a)]:
        progress(f'DDR: mode {mode}, seed {seed:#010x}, full-range write then verify')
        for command in (3,4):
            for address in range(0x02000000,0x02400000,4096):
                session.call(command,address=address,length=4096,mode=mode,seed=seed)
        passes.append(dict(mode=mode,seed=seed,bytes=0x400000,verified=True))
    # Independent host reads cross-check CPU stores and the USB memory path.
    host=hashlib.sha256();expected=hashlib.sha256()
    for address in range(0x02000000,0x02400000,0x10000):
        data=require_complete(session.device.execute(_cdb(address),data_in_length=0x10000),0x10000,'DDR host readback')
        reference=b''.join(struct.pack('<I',a^0x5a5a5a5a) for a in range(address,address+len(data),4))
        host.update(data);expected.update(reference)
        if data!=reference:raise ApxError(f'DDR host readback mismatch at block {address:#x}')
    # Leave the freshly initialized RAM clear for subsequent SDK preflight.
    for command in (3,4):
        for address in range(0x02000000,0x02400000,4096):
            session.call(command,address=address,length=4096,mode=0,seed=0)
    return dict(bytes=0x400000,passes=passes,host_sha256=host.hexdigest(),
                expected_sha256=expected.hexdigest(),zeroed_and_verified=True)


def _report(result,path):
    path.write_text(json.dumps(result,indent=2)+'\n')


def run(path,stock,image,released_file,output,result,*,timeout=180,stable_seconds=15):
    from .silvergear import prepare
    def publish(phase):
        result['phase']=phase;_report(result,output)
        print(phase,flush=True)
    with open_loader(path) as device:
        with PowerSession(device,image,lambda value:publish('POWER_SETUP: '+value)) as session:
            result['manifest']=session.manifest
            result['before']=session.last
            if session.last['spr'][0x1800]&1 or session.last['spr'][0x2000]&1:
                raise ApxError('power/DDR probe requires a cold loader with caches off')
            result['latched']=session.call(1)
            publish('POWER_LATCH_READY: release the power button now; USB stays connected')
            deadline=time.monotonic()+timeout
            while not released_file.exists():
                if time.monotonic()>=deadline:raise ApxError('power release was not confirmed; DDR not attempted')
                session.call(0);time.sleep(.05)
            publish('POWER_RELEASE_CONFIRMED: checking loader stability without the button')
            deadline=time.monotonic()+stable_seconds;count=0
            while time.monotonic()<deadline:
                state=session.call(0);count+=1
                if not state['pm'][0x40]&4 or not state['pm'][0x5a]&8:
                    raise ApxError('power-hold settings changed after releasing the button')
                time.sleep(.05)
            result['power_hold_verified']=dict(seconds=stable_seconds,callbacks=count)
            result['rails']=session.call(2)
        result['power_probe_sram_restored']=True
    publish('POWER_HOLD_PASSED: running verified stock DDR calibration stage')
    result['stage']=prepare(path,stock,power_configured=True)
    publish('DDR_STAGE_RETURNED: BootROM recovered; testing all 4 MiB')
    with open_loader(result['stage']['device']) as device:
        with PowerSession(device,image) as session:
            result['after_stage']=session.last
            result['memory_test']=memory_test(session,lambda value:print(value,flush=True))
            result['final']=session.call(0)
            session.call(7,seed=result['before']['inherited_watchdog'])
            result['watchdog_policy_restored']=True
        result['ddr_probe_sram_restored']=True
        result['loader_after']=vars(inquiry(device))
    publish('COMPLETE: power hold, DDR patterns, SRAM restoration and BootROM USB verified')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock',type=Path,required=True)
    parser.add_argument('--image',type=Path,default=default_image())
    parser.add_argument('--released-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--timeout',type=float,default=180)
    args=parser.parse_args()
    stock=args.stock.read_bytes()
    if len(stock)!=0x400000 or hashlib.sha256(stock).hexdigest()!=STOCK_SHA256:
        raise ValueError('verified Silvergear stock backup required')
    load_probe(args.image)
    if args.released_file.exists():raise FileExistsError('release signal already exists')
    if args.timeout<=0 or args.timeout>600:raise ValueError('invalid wait timeout')
    with args.output.open('x') as f:f.write('{}\n')
    result=dict(flash_access='none',stock_sha256=STOCK_SHA256,phase='waiting_for_bootrom')
    _report(result,args.output)
    print('Waiting for BootROM. Hold power until POWER_LATCH_READY.',flush=True)
    deadline=time.monotonic()+args.timeout
    try:
        while time.monotonic()<deadline:
            paths=loader_sg_paths()
            if len(paths)>1:raise ApxError('multiple BootROM cameras present')
            if paths:
                result['device']=paths[0]
                run(paths[0],args.stock,args.image,args.released_file,args.output,result,timeout=args.timeout)
                break
            time.sleep(.01)
        else:raise ApxError('no BootROM camera appeared; no upload made')
    except (ApxError,OSError,ValueError,KeyboardInterrupt) as error:
        result['error']=str(error) or 'interrupted'
        _report(result,args.output)
        print('Stopped:',result['error'],flush=True)
        return 1
    finally:
        _report(result,args.output)
        print('Result:',args.output,flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
