import json
import struct
from unittest.mock import Mock, call, patch

import pytest

from apxtool.debug import Operation
from apxtool.errors import ApxError, DebugCommandError, TransferError
from apxtool.gpio import button_names
from apxtool.printer import PrinterClient


def test_raster_upload_chunks_and_completion_sequence():
    debug = Mock()
    status = {'sequence': 17, 'state': 4, 'error': 0}
    debug.request.side_effect = lambda op, address, **kw: json.dumps(status).encode() if address == 0 else b''
    printer = PrinterClient(debug)
    raster = bytes(range(256)) * 192
    printer.raster(raster, 384, 1024)
    calls = debug.request.call_args_list
    assert all(c.args == (Operation.PRINTER,) for c in calls)
    assert calls[0].kwargs['payload'] == struct.pack('<II', 384, 1024)
    chunks = [c.kwargs['payload'] for c in calls if c.kwargs['address'] == 4]
    assert b''.join(c[4:] for c in chunks) == raster
    assert [struct.unpack_from('<I', c)[0] for c in chunks] == list(range(0, len(raster), 4092))
    assert printer.sequence == 18
    # A previous job's DONE must not satisfy the new job's wait.
    updates = iter([status, dict(status, sequence=18, state=3), dict(status, sequence=18)])
    debug.request.side_effect = lambda *a, **kw: json.dumps(next(updates)).encode()
    with patch('apxtool.printer.time.sleep'):
        assert printer.wait()['sequence'] == 18


def test_printer_validation_faults_and_timeout_cancel():
    debug = Mock()
    printer = PrinterClient(debug)
    for data, w, h in [(b'', 1, 1), (b'\0', 385, 1), (b'', 1, 1025), (b'', True, 1)]:
        with pytest.raises(ValueError):
            printer.raster(data, w, h)
    for steps in [0, 4097, True]:
        with pytest.raises(ValueError):
            printer.feed(steps)
    debug.request.assert_not_called()
    debug.request.side_effect = DebugCommandError(0x604)
    with pytest.raises(ApxError, match='TEMPERATURE'):
        printer.info()
    debug.request.side_effect = None
    debug.request.return_value = b'x'
    with pytest.raises(TransferError):
        printer.cancel()
    debug.request.side_effect = lambda *a, address, **kw: b'{"sequence":0,"state":3}' if address == 0 else b''
    with patch('apxtool.printer.time.monotonic', side_effect=[0, 2]):
        with pytest.raises(ApxError, match='cancellation sent'):
            printer.wait(timeout=1)
    assert debug.request.call_args.kwargs['address'] == 1


def test_new_button_bits_preserve_existing_names():
    assert button_names(1 | 32 | 64 | 128 | 256 | 512) == ['SHUTTER', 'POWER', 'MODE', 'PRINT', 'CAMERA', 'EXIT']


def test_camera_select_command_and_cli_handler():
    from apxtool.camera import CameraClient
    from apxtool.debug_cli import run
    from types import SimpleNamespace
    debug = Mock()
    debug.request.return_value = b''
    CameraClient(debug).select(1)
    assert debug.request.call_args.kwargs['address'] == 8
    assert debug.request.call_args.kwargs['payload'] == struct.pack('<I', 1)
    with pytest.raises(ValueError):
        CameraClient(debug).select(True)
    args = SimpleNamespace(command='debug', debug_command='camera', camera_command='select',
                           camera='front', port='7-1', vid=0x1908, pid=0x3319)
    with patch('apxtool.debug_cli.USBTransport'), patch('apxtool.debug_cli.DebugClient', return_value=debug):
        run(args)
    assert debug.request.call_args.kwargs['address'] == 8


def test_sensor_commands_validate_and_preserve_protocol():
    debug=Mock()
    debug.request.side_effect=lambda op,address,**kw:b'{"sequence":4}' if address==0 else b''
    printer=PrinterClient(debug)
    printer.check();assert printer.sequence==5
    assert debug.request.call_args.kwargs['address']==7
    printer.temperature_limit(10)
    assert debug.request.call_args.kwargs['payload']==struct.pack('<I',10)
    printer.probe_adc(1);assert printer.sequence==5
    assert debug.request.call_args.kwargs['address']==10
    printer.shutdown();assert debug.request.call_args.kwargs['address']==8
    for value in (-1,66,True):
        with pytest.raises(ValueError):printer.temperature_limit(value)
    for value in (-1,12,True):
        with pytest.raises(ValueError):printer.probe_adc(value)
    printer.energy(200)
    assert debug.request.call_args.kwargs['address']==11
    assert debug.request.call_args.kwargs['payload']==struct.pack('<I',200)
    printer.energy(0)
    assert debug.request.call_args.kwargs['payload']==struct.pack('<I',0)
    for value in (1,99,401,-1,True):
        with pytest.raises(ValueError):printer.energy(value)


def test_gray_dot_selection_and_luma_upload():
    debug = Mock()
    debug.request.side_effect = lambda op, address, **kw: b'{"sequence":9}' if address == 0 else b''
    printer = PrinterClient(debug)
    for name, value in [('gray', 0), ('dot', 1)]:
        printer.mode(name)
        assert debug.request.call_args.kwargs['address'] == 13
        assert debug.request.call_args.kwargs['payload'] == struct.pack('<I', value)
    data = bytes(range(256)) * 192
    debug.reset_mock()
    printer.image(data, 384, 128)
    calls = debug.request.call_args_list
    assert calls[0].kwargs['address'] == 14
    assert calls[0].kwargs['payload'] == struct.pack('<II', 384, 128)
    chunks = [c.kwargs['payload'] for c in calls if c.kwargs['address'] == 15]
    assert b''.join(c[4:] for c in chunks) == data
    assert [struct.unpack_from('<I', c)[0] for c in chunks] == list(range(0, len(data), 4092))
    assert calls[-1].kwargs['address'] == 16 and printer.sequence == 10
    debug.reset_mock()
    for name in [True, 0, 'GRAY', '', None]:
        with pytest.raises(ValueError): printer.mode(name)
    for data, w, h in [(b'', 1, 1), (b'\0', True, 1), (b'', 385, 1), (b'', 1, 4097), (bytes(384*129), 384, 129)]:
        with pytest.raises(ValueError): printer.image(data, w, h)
    debug.request.assert_not_called()


def test_gray_dot_cli_selects_mode_before_photo():
    from apxtool.debug_cli import run
    from types import SimpleNamespace
    debug = Mock()
    args = SimpleNamespace(command='debug', debug_command='printer', printer_command='photo',
                           mode='dot', port='7-1', vid=0x1908, pid=0x3319)
    with patch('apxtool.debug_cli.USBTransport'), patch('apxtool.debug_cli.DebugClient', return_value=debug), \
         patch('apxtool.debug_cli.PrinterClient') as client:
        client.return_value.wait.return_value = {'state': 4, 'error': 0}
        run(args)
    assert client.return_value.method_calls[:2] == [call.mode('dot'), call.photo()]
