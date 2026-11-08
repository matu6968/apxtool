"""The physical bench must distinguish operator evidence from device state."""
import importlib.util
import struct
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('printer_bench',Path(__file__).parents[1]/'research/silvergear_printer_test.py')
bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)

class Fake:
    def __init__(self,diagnostic='signals-v1'):self.diagnostic=diagnostic;self.requests=[]
    def info(self):return dict(diagnostic=self.diagnostic,phase_name='idle',head_on=0)
    def request(self,operation,**kwargs):self.requests.append((operation,kwargs));return b''
    def read(self,address,length,**kwargs):assert length==4;return struct.pack('<I',address)

def test_signal_identity_and_heater_evidence():
    f=Fake(None)
    with pytest.raises(ValueError):bench.signal_phase(f,f,dict(phase='data-high'))
    assert not f.requests
    f=Fake()
    for c in (dict(phase='strobe-high'),dict(phase='strobe-high',heater_power_off_verified=1),dict(phase='data-high',heater_power_off_verified=True)):
        with pytest.raises(ValueError):bench.signal_phase(f,f,c)
    assert not f.requests
    bench.signal_phase(f,f,dict(phase='strobe-high',heater_power_off_verified=True))
    assert f.requests[0][1]['payload']==struct.pack('<II',5,1)

def test_measurements_remain_operator_reports():
    f=Fake();r=bench.measurement(f,dict(point='verified connector VH pad',ground='verified ground',voltage_v=4.75,observed_phase='supply-idle'))
    assert r['operator_report']['voltage_v']==4.75
    assert r['sdk_readback']['phase_name']=='idle'
    assert r['operator_report']['observed_phase']=='supply-idle'
    assert not f.requests
    for value in (None,True,float('nan'),float('inf')):
        with pytest.raises(ValueError):bench.measurement(f,dict(point='VH',ground='GND',voltage_v=value))
