import hashlib
import struct
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest

from apxtool import silvergear as sg
from apxtool.errors import ApxError


@pytest.fixture
def images(tmp_path, monkeypatch):
    stock = bytes(0x400000)
    stage = b'\0\0\0\x15'
    backup = tmp_path / 'stock.bin'; backup.write_bytes(stock)
    binary = tmp_path / 'stage.bin'; binary.write_bytes(stage)
    monkeypatch.setattr(sg, 'STOCK_SHA256', hashlib.sha256(stock).hexdigest())
    monkeypatch.setattr(sg, 'STAGE_SHA256', hashlib.sha256(stage).hexdigest())
    monkeypatch.setattr(sg, 'ROM_SHA256', hashlib.sha256(bytes(0x10000)).hexdigest())
    return backup, binary


def test_bad_stock_and_stage_never_open_device(images):
    backup, binary = images
    with patch('apxtool.rom_rescue.open_loader') as opened:
        backup.write_bytes(b'bad')
        with pytest.raises(ValueError, match='verified'):
            sg.prepare('/dev/sg0', backup, binary)
        opened.assert_not_called()


def test_stage_recovery_checks_port_marker_restoration_and_keepalive(images):
    backup, binary = images
    memory = bytearray(0x110000)
    original = bytes((i * 19) & 255 for i in range(sg.END - sg.LOW))
    memory[sg.LOW:sg.END] = original
    scratch = bytes([0x83]) * 0x2000
    memory[sg.SAVED:0x4000] = scratch
    events = []
    device = Mock()
    def execute(*a, **kw):
        events.append('invoke')
        memory[sg.LOW:sg.END] = original
        struct.pack_into('<I', memory, 0x1e14, 800)
        struct.pack_into('<5I', memory, sg.CONTROL, 0x47535841, 0, 0x1623, 0x0b71b000, 800)
        raise ApxError('expected USB detach')
    device.execute.side_effect = execute
    power = Mock()
    power.last={'pm':{0x40:0x16,0x58:8,0x5a:8},'spr':{0x1800:0,0x2000:0}}
    power.call.side_effect=lambda command:events.append('keepalive' if command==1 else 'rails')
    def write(d, address, data):
        events.append(('write', address)); memory[address:address+len(data)] = data
    with patch('apxtool.rom_rescue.open_loader', side_effect=lambda p: nullcontext(device)), \
         patch('apxtool.rom_rescue.loader_sg_paths', return_value=['/dev/sg9']), \
         patch('apxtool.sdk.scsi_port', return_value='7-1'), \
         patch.object(sg, 'inquiry', return_value=Mock(is_loader=True)), \
         patch('apxtool.silvergear_bringup.PowerSession', side_effect=lambda *a, **kw: nullcontext(power)), \
         patch.object(sg, '_read', side_effect=lambda d, a, n: bytes(memory[a:a+n])), \
         patch.object(sg, '_write', side_effect=write):
        result = sg.prepare('/dev/sg0', backup, binary)
    assert result['device'] == '/dev/sg9' and result['adc_calibration'] == 800
    assert memory[sg.SAVED:0x4000] == scratch
    assert events.index('keepalive') < events.index('invoke')
    assert events[-1] == ('write', sg.SAVED)


def test_stage_hash_pin_matches_built_binary_when_available():
    from pathlib import Path
    binary = Path(__file__).resolve().parents[2] / 'appotech-sdk/build/flasher/silvergear-stage.bin'
    if binary.exists():
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == sg.STAGE_SHA256
