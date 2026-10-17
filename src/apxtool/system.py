"""SDK watchdog, graceful reset and board power-off (APXD SYSTEM)."""
import json
import struct
from .debug import Operation
from .errors import TransferError

class SystemClient:
    def __init__(self,debug):self.debug=debug
    def _request(self,command,payload=b''):
        return self.debug.request(Operation.SYSTEM,address=command,length=len(payload),payload=payload)
    def _empty(self,command,payload=b''):
        if self._request(command,payload):raise TransferError('unexpected system response payload')
    def info(self):return json.loads(self._request(0))
    def watchdog_start(self,selector=5,*,automatic=True):
        if type(selector)!=int or not 0<=selector<=7 or type(automatic)!=bool:
            raise ValueError('watchdog selector must be 0..7 and automatic must be boolean')
        self._empty(1,struct.pack('<II',selector,int(automatic)))
    def watchdog_feed(self):self._empty(2)
    def watchdog_stop(self):self._empty(3)
    def reboot(self):self._empty(4)
    def power_off(self):self._empty(5)
