"""APXD v1 bulk debugging; independent of the SCSI callback protocol."""
from dataclasses import dataclass
from enum import IntEnum
import json
import struct
import time
from .errors import ApxError, DeviceError, TransferError,DebugCommandError

HEADER=struct.Struct('<4sBBBBIIII')
MAX_PAYLOAD=4096
RAW=0x80
class Operation(IntEnum):
    INFO=0; READ=1; WRITE=2; BOOTROM=3; LOG=4; STORAGE=5; GPIO=6; SYSTEM=7; JPEG=8; AUDIO=9; MICROPHONE=10; CAMERA=11; SPI=12; BATTERY=13; UART=14
class Space(IntEnum):
    MEMORY=0; MMIO=1; SPR=2
@dataclass(frozen=True)
class Frame:
    operation: int
    space: int=0
    width: int=1
    sequence: int=0
    address: int=0
    length: int=0
    status: int=0
    payload: bytes=b''
    def encode(self):
        if not 0<=self.length<=MAX_PAYLOAD:raise ValueError('APXD length exceeds 4096')
        for value in (self.sequence,self.address,self.status):
            if not 0<=value<=0xffffffff:raise ValueError('APXD integer exceeds 32 bits')
        return HEADER.pack(b'APXD',1,self.operation,self.space,self.width,
                           self.sequence,self.address,self.length,self.status)+self.payload
    @classmethod
    def decode(cls,data):
        if len(data)<HEADER.size:raise TransferError('short APXD header')
        magic,version,operation,space,width,sequence,address,length,status=HEADER.unpack_from(data)
        if magic!=b'APXD' or version!=1:raise TransferError('invalid APXD magic/version')
        if length>MAX_PAYLOAD or len(data)!=HEADER.size+length:raise TransferError('APXD response length mismatch')
        return cls(operation,space,width,sequence,address,length,status,data[HEADER.size:])

def physical_port(device):
    return f'{device.bus}-'+'.'.join(map(str,device.port_numbers or []))

class USBTransport:
    def __init__(self,port=None,vid=0x1908,pid=0x3319,timeout_ms=5000):
        try:import usb.core,usb.util
        except ImportError as exc:raise ApxError('install apxtool[usb] for SDK debugging') from exc
        candidates=[]
        for dev in usb.core.find(find_all=True,idVendor=vid,idProduct=pid):
            if port is not None and physical_port(dev)!=port:continue
            try:
                config=dev.get_active_configuration()
                for interface in config:
                    endpoints={e.bEndpointAddress:e for e in interface}
                    if interface.bInterfaceClass==0xff and set(endpoints)=={1,0x81}:
                        if all((e.bmAttributes&3)==2 for e in endpoints.values()):
                            candidates.append((dev,interface.bInterfaceNumber))
            except usb.core.USBError as exc:
                raise DeviceError(f'USB access failed: {exc}') from exc
        if len(candidates)!=1:raise DeviceError(f'expected one SDK bulk interface, found {len(candidates)}')
        self.device,self.interface=candidates[0]
        self.util=usb.util;self.timeout_ms=timeout_ms
        usb.util.claim_interface(self.device,self.interface)
        self.port=physical_port(self.device)
    def exchange(self,data):
        try:
            count=self.device.write(1,data,timeout=self.timeout_ms)
            if count!=len(data):raise TransferError('short USB write')
            return bytes(self.device.read(0x81,HEADER.size+MAX_PAYLOAD,timeout=self.timeout_ms))
        except OSError as exc:raise TransferError(f'USB transfer failed: {exc}') from exc
    def close(self):
        try:self.util.release_interface(self.device,self.interface)
        except OSError:pass
        finally:self.util.dispose_resources(self.device)
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

class DebugClient:
    def __init__(self,transport):self.transport=transport;self.sequence=0
    def request(self,operation,*,space=Space.MEMORY,width=1,address=0,length=0,payload=b'',raw=False):
        self.sequence=(self.sequence+1)&0xffffffff
        frame=Frame(operation,space,width|(RAW if raw else 0),self.sequence,address,length,0,payload)
        reply=Frame.decode(self.transport.exchange(frame.encode()))
        if (reply.operation,reply.space,reply.width,reply.sequence,reply.address)!=(frame.operation,frame.space,frame.width,frame.sequence,frame.address):
            raise TransferError('APXD response does not match request')
        if reply.status:raise DebugCommandError(reply.status)
        return reply.payload
    def info(self):
        result=json.loads(self.request(Operation.INFO))
        if result.get('protocol')!=1 or result.get('max_payload')!=MAX_PAYLOAD:raise TransferError('unsupported SDK capabilities')
        return result
    def read(self,address,length,*,space=Space.MEMORY,width=1):
        self._range(address,length,space,width)
        output=bytearray()
        while len(output)<length:
            count=min(MAX_PAYLOAD,length-len(output))
            data=self.request(Operation.READ,space=space,width=width,address=address+len(output),length=count)
            if len(data)!=count:raise TransferError('short APXD read')
            output.extend(data)
        return bytes(output)
    def write(self,address,data,*,space=Space.MEMORY,width=1,raw=False):
        self._range(address,len(data),space,width)
        for offset in range(0,len(data),MAX_PAYLOAD):
            block=data[offset:offset+MAX_PAYLOAD]
            if self.request(Operation.WRITE,space=space,width=width,address=address+offset,
                            length=len(block),payload=block,raw=raw):raise TransferError('unexpected WRITE payload')
    @staticmethod
    def _range(address,length,space,width):
        if width not in (1,2,4) or address<0 or length<0 or address+length>0x100000000 or (space!=Space.SPR and address%width) or length%width:
            raise ValueError('invalid range or access width/alignment')
        if space==Space.SPR and (width!=4 or length!=4 or address>0xffff):raise ValueError('SPR access requires one 32-bit register')
    def logs(self):return self.request(Operation.LOG,length=MAX_PAYLOAD)
    def bootrom(self):
        if self.request(Operation.BOOTROM):raise TransferError('unexpected BOOTROM payload')

def wait_for_sdk(port,timeout=20,build_id=None,vid=0x1908,pid=0x3319):
    deadline=time.monotonic()+timeout
    last=None
    while time.monotonic()<deadline:
        transport=None
        try:
            transport=USBTransport(port=port,vid=vid,pid=pid,timeout_ms=1000)
            info=DebugClient(transport).info()
            if build_id is not None and info.get('build_id')!=build_id:raise DeviceError('SDK build identifier mismatch')
            # The short deadline is for readiness probes, not normal SDK jobs.
            transport.timeout_ms=5000
            return transport,info
        except (ApxError,OSError,ValueError) as exc:
            last=exc
            if transport is not None:transport.close()
            time.sleep(.1)
    raise DeviceError(f'SDK did not become ready at {port}: {last}')
