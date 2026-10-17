import io,json,struct,wave
from unittest.mock import Mock
import pytest
from apxtool.debug import DebugClient,Frame,HEADER,Operation
from apxtool.microphone import MicrophoneClient
from apxtool.errors import ApxError,TransferError
def client(status=0,payload=None):
 def exchange(data):
  _,_,op,space,width,seq,command,length,_=HEADER.unpack_from(data)
  assert op==Operation.MICROPHONE and space==0 and width==1 and length==len(data)-24
  reply=payload
  if reply is None:
   if command==0:reply=b'{"state":3,"guard_ok":true,"bytes":8224,"rate":16000}'
   elif command==3:reply=bytes(struct.unpack_from('<I',data,28)[0])
   else:reply=b''
  return Frame(op,space,width,seq,command,len(reply),status,reply).encode()
 return MicrophoneClient(DebugClient(Mock(exchange=Mock(side_effect=exchange))))
def test_capture_frames_and_chunked_WAV():
 c=client();c.start(65536,32000)
 assert c.debug.transport.exchange.call_args.args[0][24:]==struct.pack('<II',65536,32000)
 c.save('0:/声.wav');assert c.debug.transport.exchange.call_args.args[0][24:]==('0:/声.wav\0').encode()
 out=io.BytesIO();c.download(out);out.seek(0)
 with wave.open(out,'rb') as w:assert w.getparams()[:4]==(1,2,16000,4112) and w.readframes(4112)==bytes(8224)
 requests=[p.args[0] for p in c.debug.transport.exchange.call_args_list if HEADER.unpack_from(p.args[0])[6]==3]
 assert [struct.unpack_from('<II',p,24) for p in requests]==[(0,4096),(4096,4096),(8192,32)]
 c.play();c.stop()
def test_capture_validation_and_errors():
 for fn in [lambda c:c.start(33),lambda c:c.start(65568),lambda c:c.start(32,44100),lambda c:c.start(True),lambda c:c.read(1,2),lambda c:c.read(0,65538)]:
  with pytest.raises(ValueError):fn(client())
 with pytest.raises(ApxError,match='BUSY'):client(status=0x403).start()
 with pytest.raises(ApxError,match='storage'):client(status=0x108).save('exists.wav')
 with pytest.raises(TransferError):client(payload=b'x').stop()
 with pytest.raises(TransferError):client(payload=b'x').read(0,2)
 for data in [{'state':3,'guard_ok':False},{'state':4,'error':4},{'state':0},{'state':1}]:
  with pytest.raises(ApxError):client(payload=json.dumps(data).encode()).wait()
