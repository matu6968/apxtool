import struct
from unittest.mock import Mock
import pytest
from apxtool.uart import UARTClient
from apxtool.debug import DebugClient,Frame,HEADER,Operation

def client():
 def exchange(data):
  _,_,op,space,width,seq,cmd,length,_=HEADER.unpack_from(data);assert op==Operation.UART and length==len(data)-24
  reply=b'{"tx_queued":0,"timeouts":0}' if cmd==0 else b'UART' if cmd==3 else b''
  return Frame(op,space,width,seq,cmd,len(reply),0,reply).encode()
 return UARTClient(DebugClient(Mock(exchange=Mock(side_effect=exchange))))
def test_uart_protocol():
 c=client();c.configure();assert c.debug.transport.exchange.call_args.args[0][24:]==struct.pack('<IIi',115200,6,-1)
 c.configure(9600,1,1);c.console(True);c.console(False);c.write(bytes(range(256))*3);assert c.read(4)==b'UART';c.stop()
 calls=c.debug.transport.exchange.call_args_list
 assert [len(x.args[0])-24 for x in calls if HEADER.unpack_from(x.args[0])[6]==2]==[256,256,256]
def test_uart_validation():
 c=client()
 for args in [(0,6,None),(115200,0,None),(115200,32,None),(115200,6,32),(True,6,None)]:
  with pytest.raises(ValueError):c.configure(*args)
 with pytest.raises(ValueError):c.read(4097)
 with pytest.raises(ValueError):c.write(b'x',0)
