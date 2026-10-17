"""Filtered battery voltage and coarse indication; no charger control."""
import json
from .debug import Operation
class BatteryClient:
    def __init__(self,debug):self.debug=debug
    def info(self):return json.loads(self.debug.request(Operation.BATTERY,address=0,length=0,payload=b''))
