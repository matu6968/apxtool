import json
import struct
import unittest
from unittest.mock import Mock,patch
from apxtool.debug import DebugClient,Frame,Operation,Space,MAX_PAYLOAD,HEADER
from apxtool.errors import ApxError,TransferError

def test_sdk_handshake_restores_request_deadline():
    from apxtool.debug import wait_for_sdk
    failed=Mock(timeout_ms=1000,exchange=Mock(side_effect=TransferError('not ready')))
    ready=Mock(timeout_ms=1000)
    def exchange(data):
        assert ready.timeout_ms==1000
        request=Frame.decode(data)
        payload=json.dumps({'protocol':1,'max_payload':4096,'build_id':'audio-test'}).encode()
        return Frame(request.operation,request.space,request.width,request.sequence,
                     request.address,len(payload),0,payload).encode()
    ready.exchange.side_effect=exchange
    with patch('apxtool.debug.USBTransport',side_effect=[failed,ready]) as create,\
         patch('apxtool.debug.time.sleep'):
        transport,info=wait_for_sdk('5-1',build_id='audio-test')
    assert transport is ready and info['build_id']=='audio-test'
    assert ready.timeout_ms==5000
    failed.close.assert_called_once();ready.close.assert_not_called()
    assert all(call.kwargs['timeout_ms']==1000 for call in create.call_args_list)

class FakeBulk:
    def __init__(self):self.requests=[];self.corrupt=False;self.status=0
    def exchange(self,data):
        magic,version,op,space,width,seq,addr,length,status=HEADER.unpack_from(data)
        self.requests.append((op,addr,length,data[24:]))
        payload=bytes((addr+i)&255 for i in range(length)) if op==Operation.READ else b''
        if op==Operation.INFO:payload=json.dumps({'protocol':1,'max_payload':MAX_PAYLOAD}).encode()
        return Frame(op,space,width,seq+(1 if self.corrupt else 0),addr,len(payload),self.status,payload).encode()

class DebugTests(unittest.TestCase):
    def test_header_layout(self):
        data=Frame(Operation.WRITE,Space.MEMORY,1,7,0x2200000,3,0,b'abc').encode()
        self.assertEqual(data[:4],b'APXD');self.assertEqual(len(data),27)
        self.assertEqual(struct.unpack_from('<I',data,12)[0],0x2200000)
    def test_chunked_memory_read_write(self):
        transport=FakeBulk();client=DebugClient(transport)
        self.assertEqual(len(client.read(0x2210000,4097)),4097)
        self.assertEqual([r[2] for r in transport.requests],[4096,1])
        transport.requests=[];client.write(0x2210000,b'x'*4097)
        self.assertEqual([r[2] for r in transport.requests],[4096,1])
    def test_stale_or_short_responses_fail(self):
        transport=FakeBulk();transport.corrupt=True
        with self.assertRaises(TransferError):DebugClient(transport).info()
        for data in (b'',b'APXD',Frame(Operation.READ,length=3,payload=b'ab').encode()):
            with self.assertRaises(TransferError):Frame.decode(data)
    def test_failed_writes_are_not_retried(self):
        transport=FakeBulk();transport.status=4
        with self.assertRaises(ApxError):DebugClient(transport).write(0x2180080,b'x')
        self.assertEqual(len(transport.requests),1)
    def test_spr_and_alignment_checks(self):
        client=DebugClient(FakeBulk())
        for address,length,width in ((0x9804,8,4),(0x10000,4,4),(0x9804,4,1)):
            with self.assertRaises(ValueError):client.read(address,length,space=Space.SPR,width=width)
        with self.assertRaises(ValueError):client.read(0xffffffff,2)
        with self.assertRaises(ValueError):client.read(0x2210001,4,width=4)
        self.assertEqual(len(client.read(0x11,4,space=Space.SPR,width=4)),4)
