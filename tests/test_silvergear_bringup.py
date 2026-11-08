"""Actual power/DDR C payload behind modeled BootROM transfers."""
import ctypes
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
import struct
import subprocess

import pytest

from apxtool import silvergear, silvergear_bringup as bringup
from apxtool.errors import ApxError
from apxtool.protocol import RAM_TRANSFER_FUNCTION
from apxtool.transport import SCSITransfer

SDK=Path(__file__).resolve().parents[2]/'appotech-sdk'
BUILD=0x12345678


class Loader:
    def __init__(self,lib):
        self.lib=lib;self.sram=ctypes.create_string_buffer(0x1c00)
        self.sram[:]=bytes((i*13+7)&255 for i in range(len(self.sram)))
        self.original=bytes(self.sram);self.callbacks=[]
        lib.bringup_model_reset()
    def execute(self,cdb,*,data_out=None,data_in_length=0):
        if cdb[0]==0x12:
            return SCSITransfer(bytes(8)+b'Buildwin'+b'Video050Loader  '+b'1.00')
        assert cdb[0]==0xcb
        function,address,callback=struct.unpack_from('<3I',cdb,1)
        assert function==RAM_TRANSFER_FUNCTION
        size=len(data_out) if data_out is not None else data_in_length
        if address==0x100000 or 0x100000<address<0x110000:
            assert callback==0xffffffff and data_out is None and address+size<=0x110000
            return SCSITransfer(bytes(size))
        if address>=0x02000000:
            assert data_out is None and callback==0xffffffff and address+size<=0x02400000
            return SCSITransfer(ctypes.string_at(self.lib.bringup_model_memory()+address-0x02000000,size))
        assert bringup.ENTRY<=address<=address+size<=bringup.END
        offset=address-bringup.ENTRY
        if data_out is not None:self.sram[offset:offset+size]=data_out
        if callback!=0xffffffff:
            assert callback==bringup.ENTRY and address==bringup.CONTROL
            self.callbacks.append(struct.unpack_from('<I',self.sram,offset+8)[0])
            self.lib.rescue(ctypes.addressof(self.sram)+offset)
        return SCSITransfer(bytes(self.sram)[offset:offset+size] if data_in_length else b'')


@pytest.fixture
def setup(tmp_path,monkeypatch):
    library=tmp_path/'bringup.so'
    subprocess.run(['cc','-std=c11','-O1','-fPIC','-shared','-Wall','-Wextra','-Werror',
                    '-fsanitize=undefined','-fno-sanitize-recover=undefined',
                    '-DBRINGUP_LIBRARY',f'-DBRINGUP_BUILD_ID={BUILD}u',
                    str(SDK/'tests/silvergear_bringup_model.c'),'-o',str(library)],check=True)
    lib=ctypes.CDLL(str(library));lib.rescue.argtypes=[ctypes.c_void_p]
    lib.bringup_model_memory.restype=ctypes.c_void_p
    loader=Loader(lib)
    code=b'\0\0\0\x15'*20
    manifest=dict(board='silvergear-3222882',abi=1,entry=bringup.ENTRY,control=bringup.CONTROL,
                  reservation_end=bringup.END,request_bytes=208,build_id=BUILD,bytes=len(code),
                  sha256=hashlib.sha256(code).hexdigest())
    image=tmp_path/'bringup.bin';image.write_bytes(code)
    image.with_suffix('.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(bringup,'ROM_SHA256',hashlib.sha256(bytes(0x10000)).hexdigest())
    return loader,image,tmp_path


def test_power_masks_full_ddr_patterns_usb_readback_and_sram_restore(setup):
    loader,image,_=setup
    with bringup.PowerSession(loader,image) as session:
        before=session.last['pm']
        held=session.call(1)['pm']
        assert held[0x40]==before[0x40]|4 and held[0x5a]==before[0x5a]|8
        rails=session.call(2)['pm']
        assert loader.lib.bringup_model_last44()==before[0x44] and rails[0x44]==before[0x44]
        assert rails[0x40]==before[0x40]|0x14
        with pytest.raises(ApxError,match='command 6 failed'):session.call(6)
        loader.lib.bringup_model_ready()
        result=bringup.memory_test(session)
        assert result['zeroed_and_verified'] and len(result['passes'])==4
        assert result['host_sha256']==result['expected_sha256']
    assert bytes(loader.sram)==loader.original
    assert set(loader.callbacks)=={0,1,2,3,4,6}


def test_aliases_between_halves_fail_and_cleanup_still_restores_sram(setup):
    loader,image,_=setup;loader.lib.bringup_model_ready();loader.lib.bringup_model_alias(1)
    with pytest.raises(ApxError,match='command 4 failed'):
        with bringup.PowerSession(loader,image) as session:
            bringup.memory_test(session)
    assert bytes(loader.sram)==loader.original


def test_workflow_waits_for_release_then_recovers_and_tests_ddr(setup,monkeypatch):
    loader,image,tmp=setup
    release=tmp/'released';release.write_text('power button released\n')
    def prepare(path,stock,*,power_configured):
        assert power_configured and loader.lib.bringup_model_pm(0)&0x14==0x14
        assert bytes(loader.sram)==loader.original
        loader.lib.bringup_model_ready()
        return dict(device=path,ddr_initialized=True,adc_calibration=800)
    monkeypatch.setattr(silvergear,'prepare',prepare)
    monkeypatch.setattr(bringup,'open_loader',lambda path:nullcontext(loader))
    result={}
    bringup.run('/dev/model','stock',image,release,tmp/'result.json',result,stable_seconds=0)
    assert result['phase'].startswith('COMPLETE')
    assert result['ddr_probe_sram_restored'] and result['memory_test']['zeroed_and_verified']


def test_wrong_build_and_rom_refuse_before_pm_changes(setup,monkeypatch):
    loader,image,_=setup
    monkeypatch.setattr(bringup,'ROM_SHA256','0'*64)
    with pytest.raises(ValueError,match='BootROM'):bringup.PowerSession(loader,image)
    assert not loader.callbacks and bytes(loader.sram)==loader.original
    monkeypatch.setattr(bringup,'ROM_SHA256',hashlib.sha256(bytes(0x10000)).hexdigest())
    manifest=json.loads(image.with_suffix('.json').read_text());manifest['build_id']^=1
    image.with_suffix('.json').write_text(json.dumps(manifest))
    with pytest.raises(ApxError,match='handshake'):bringup.PowerSession(loader,image)
    assert loader.callbacks==[0] and bytes(loader.sram)==loader.original
