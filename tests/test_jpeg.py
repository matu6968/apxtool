import json,struct
from unittest.mock import Mock,patch
import pytest
from apxtool.debug import DebugClient,Frame,HEADER,Operation
from apxtool.jpeg import JPEGClient
from apxtool.errors import ApxError,TransferError

def client(payload=b'',status=0):
 def exchange(data):
  _,_,op,space,width,seq,command,length,_=HEADER.unpack_from(data)
  assert op==Operation.JPEG and space==0 and width==1 and length==len(data)-24
  return Frame(op,space,width,seq,command,len(payload),status,payload).encode()
 return JPEGClient(DebugClient(Mock(exchange=Mock(side_effect=exchange))))
def test_frames_limits_and_errors():
 c=client();c.preview('0:/日本語.jpg');data=c.debug.transport.exchange.call_args.args[0]
 assert HEADER.unpack_from(data)[6]==1 and data[24:]==('0:/日本語.jpg\0').encode()
 c.preview('0:/large.jpg',allow_slow=True);data=c.debug.transport.exchange.call_args.args[0]
 assert HEADER.unpack_from(data)[6]==4 and data[24:]==struct.pack('<I',1)+b'0:/large.jpg\0'
 with pytest.raises(ValueError):c.preview('x',allow_slow=1)
 with pytest.raises(ApxError,match='--allow-slow'):client(status=0x207).preview('x')
 c.encode_sample('0:/new.jpg',2048,1536);data=c.debug.transport.exchange.call_args.args[0]
 assert HEADER.unpack_from(data)[6]==2 and data[24:36]==struct.pack('<III',2048,1536,8)
 c.cancel();assert HEADER.unpack_from(c.debug.transport.exchange.call_args.args[0])[6:8]==(3,0)
 for dims in [(0,176),(221,176),(220,175),(2050,1536),(2048,1538)]:
  with pytest.raises(ValueError):client().encode_sample('x',*dims)
 with pytest.raises(ValueError):client().preview('x\0y')
 with pytest.raises(ValueError):client().encode_sample('x',220,176,selector=16)
 with pytest.raises(TransferError):client(b'x').cancel()
 with pytest.raises(ApxError,match='UNSUPPORTED'):client(status=0x202).preview('x')
 with pytest.raises(ApxError,match='storage'):client(status=0x108).encode_sample('x',220,176)
def test_job_wait_state_and_timeout():
 for job in (0,6):
  with pytest.raises(ApxError):client(json.dumps({'job':job,'job_error':0x205}).encode()).wait()
 c=client(json.dumps({'job':5,'bytes':123}).encode());assert c.wait()['bytes']==123
 with pytest.raises(ValueError):c.wait(0)

def test_wait_yields_more_cpu_to_software_jobs():
 c=JPEGClient(Mock())
 c.info=Mock(side_effect=[{'job':1,'software':0},{'job':1,'software':1},{'job':5,'bytes':42}])
 with patch('apxtool.jpeg.time.sleep') as sleep:
  assert c.wait()['bytes']==42
 assert [call.args[0] for call in sleep.call_args_list]==[.02,.25]
