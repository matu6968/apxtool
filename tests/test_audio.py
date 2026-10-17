import json,struct
from unittest.mock import Mock
import pytest
from apxtool.debug import DebugClient,Frame,HEADER,Operation
from apxtool.audio import AudioClient
from apxtool.errors import ApxError,TransferError
def client(payload=b'',status=0):
 def exchange(data):
  _,_,op,space,width,seq,command,length,_=HEADER.unpack_from(data)
  assert op==Operation.AUDIO and space==0 and width==1 and length==len(data)-24
  return Frame(op,space,width,seq,command,len(payload),status,payload).encode()
 return AudioClient(DebugClient(Mock(exchange=Mock(side_effect=exchange))))
def test_audio_frames():
 c=client();c.tone(660,500,16000);assert c.debug.transport.exchange.call_args.args[0][24:]==struct.pack('<III',660,500,16000)
 c.volume(60);c.mute();c.stop();c.wav('0:/日本語.wav')
 assert c.debug.transport.exchange.call_args.args[0][24:]==('0:/日本語.wav\0').encode()
 data=bytes(8186);c.debug.transport.exchange.reset_mock();c.pcm(data)
 packets=[x.args[0] for x in c.debug.transport.exchange.call_args_list]
 assert [HEADER.unpack_from(x)[6] for x in packets]==[6,6,6,7]
 assert [struct.unpack_from('<I',x,24)[0] for x in packets[:-1]]==[0,4092,8184]
 assert b''.join(x[28:] for x in packets[:-1])==data
def test_audio_errors_and_wait():
 for f in [lambda c:c.volume(101),lambda c:c.volume(True),lambda c:c.mute(1),lambda c:c.pcm(b'x'),lambda c:c.pcm(bytes(65506)),lambda c:c.pcm(b'xx',44100),lambda c:c.tone(1),lambda c:c.tone(440,4000,32000)]:
  with pytest.raises(ValueError):f(client())
 with pytest.raises(ApxError,match='BUSY'):client(status=0x303).tone()
 with pytest.raises(ApxError,match='storage'):client(status=0x104).wav('x')
 with pytest.raises(TransferError):client(b'x').stop()
 for state in (0,1,5):
  with pytest.raises(ApxError):client(json.dumps({'state':state,'error':4}).encode()).wait()
 assert client(b'{"state":4,"ticks":10}').wait()['ticks']==10
