import ctypes
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apxtool import silvergear_recovery as recovery
from apxtool.errors import ApxError, UnsafeOperation
from apxtool.flasher import PayloadFlash, SRAM
from test_flasher import LoaderModel, CODE, SDK


@pytest.fixture
def setup(tmp_path, monkeypatch):
    library=tmp_path/'restore.so'
    subprocess.run(['cc','-std=c11','-O1','-fPIC','-shared','-Wall','-Wextra','-Werror',
                    '-fsanitize=undefined','-fno-sanitize-recover=undefined',
                    '-DFLASHER_HOST','-DSILVERGEAR_BOOT_RESTORE',str(SDK/'boot/flasher.c'),
                    str(SDK/'tests/flasher_model.c'),'-o',str(library)],check=True)
    lib=ctypes.CDLL(str(library));lib.model_nor.restype=ctypes.POINTER(ctypes.c_uint8*0x400000)
    lib.model_set_sram_window.argtypes=[ctypes.c_void_p,ctypes.c_uint64];lib.model_call.argtypes=[ctypes.c_void_p]
    loader=LoaderModel(lib,ddr_alive=False,jedec=bytes.fromhex('944016'))
    stock=bytes([0xa5])*0x400000;current=bytearray(stock)
    for address in recovery.WRITE_ORDER:current[address:address+4096]=bytes(4096)
    ctypes.memmove(ctypes.addressof(loader.nor),bytes(current),len(current))
    monkeypatch.setattr(recovery,'STOCK_SHA256',hashlib.sha256(stock).hexdigest())
    monkeypatch.setattr(recovery,'CURRENT_SHA256',hashlib.sha256(current).hexdigest())
    flash=PayloadFlash(loader,CODE,SRAM,spi_init='sdk')
    return flash,loader,stock,tmp_path


def test_restores_only_reviewed_sectors_with_header_last_and_full_verify(setup):
    flash,loader,stock,tmp=setup
    result=recovery.restore(flash,stock,tmp/'before.bin',tmp/'journal.json')
    assert result['status']=='verified' and result['verified_sectors']==list(recovery.WRITE_ORDER)
    assert result['boot_header_committed'] and bytes(loader.nor)==stock
    assert hashlib.sha256((tmp/'before.bin').read_bytes()).hexdigest()==recovery.CURRENT_SHA256
    assert json.loads((tmp/'journal.json').read_text())['readback_sha256']==recovery.STOCK_SHA256
    assert loader.lib.model_command_count(0x20)==3
    # No PM configuration or status-register write opcode is permitted/needed.
    assert loader.lib.model_command_count(1)==0


def test_changed_flash_and_protection_refuse_before_erase(setup):
    flash,loader,stock,tmp=setup;loader.nor[0x2000]^=1
    with pytest.raises(UnsafeOperation,match='reviewed readback'):
        recovery.restore(flash,stock,tmp/'before.bin',tmp/'journal.json')
    assert loader.lib.model_command_count(6)==0
    loader.nor[0x2000]^=1;loader.lib.model_set_flash_status(0x0c)
    with pytest.raises(UnsafeOperation,match='protection'):
        recovery.restore(flash,stock,tmp/'before.bin',tmp/'journal.json')
    assert loader.lib.model_command_count(6)==0 and not (tmp/'before.bin').exists()


def test_lost_erase_ack_stops_without_committing_header(setup):
    flash,loader,stock,tmp=setup;execute=loader.execute
    def fail(cdb,**kwargs):
        command=int.from_bytes(bytes(loader.sram)[0x1908:0x190c],'little')
        result=execute(cdb,**kwargs)
        if command==3 and int.from_bytes(cdb[9:13],'little')!=0xffffffff:
            raise ApxError('lost erase acknowledgement')
        return result
    with patch.object(loader,'execute',side_effect=fail):
        with pytest.raises(ApxError,match='lost erase'):
            recovery.restore(flash,stock,tmp/'before.bin',tmp/'journal.json')
    journal=json.loads((tmp/'journal.json').read_text())
    assert journal['status']=='interrupted' and not journal['boot_header_committed']
    assert loader.lib.model_command_count(0x20)==1
    assert bytes(loader.nor)[:4096]==bytes(4096)


def test_rejects_unreviewed_program_bytes_and_addresses_before_transport(setup):
    flash,loader,stock,tmp=setup
    with patch.object(loader,'execute') as execute:
        for command,address,data in [(3,0x2000,b''),(2,0,b'bad'),(2,0,bytes(256)),(7,0,b'')]:
            with pytest.raises(ValueError):recovery.write_request(flash,stock,command,address,data)
        execute.assert_not_called()
