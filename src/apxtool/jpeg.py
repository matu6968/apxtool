"""APXD hardware/software image jobs, using the SDK's SD/display integration."""
import json
import struct
import time
from .debug import Operation
from .errors import ApxError,DebugCommandError,TransferError
from .storage import StorageClient

class JPEGClient:
    def __init__(self,debug):self.debug=debug
    def _request(self,command,data=b''):
        try:return self.debug.request(Operation.JPEG,address=command,length=len(data),payload=data)
        except DebugCommandError as exc:
            names=('OK','INVALID','UNSUPPORTED','RANGE','BUSY','HARDWARE','TIMEOUT','SLOW: software image rendering may take minutes; opt in with --allow-slow')
            if 0x200<=exc.status<0x200+len(names):raise ApxError('JPEG: '+names[exc.status-0x200]) from exc
            if exc.status>=0x100:raise ApxError(f'JPEG storage status: {exc.status:#x}') from exc
            raise
    def info(self):return json.loads(self._request(0))
    def preview(self,path,*,allow_slow=False):
        if type(allow_slow)!=bool:raise ValueError('allow_slow requires a boolean')
        command,data=(4,struct.pack('<I',1)) if allow_slow else (1,b'')
        if self._request(command,data+StorageClient._path(path)):raise TransferError('unexpected JPEG preview payload')
    def encode_sample(self,path,width,height,*,selector=8):
        if not 0<width<=2048 or not 0<height<=1536 or (width|height)&1 or not 0<=selector<=15:
            raise ValueError('invalid JPEG dimensions/selector')
        if self._request(2,struct.pack('<III',width,height,selector)+StorageClient._path(path)):
            raise TransferError('unexpected JPEG encode payload')
    def cancel(self):
        if self._request(3):raise TransferError('unexpected JPEG cancel payload')
    def wait(self,timeout=600):
        if not 0<timeout<=3600:raise ValueError('invalid JPEG job timeout')
        deadline=time.monotonic()+timeout
        while True:
            info=self.info()
            if info['job']==5:return info
            if info['job']==6:raise ApxError(f'JPEG job failed: {info["job_error"]:#x}')
            if info['job']==0:raise ApxError('no JPEG job is active')
            if time.monotonic()>=deadline:raise ApxError('JPEG job did not finish before host timeout')
            # Software decoding shares the CPU with status serialization and SD
            # paging. Leave it time to run while retaining fast hardware waits.
            time.sleep(.25 if info.get('software') else .02)
