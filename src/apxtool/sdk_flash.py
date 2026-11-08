"""Independent SDK SPI reads and backup-first, verified firmware replacement."""
import hashlib
import os
from pathlib import Path
import json
import time
import struct
from .debug import Operation,MAX_PAYLOAD
from .errors import ApxError,TransferError
CAPACITY=2*1024*1024
# Chips the SDK driver supports, by JEDEC ID: the Action camera's 2 MiB part and the Silvergear 4 MiB part.
GEOMETRIES={0x5e4015:CAPACITY,0x944016:4*1024*1024}
class FlashClient:
    chunk=MAX_PAYLOAD # largest read per backend request
    def __init__(self,debug):self.debug=debug;self._capacity=None
    def _request(self,command,payload=b''):
        return self.debug.request(Operation.SPI,address=command,length=len(payload),payload=payload)
    def info(self):
        data=json.loads(self._request(0));value=data.get('capacity',CAPACITY) # older SDK builds are 2 MiB boards
        self._capacity=value if value in GEOMETRIES.values() else CAPACITY # remembered: no extra request per read
        return data
    @property
    def capacity(self):
        # The running SDK reports its board's flash size; an SDK that does not (older builds) is a 2 MiB board.
        # Loader backends override this property.
        if self._capacity is None:self.info()
        return self._capacity
    def _limit(self):
        # Upper bound for write arguments. A backend that cannot size the chip refuses writes itself (UnsafeOperation).
        try:return self.capacity
        except ApxError:return CAPACITY
    def read(self,address,length):
        capacity=self.capacity
        if type(address)!=int or type(length)!=int or address<0 or length<0 or address>capacity or length>capacity-address:
            raise ValueError(f'SPI range exceeds the {capacity:#x}-byte device')
        result=bytearray()
        while len(result)<length:
            n=min(self.chunk,length-len(result));data=self._request(1,struct.pack('<II',address+len(result),n))
            if len(data)!=n:raise TransferError('short SPI read response')
            result.extend(data)
        return bytes(result)
    def download(self,output,address=0,length=None):
        # Refuse an existing output before any USB traffic.
        with open(output,'xb') as file:file.write(self.read(address,self.capacity-address if length is None else length))

    def program_page(self,address,data):
        if type(address)!=int or address<0 or address>=self._limit() or not isinstance(data,bytes) or not 0<len(data)<=256-address%256:
            raise ValueError('SPI program must stay within one 256-byte page')
        if self._request(2,b'APXF'+struct.pack('<I',address)+data):raise TransferError('unexpected program response')
    def erase_sector(self,address):
        if type(address)!=int or not 0<=address<self._limit() or address%4096:
            raise ValueError('SPI erase requires a 4 KiB aligned sector')
        if self._request(3,b'APXF'+struct.pack('<I',address)):raise TransferError('unexpected erase response')
    def replace(self,image,backup,*,expected_sha256,progress=None,current=None):
        """Replace a full NOR image, committing the boot header sector last.

        Power loss can interrupt sector rewriting. Keep the original backup
        and an independent recovery method available; this is not atomic.
        """
        capacity=self.capacity
        if not isinstance(image,bytes) or len(image)!=capacity:raise ValueError(f'replacement must be exactly {capacity>>20} MiB')
        if not isinstance(expected_sha256,str) or len(expected_sha256)!=64:
            raise ValueError('expected current SHA256 is required')
        try:bytes.fromhex(expected_sha256)
        except ValueError:raise ValueError('invalid SHA256') from None
        backup=Path(backup);journal=backup.with_suffix(backup.suffix+'.json')
        if backup.exists() or journal.exists():raise FileExistsError('backup/journal must be new')
        info=self.info()
        if GEOMETRIES.get(info.get('jedec'))!=capacity or info.get('host_read_only',True) or info.get('write_protocol')!=1:
            raise TransferError('SDK does not support this board/flash write protocol')
        # A caller that already read the chip (the forced-BootROM rescue) can pass
        # those bytes instead of paying for a second full read.
        if current is not None and (not isinstance(current,bytes) or len(current)!=capacity):
            raise ValueError(f'current image must be exactly {capacity>>20} MiB')
        original=current if current is not None else self.read(0,capacity);digest=hashlib.sha256(original).hexdigest()
        if digest!=expected_sha256.lower():raise TransferError('current SPI hash differs from expected; no erase performed')
        with backup.open('xb') as file:
            file.write(original);file.flush();os.fsync(file.fileno())
        if backup.read_bytes()!=original:raise TransferError('backup readback failed; no erase performed')
        state={'status':'backed_up','original_sha256':digest,'target_sha256':hashlib.sha256(image).hexdigest(),
               'bytes':capacity,'verified_sectors':[],'boot_sector_committed':False}
        def save():
            # A complete journal always precedes the next destructive command.
            temp=journal.with_suffix(journal.suffix+'.new')
            with temp.open('w') as file:
                json.dump(state,file,indent=2);file.write('\n');file.flush();os.fsync(file.fileno())
            os.replace(temp,journal)
        save()
        sectors=[a for a in range(4096,capacity,4096) if original[a:a+4096]!=image[a:a+4096]]
        if original[:4096]!=image[:4096]:sectors.append(0)
        state['changed_sectors']=sectors;state['status']='writing';save()
        previous=getattr(self.debug.transport,'timeout_ms',None)
        if previous is not None:self.debug.transport.timeout_ms=max(previous,15000)
        try:
            for address in sectors:
                self.erase_sector(address)
                for offset in range(0,4096,256):
                    page=image[address+offset:address+offset+256]
                    if page!=b'\xff'*256:self.program_page(address+offset,page)
                if self.read(address,4096)!=image[address:address+4096]:raise TransferError(f'SPI verify failed at {address:#x}')
                state['verified_sectors'].append(address)
                if address==0:state['boot_sector_committed']=True
                save()
                if progress:progress(len(state['verified_sectors']),len(sectors))
            actual=hashlib.sha256(self.read(0,capacity)).hexdigest()
            if actual!=state['target_sha256']:raise TransferError('full SPI readback hash mismatch')
            state['status']='verified';state['readback_sha256']=actual;save()
            return state
        except BaseException as error:
            state.update(status='interrupted',error=str(error));save();raise
        finally:
            if previous is not None:self.debug.transport.timeout_ms=previous
