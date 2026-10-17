import json,struct
from unittest.mock import Mock
import pytest
from apxtool.debug import DebugClient,Frame,HEADER,Operation
from apxtool.camera import CameraClient
from apxtool.errors import ApxError,TransferError
def client(status=0,payload=None):
 def exchange(data):
  _,_,op,space,width,seq,command,length,_=HEADER.unpack_from(data)
  assert op==Operation.CAMERA and space==0 and width==1 and length==len(data)-24
  reply=payload
  if reply is None:
   if command==0:reply=b'{"job":4,"guard_ok":true,"jpeg_bytes":8225}'
   elif command in (3,7):reply=bytes(struct.unpack_from('<I',data,28)[0])
   else:reply=b''
  return Frame(op,space,width,seq,command,len(reply),status,reply).encode()
 return CameraClient(DebugClient(Mock(exchange=Mock(side_effect=exchange))))
def test_camera_protocol(tmp_path):
 c=client();c.capture(2048,1536,8)
 assert c.debug.transport.exchange.call_args.args[0][24:]==struct.pack('<III',2048,1536,8)
 c.preview(False);c.capture_raw();c.save('0:/照片.jpg')
 assert c.debug.transport.exchange.call_args.args[0][24:]==('0:/照片.jpg\0').encode()
 output=tmp_path/'test.jpg';c.download(output);assert output.read_bytes()==bytes(8225)
 with pytest.raises(FileExistsError):c.download(output)
 requests=[p.args[0] for p in c.debug.transport.exchange.call_args_list if HEADER.unpack_from(p.args[0])[6]==3]
 assert [struct.unpack_from('<II',p,24) for p in requests[:3]]==[(0,4096),(4096,4096),(8192,33)]
def test_camera_validation_errors():
 for f in [lambda c:c.capture(200,176),lambda c:c.capture(2049,1536),lambda c:c.capture(640,481),lambda c:c.capture(selector=16),lambda c:c.preview(1),lambda c:c.read(-1,3),lambda c:c.read(524288,1)]:
  with pytest.raises(ValueError):f(client())
 with pytest.raises(ApxError,match='NACK'):client(status=0x507).capture_raw()
 with pytest.raises(ApxError,match='storage'):client(status=0x108).save('exists.jpg')
 with pytest.raises(TransferError):client(payload=b'x').preview()
 with pytest.raises(TransferError):client(payload=b'x').read(0,2)
 for data in [{'job':4,'guard_ok':False},{'job':5,'error':4,'status':0},{'job':0}]:
  with pytest.raises(ApxError):client(payload=json.dumps(data).encode()).wait()
