import json,struct
from unittest.mock import Mock
import pytest
from apxtool.debug import DebugClient,Frame,HEADER,Operation
from apxtool.sdk_flash import FlashClient,CAPACITY
from apxtool.battery import BatteryClient
from apxtool.errors import TransferError

def debug(short=False):
 def exchange(data):
  _,_,op,space,width,seq,cmd,length,_=HEADER.unpack_from(data)
  assert space==0 and width==1 and length==len(data)-24
  if op==Operation.SPI:
   if not cmd:reply=b'{"jedec":6176789}'
   else:
    address,n=struct.unpack_from('<II',data,24)
    reply=bytes((address+i)&255 for i in range(n-int(short)))
  else:
   assert op==Operation.BATTERY and not cmd and not length
   reply=b'{"valid":true,"millivolts":4034,"level":4}'
  return Frame(op,space,width,seq,cmd,len(reply),0,reply).encode()
 return DebugClient(Mock(exchange=Mock(side_effect=exchange)))

def test_sdk_flash_chunks_and_battery(tmp_path):
 d=debug();f=FlashClient(d);assert f.info()['jedec']==0x5e4015
 assert f.read(17,8193)==bytes((17+i)&255 for i in range(8193))
 assert [struct.unpack_from('<II',p.args[0],24) for p in d.transport.exchange.call_args_list[1:]]==[(17,4096),(4113,4096),(8209,1)]
 p=tmp_path/'spi.bin';f.download(p,0,33);assert p.stat().st_size==33
 count=d.transport.exchange.call_count
 with pytest.raises(FileExistsError):f.download(p)
 assert d.transport.exchange.call_count==count
 assert BatteryClient(d).info()['millivolts']==4034

def test_sdk_flash_rejects_ranges_and_short_response():
 f=FlashClient(debug())
 for address,n in [(-1,1),(0,-1),(CAPACITY,1),(2**32,0),(False,1),(0,True)]:
  with pytest.raises(ValueError):f.read(address,n)
 assert f.read(CAPACITY,0)==b''
 with pytest.raises(TransferError):FlashClient(debug(True)).read(0,33)
