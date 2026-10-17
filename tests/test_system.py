import json,struct
from unittest.mock import Mock
import pytest
from apxtool.debug import Operation,DebugClient,Frame,HEADER
from apxtool.system import SystemClient
from apxtool.errors import TransferError,DebugCommandError
def client(payload=b'',status=0):
    def exchange(data):
        _,_,op,space,width,seq,command,length,_=HEADER.unpack_from(data)
        assert op==Operation.SYSTEM and space==0 and width==1 and length==len(data)-24
        return Frame(op,space,width,seq,command,len(payload),status,payload).encode()
    return SystemClient(DebugClient(Mock(exchange=Mock(side_effect=exchange))))
def test_system_frames_and_watchdog_policy():
    c=client(json.dumps({'watchdog':{'automatic':0,'selector':7}}).encode())
    assert c.info()['watchdog']['selector']==7
    c=client();c.watchdog_start(6,automatic=False)
    assert HEADER.unpack_from(c.debug.transport.exchange.call_args.args[0])[6:8]==(1,8)
    assert c.debug.transport.exchange.call_args.args[0][24:]==struct.pack('<II',6,0)
    for method,command in [('watchdog_feed',2),('watchdog_stop',3),('reboot',4),('power_off',5)]:
        getattr(c,method)();assert HEADER.unpack_from(c.debug.transport.exchange.call_args.args[0])[6:8]==(command,0)
def test_system_invalid_configuration_and_errors():
    for selector,automatic in [(-1,True),(8,True),(1,2),(1,1.0),(1.0,True),(True,True)]:
        with pytest.raises(ValueError):client().watchdog_start(selector,automatic=automatic)
    with pytest.raises(TransferError):client(b'x').power_off()
    with pytest.raises(DebugCommandError):client(status=0x110).reboot()
