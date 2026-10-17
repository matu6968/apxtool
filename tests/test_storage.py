import io,json,struct
from unittest.mock import Mock
import pytest
from apxtool.debug import Operation,DebugClient,Frame,HEADER
from apxtool.storage import StorageClient
from apxtool.errors import ApxError,TransferError

def client(payload=b'',status=0):
    def exchange(data):
        _,_,op,space,width,seq,command,length,_=HEADER.unpack_from(data)
        assert op==Operation.STORAGE and length==len(data)-24
        return Frame(op,space,width,seq,command,len(payload),status,payload).encode()
    return StorageClient(DebugClient(Mock(exchange=Mock(side_effect=exchange))))

def test_storage_utf8_paths_and_64bit_file_offsets():
    s=client(b'abc');assert s.read('0:/日本語.txt',0x100000000,3)==b'abc'
    data=s.debug.transport.exchange.call_args.args[0]
    assert data[24:36]==struct.pack('<QI',0x100000000,3)
    assert data[36:]=='0:/日本語.txt\0'.encode()

def test_storage_volume_and_directory_decode():
    s=client(struct.pack('<III',4,128,0x12345678)+'日本語\0'.encode())
    assert s.volume()=={'filesystem':'exFAT','cluster_sectors':128,'serial':'12345678','label':'日本語'}
    replies=iter([struct.pack('<III',3,64,1)+b'\0',struct.pack('<QIII',0x100000000,0x20,1,2)+'long 日本語.txt\0'.encode(),b''])
    s=client();s._request=lambda *a:next(replies)
    entries=list(s.list());assert entries[0]['bytes']==0x100000000 and entries[0]['name']=='long 日本語.txt'

def test_storage_rejects_bad_ranges_paths_and_responses():
    s=client()
    for sector,count in [(-1,1),(0,0),(0,9),(0xffffffff,2)]:
        with pytest.raises(ValueError):s.sectors(sector,count)
    for path in ['', 'nul\0name', 'a'*1024]:
        with pytest.raises(ValueError):s.read(path)
    with pytest.raises(ValueError):s.read('x',-1)
    with pytest.raises(ValueError):s.read('x',length=4097)
    with pytest.raises(TransferError):s.sectors(0)
    with pytest.raises(TransferError):s.volume()
    with pytest.raises(ApxError,match='FR_NOT_READY'):client(status=0x103).volume()
    with pytest.raises(TransferError):client(b'abc').read('x',length=2)

def test_download_stops_at_eof_and_never_sends_write_operation():
    s=client();replies=iter([b'a'*4096,b'b'*13]);s.read=Mock(side_effect=lambda *a:next(replies))
    output=io.BytesIO();assert s.download('file',output)==4109
    assert output.getvalue()==b'a'*4096+b'b'*13
    assert [call.args[1] for call in s.read.call_args_list]==[0,4096]

def test_write_frames_limits_and_short_writes():
    s=client(struct.pack('<I',3));assert s.write('0:/日本語.bin',b'abc',0,'new')==3
    payload=s.debug.transport.exchange.call_args.args[0][24:]
    offset,mode,path_len=struct.unpack_from('<QII',payload)
    assert offset==0 and mode==1 and payload[16:16+path_len]=='0:/日本語.bin\0'.encode()
    assert payload[16+path_len:]==b'abc'
    for offset,mode in [(-1,'existing'),(1,'new'),(0,'invalid')]:
        with pytest.raises(ValueError):s.write('x',b'',offset,mode)
    with pytest.raises(ValueError):s.write('x',bytes(4096))
    with pytest.raises(ApxError,match='short file write'):client(struct.pack('<I',1)).write('x',b'abc')
    with pytest.raises(TransferError):client(b'bad').write('x',b'')
    with pytest.raises(ValueError):s.write_sectors(0,bytes(8*512))
    with pytest.raises(ValueError):s.write_sectors(0xffffffff,bytes(2*512))
    s=client();s.write_sectors(123,bytes(7*512))
    assert s.debug.transport.exchange.call_args.args[0][24:28]==struct.pack('<I',123)

def test_upload_chunks_creates_once_and_handles_empty_files():
    s=client();s.volume=Mock(return_value={});s.write=Mock(side_effect=lambda path,data,*a:len(data))
    data=bytes(12000);assert s.upload('0:/longname.bin',io.BytesIO(data))==len(data)
    calls=s.write.call_args_list;assert calls[0].args[3]=='new'
    assert all(c.args[3]=='existing' for c in calls[1:])
    assert all(len(c.args[1])+16+len(s._path('0:/longname.bin'))<=4096 for c in calls)
    s.write.reset_mock();assert s.upload('empty',io.BytesIO(),overwrite=True)==0
    assert s.write.call_args.args==('empty',b'',0,'replace')

def test_format_options_timeout_restoration_and_metadata():
    s=client();s.debug.transport.timeout_ms=5000;s.volume=Mock(return_value={'filesystem':'FAT32'})
    original=s._request
    def request(command,data=b''):
        assert s.debug.transport.timeout_ms==120000
        return original(command,data)
    s._request=request
    assert s.format('fat32',cluster_bytes=4096,superfloppy=True)['filesystem']=='FAT32'
    assert s.debug.transport.timeout_ms==5000
    assert s.debug.transport.exchange.call_args.args[0][24:]==struct.pack('<II',10,4096)
    for filesystem,size in [('bad',0),('fat',3),('fat',128*1024),('exfat',32*1024*1024)]:
        with pytest.raises(ValueError):s.format(filesystem,cluster_bytes=size)
    s=client(status=0x10e);s.debug.transport.timeout_ms=5000
    with pytest.raises(ApxError):s.format()
    assert s.debug.transport.timeout_ms==5000
    s=client();s.mkdir('0:/x');s.rename('0:/x','0:/y');s.unlink('0:/y');s.label('SDK TEST');s.sync();s.unmount()
    assert s.debug.transport.exchange.call_count==6
