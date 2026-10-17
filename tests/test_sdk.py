import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from types import SimpleNamespace
from unittest.mock import Mock
from apxtool.sdk import load_manifest,launch,RESERVATIONS,DISPLAY_RESERVATIONS,ALLOWED_RESERVATIONS
from apxtool.errors import ApxError,UnsafeOperation

@pytest.fixture
def manifest(tmp_path):
    data=b'\x15\0\0\0'*4
    (tmp_path/'code.bin').write_bytes(data)
    document={'schema_version':1,'protocol':1,'vid':0x1908,'pid':0x3319,
              'build_id':'test','clock_policy':'inherit-loader-96','reservations':RESERVATIONS,
              'entry':0x02180000,'segments':[{'file':'code.bin','address':0x02180000,
              'bytes':16,'memory_bytes':32,'flags':5,'sha256':hashlib.sha256(data).hexdigest()}]}
    path=tmp_path/'test.manifest.json';path.write_text(json.dumps(document))
    return path,document

def test_manifest_rejects_hash_range_overlap_and_entry(manifest):
    path,original=manifest
    assert load_manifest(path)[1]==[(0x02180000,(path.parent/'code.bin').read_bytes())]
    for edit in (lambda d:d['segments'][0].update(sha256='0'*64),
                 lambda d:d['segments'][0].update(address=0x02000000),
                 lambda d:d['segments'].append(dict(d['segments'][0])),
                 lambda d:d.update(entry=0x02180001),
                 lambda d:d['segments'][0].update(flags=4),
                 lambda d:d['segments'][0].update(file='../code.bin'),
                 lambda d:d['segments'][0].update(memory_bytes=-1)):
        document=json.loads(json.dumps(original));edit(document);path.write_text(json.dumps(document))
        with pytest.raises(ValueError):load_manifest(path)

def test_sdk_clock_policy_selects_only_fixed_profiles(manifest):
    path,document=manifest
    for policy in ('inherit-loader-96','sdk-pll1-144'):
        document['clock_policy']=policy;path.write_text(json.dumps(document))
        assert load_manifest(path)[0]['clock_policy']==policy
    document['clock_policy']='arbitrary-pll';path.write_text(json.dumps(document))
    with patch('apxtool.sdk.enter_loader_with_device') as enter:
        with pytest.raises(ValueError,match='clock policy'):launch('/dev/sg0',path)
        enter.assert_not_called()

class FakeDevice:
    def __init__(self):self.closed=False
    def close(self):self.closed=True
    def execute(self,*args,**kwargs):raise ApxError('old USB interface disconnected')

def test_takeover_never_cleans_active_sdk_after_handshake_failure(manifest):
    path,document=manifest;device=FakeDevice();reads=iter([bytes(0x20000),bytes(0x1000),b'\x15\0\0\0'*4,bytes(0x2000)])
    with patch('apxtool.sdk.scsi_port',return_value='7-1'),patch('apxtool.sdk.enter_loader_with_device',return_value=('/dev/sg0',device,None)),\
         patch('apxtool.sdk.read_ram',side_effect=lambda *a:next(reads)),patch('apxtool.sdk.write_ram_experimental') as write,\
         patch('apxtool.sdk.wait_for_sdk',side_effect=ApxError('no handshake')):
        with pytest.raises(ApxError,match='no handshake'):launch('/dev/sg0',path)
        assert write.call_count==1
        assert device.closed

@pytest.mark.parametrize('clock',[
    None,
    {'requested_mhz':144,'pll1_control':0x1623,'pll1_coefficient':0x0b71b000,'error':0},
    {'requested_mhz':144,'pll1_control':0x1123,'pll1_coefficient':0x112a8800,'error':2},
])
def test_clock_handshake_mismatch_never_restores_active_sdk_ram(manifest,clock):
    path,document=manifest;document['clock_policy']='sdk-pll1-144';path.write_text(json.dumps(document))
    device=FakeDevice();transport=Mock();info={'cpu_hz_estimate':144000000,'clock':clock}
    reads=iter([bytes(0x20000),bytes(0x1000),b'\x15\0\0\0'*4,bytes(0x2000)])
    with patch('apxtool.sdk.scsi_port',return_value='5-1'),patch('apxtool.sdk.enter_loader_with_device',return_value=('/dev/sg0',device,None)),\
         patch('apxtool.sdk.read_ram',side_effect=lambda *a:next(reads)),patch('apxtool.sdk.write_ram_experimental') as write,\
         patch('apxtool.sdk.wait_for_sdk',return_value=(transport,info)):
        with pytest.raises(ApxError,match='clock profile'):launch('/dev/sg0',path)
        assert write.call_count==1 and device.closed
        transport.close.assert_called_once()

def test_occupied_ram_prevents_upload_and_callback(manifest):
    path,_=manifest;device=FakeDevice()
    with patch('apxtool.sdk.scsi_port',return_value='7-1'),patch('apxtool.sdk.enter_loader_with_device',return_value=('/dev/sg0',device,None)),\
         patch('apxtool.sdk.read_ram',return_value=b'occupied'),patch('apxtool.sdk.write_ram_experimental') as write,\
         patch.object(device,'execute') as execute:
        with pytest.raises(UnsafeOperation):launch('/dev/sg0',path)
        write.assert_not_called();execute.assert_not_called();assert device.closed

def test_bootrom_release_requires_expected_ownership():
    from apxtool.debug_cli import wait_bootrom
    with patch('apxtool.debug_cli.iter_sg_paths') as paths:
        with pytest.raises(ValueError,match='ownership'):wait_bootrom('7-1',owned_ram=[[0x02000000,0x02400000]])
        paths.assert_not_called()

@pytest.mark.parametrize('reservations',ALLOWED_RESERVATIONS)
def test_sdk_ram_release_follows_loader_inquiry_and_ram_read(reservations):
    from apxtool.debug_cli import wait_bootrom
    events=[]
    class Loader:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    def read(device,address,length):
        events.append(('read',address,length));return bytes(length)
    def write(device,address,data):events.append(('write',address,len(data)))
    with patch('apxtool.debug_cli.iter_sg_paths',return_value=['/dev/sg0']),\
         patch('apxtool.debug_cli.scsi_port',return_value='7-1'),\
         patch('apxtool.debug_cli.LinuxSCSITransport',return_value=Loader()),\
         patch('apxtool.debug_cli.inquiry',return_value=SimpleNamespace(is_loader=True)),\
         patch('apxtool.debug_cli.read_ram',side_effect=read),\
         patch('apxtool.debug_cli.write_ram_experimental',side_effect=write):
        assert wait_bootrom('7-1',owned_ram=[[b,b+n] for b,n in reservations])=='/dev/sg0'
    assert events[0]==('read',0x02000000,16)
    assert [e for e in events if e[0]=='write']==[('write',b,n) for b,n in reservations]

def test_optional_display_reservation_is_bounded(manifest):
    path,document=manifest
    document['reservations']=DISPLAY_RESERVATIONS
    path.write_text(json.dumps(document));assert load_manifest(path)[0]['reservations']==DISPLAY_RESERVATIONS
    for address,length in ((0x02210000,0x20000),(0x02220000,0x10000),(0x02000000,0x10000)):
        document['reservations']=RESERVATIONS+[[address,length]];path.write_text(json.dumps(document))
        with pytest.raises(ValueError,match='reservations'):load_manifest(path)

@pytest.mark.parametrize('reservations',ALLOWED_RESERVATIONS[2:])
def test_storage_reservations_are_explicit_and_tables_protected_by_manifest(manifest,reservations):
    path,document=manifest;document['reservations']=reservations
    path.write_text(json.dumps(document));assert load_manifest(path)[0]['reservations']==reservations
    document['reservations']=reservations[:-1]+[[0x02220000,0xd0000]]
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError,match='reservations'):load_manifest(path)

def test_occupied_display_workspace_prevents_takeover(manifest):
    path,document=manifest;document['reservations']=DISPLAY_RESERVATIONS;path.write_text(json.dumps(document))
    device=FakeDevice();reads=iter([bytes(0x20000),bytes(0x1000),b'occupied',bytes(0x20000),bytes(0x1000)])
    with patch('apxtool.sdk.scsi_port',return_value='7-1'),patch('apxtool.sdk.enter_loader_with_device',return_value=('/dev/sg0',device,None)),\
         patch('apxtool.sdk.read_ram',side_effect=lambda *a:next(reads)),patch('apxtool.sdk.write_ram_experimental') as write,\
         patch.object(device,'execute') as execute:
        with pytest.raises(UnsafeOperation,match='0x2210000'):launch('/dev/sg0',path)
        assert write.call_count==2 # restore only previously preflighted zero regions
        execute.assert_not_called();assert device.closed

def test_lost_bootrom_ack_never_attempts_ram_release():
    from apxtool.debug_cli import run
    transport=SimpleNamespace(port='7-1',close=Mock())
    client=Mock();client.info.return_value={'owned_ram':[[b,b+n] for b,n in RESERVATIONS]}
    client.bootrom.side_effect=ApxError('lost ACK')
    args=SimpleNamespace(command='debug',port='7-1',vid=0x1908,pid=0x3319,debug_command='bootrom')
    with patch('apxtool.debug_cli.USBTransport',return_value=transport),\
         patch('apxtool.debug_cli.DebugClient',return_value=client),\
         patch('apxtool.debug_cli.wait_bootrom') as recover:
        with pytest.raises(ApxError,match='lost ACK'):run(args)
        recover.assert_not_called();transport.close.assert_called_once()

def test_jpeg_reclaim_backs_up_and_restores_before_takeover(manifest):
 from apxtool.sdk import JPEG_RESERVATION
 path,document=manifest;document['reservations']=RESERVATIONS+[JPEG_RESERVATION];path.write_text(json.dumps(document))
 device=FakeDevice();original=bytes([17])*JPEG_RESERVATION[1]
 reads=iter([bytes(0x20000),bytes(0x1000),original,bytes(JPEG_RESERVATION[1]),b'wrong',bytes(0x20000),bytes(0x1000),original])
 with patch('apxtool.sdk.scsi_port',return_value='7-1'),patch('apxtool.sdk.enter_loader_with_device',return_value=('/dev/sg0',device,None)),\
      patch('apxtool.sdk.read_ram',side_effect=lambda *a:next(reads)),patch('apxtool.sdk.write_ram_experimental') as write,\
      patch.object(device,'execute') as execute:
  with pytest.raises(ApxError,match='upload readback'):launch('/dev/sg0',path,reclaim_jpeg_ram=True)
  execute.assert_not_called();assert device.closed
  assert write.call_args_list[-1].args[1:]==(JPEG_RESERVATION[0],original)
 backup=list(path.parent.glob('jpeg-ram-before-*.bin'));assert len(backup)==1 and backup[0].read_bytes()==original
 metadata=json.loads(backup[0].with_suffix('.json').read_text());assert metadata['sha256']==hashlib.sha256(original).hexdigest()
