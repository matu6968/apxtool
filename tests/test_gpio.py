import json,struct
from unittest.mock import Mock
import pytest
from apxtool.debug import Operation,DebugClient,Frame,HEADER
from apxtool.gpio import GPIOClient,button_names
from apxtool.errors import TransferError,DebugCommandError
def client(payload=b'',status=0):
    def exchange(data):
        _,_,op,space,width,seq,command,length,_=HEADER.unpack_from(data)
        assert op==Operation.GPIO and length==len(data)-24
        return Frame(op,space,width,seq,command,len(payload),status,payload).encode()
    return GPIOClient(DebugClient(Mock(exchange=Mock(side_effect=exchange))))
def test_gpio_led_info_events():
    c=client(json.dumps({'state':4,'adc':484}).encode());assert c.info()['adc']==484
    c=client();c.led(True);assert c.debug.transport.exchange.call_args.args[0][24:]==struct.pack('<I',1)
    c=client(struct.pack('<IIII',0xffffffff,36,36,0));assert c.events()==[{'ticks':0xffffffff,'state':36,'pressed':36,'released':0}]
    assert button_names(36)==['UP','POWER']
def test_gpio_rejects_invalid_responses_and_state():
    with pytest.raises(ValueError):client().led(2)
    with pytest.raises(TransferError):client(b'x').led(1)
    with pytest.raises(TransferError):client(b'x').events()
    with pytest.raises(TransferError):client(bytes(272)).events()
    with pytest.raises(DebugCommandError):client(status=3).info()
