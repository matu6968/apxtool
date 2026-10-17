"""APXD SD/FatFs read, write and format extension. Paths are UTF-8."""
import json
import struct
from .debug import Operation,MAX_PAYLOAD
from .errors import TransferError,DebugCommandError,ApxError

class StorageClient:
    def __init__(self,debug):self.debug=debug
    def _request(self,command,data=b''):
        try:return self.debug.request(Operation.STORAGE,address=command,length=len(data),payload=data)
        except DebugCommandError as exc:
            names=('OK','DISK_ERR','INT_ERR','NOT_READY','NO_FILE','NO_PATH','INVALID_NAME',
                   'DENIED','EXIST','INVALID_OBJECT','WRITE_PROTECTED','INVALID_DRIVE','NOT_ENABLED',
                   'NO_FILESYSTEM','MKFS_ABORTED','TIMEOUT','LOCKED','NOT_ENOUGH_CORE','TOO_MANY_OPEN_FILES','INVALID_PARAMETER')
            if 0x100<=exc.status<0x100+len(names):
                raise ApxError(f'FatFs: FR_{names[exc.status-0x100]}') from exc
            raise
    @staticmethod
    def _path(path):
        data=path.encode('utf-8')
        if not data or b'\0' in data or len(data)>1023:raise ValueError('invalid UTF-8 path length')
        return data+b'\0'
    def info(self):return json.loads(self._request(0))
    def sectors(self,sector,count=1):
        if not 0<=sector<=0xffffffff or not 1<=count<=8 or sector+count>0x100000000:
            raise ValueError('invalid sector/count (maximum 8 per transfer)')
        data=self._request(1,struct.pack('<II',sector,count))
        if len(data)!=count*512:raise TransferError('short SD sector response')
        return data
    def volume(self):
        data=self._request(2)
        if len(data)<13 or data[-1]!=0:raise TransferError('malformed volume response')
        kind,cluster,serial=struct.unpack_from('<III',data)
        return {'filesystem':{1:'FAT12',2:'FAT16',3:'FAT32',4:'exFAT'}.get(kind,'unknown'),
                'cluster_sectors':cluster,'serial':f'{serial:08x}','label':data[12:-1].decode('utf-8')}
    def list(self,path='0:/'):
        self.volume()
        encoded=self._path(path)
        for index in range(0x100000000):
            data=self._request(3,struct.pack('<I',index)+encoded)
            if not data:return
            if len(data)<22 or data[-1]!=0:raise TransferError('malformed directory entry')
            size,attributes,date,time=struct.unpack_from('<QIII',data)
            yield {'name':data[20:-1].decode('utf-8'),'bytes':size,'attributes':attributes,
                   'directory':bool(attributes&0x10),'fat_date':date,'fat_time':time}
    def read(self,path,offset=0,length=MAX_PAYLOAD):
        if not 0<=offset<=0xffffffffffffffff or not 0<=length<=MAX_PAYLOAD:
            raise ValueError('invalid file offset/length')
        data=self._request(4,struct.pack('<QI',offset,length)+self._path(path))
        if len(data)>length:raise TransferError('oversized file response')
        return data
    def download(self,path,output):
        offset=0
        while True:
            data=self.read(path,offset)
            if not data:return offset
            output.write(data);offset+=len(data)
            if len(data)<MAX_PAYLOAD:return offset
    def codepage(self,codepage):
        if not 0<codepage<=65535:raise ValueError('invalid OEM codepage')
        if self._request(5,struct.pack('<I',codepage)):raise TransferError('unexpected codepage payload')
    def write_sectors(self,sector,data):
        if not data or len(data)%512 or len(data)>7*512 or not 0<=sector<=0xffffffff or sector+len(data)//512>0x100000000:
            raise ValueError('invalid sector/data (1..7 complete sectors per transfer)')
        if self._request(6,struct.pack('<I',sector)+data):raise TransferError('unexpected block write payload')
    def write(self,path,data,offset=0,mode='existing'):
        modes={'existing':0,'new':1,'replace':2}
        if mode not in modes or not 0<=offset<=0xffffffffffffffff or (mode!='existing' and offset):
            raise ValueError('invalid write mode/offset')
        encoded=self._path(path)
        if len(data)>MAX_PAYLOAD-16-len(encoded):raise ValueError('file write payload exceeds 4096 bytes')
        result=self._request(7,struct.pack('<QII',offset,modes[mode],len(encoded))+encoded+data)
        if len(result)!=4:raise TransferError('malformed file write response')
        written=struct.unpack('<I',result)[0]
        if written!=len(data):raise ApxError(f'short file write: {written}/{len(data)} bytes (card may be full)')
        return written
    def upload(self,path,source,*,overwrite=False):
        self.volume();chunk_size=MAX_PAYLOAD-16-len(self._path(path));offset=0;first=True
        while True:
            data=source.read(chunk_size)
            if not data and not first:return offset
            mode=('replace' if overwrite else 'new') if first else 'existing'
            offset+=self.write(path,data,offset,mode)
            first=False
            if not data:return offset
    def mkdir(self,path):
        if self._request(8,self._path(path)):raise TransferError('unexpected mkdir payload')
    def unlink(self,path):
        if self._request(9,self._path(path)):raise TransferError('unexpected unlink payload')
    def rename(self,path,new_path):
        first=self._path(path);second=self._path(new_path)
        if self._request(10,struct.pack('<I',len(first))+first+second):raise TransferError('unexpected rename payload')
    def label(self,label):
        if self._request(11,self._path('0:'+label)):raise TransferError('unexpected label payload')
    def format(self,filesystem='fat',*,cluster_bytes=0,superfloppy=False):
        formats={'auto':7,'fat':1,'fat32':2,'exfat':4}
        maximum=0x1000000 if filesystem in ('auto','exfat') else 65536
        if filesystem not in formats or not 0<=cluster_bytes<=maximum or (cluster_bytes and
            (cluster_bytes<512 or cluster_bytes&(cluster_bytes-1))):raise ValueError('invalid format/cluster size')
        # Format can outlast the usual five-second USB request deadline.
        transport=self.debug.transport;old=getattr(transport,'timeout_ms',None)
        if isinstance(old,int):transport.timeout_ms=max(old,120000)
        try:
            if self._request(12,struct.pack('<II',formats[filesystem]|(8 if superfloppy else 0),cluster_bytes)):
                raise TransferError('unexpected format payload')
        finally:
            if isinstance(old,int):transport.timeout_ms=old
        return self.volume()
    def unmount(self):
        if self._request(13):raise TransferError('unexpected unmount payload')
    def sync(self):
        if self._request(14):raise TransferError('unexpected sync payload')
