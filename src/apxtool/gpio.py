"""Board button events and status LED through the APXD GPIO extension."""
import json
import struct
from .debug import Operation
from .errors import TransferError
BUTTON_NAMES=('SHUTTER','DOWN','UP','LEFT','RIGHT','POWER')
def button_names(mask):
    return [name for bit,name in enumerate(BUTTON_NAMES) if mask&(1<<bit)]
class GPIOClient:
    def __init__(self,debug):self.debug=debug
    def _request(self,command,payload=b''):
        return self.debug.request(Operation.GPIO,address=command,length=len(payload),payload=payload)
    def info(self):return json.loads(self._request(0))
    def led(self,enabled):
        if enabled not in (0,1):raise ValueError('LED state must be 0 or 1')
        if self._request(1,struct.pack('<I',enabled)):raise TransferError('unexpected LED payload')
    def events(self):
        data=self._request(2)
        if len(data)>256 or len(data)%16:raise TransferError('malformed GPIO event payload')
        return [dict(zip(('ticks','state','pressed','released'),struct.unpack_from('<IIII',data,i)))
                for i in range(0,len(data),16)]
