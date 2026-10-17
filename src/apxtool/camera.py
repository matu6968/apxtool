"""Independent sensor capture, display preview and still JPEGs over APXD."""
import json
import struct
import time
from .debug import Operation,MAX_PAYLOAD
from .errors import ApxError,DebugCommandError,TransferError
from .storage import StorageClient
SIZES=((220,176),(320,240),(640,480),(1280,720),(1280,960),(1600,1200),(1920,1080),(2048,1536))

class CameraClient:
    def __init__(self,debug):self.debug=debug
    def _request(self,command,data=b''):
        try:return self.debug.request(Operation.CAMERA,address=command,length=len(data),payload=data)
        except DebugCommandError as e:
            names=('OK','INVALID','RANGE','BUSY','TIMEOUT','HARDWARE','UNSUPPORTED','NACK')
            if 0x500<=e.status<0x500+len(names):raise ApxError('Camera: '+names[e.status-0x500]) from e
            if 0x520<=e.status<0x540:raise ApxError(f'Camera JPEG status: {e.status-0x520}') from e
            if 0x100<=e.status<0x200:raise ApxError(f'Camera storage status: {e.status:#x}') from e
            raise
    def _empty(self,command,data=b''):
        if self._request(command,data):raise TransferError('unexpected camera payload')
    def info(self):return json.loads(self._request(0))
    def preview(self,enabled=True):
        if type(enabled)!=bool:raise ValueError('preview requires a boolean')
        self._empty(1,struct.pack('<I',int(enabled)))
    def capture(self,width=640,height=480,selector=8):
        if any(type(x)!=int for x in (width,height,selector)) or (width,height) not in SIZES or not 0<=selector<=15:
            raise ValueError('unsupported camera photo size or JPEG selector (0..15)')
        self._empty(2,struct.pack('<III',width,height,selector))
    def capture_raw(self):self._empty(6)
    def stop(self):self._empty(5)
    def wait(self,timeout=15,raw=False):
        if not 0<timeout<=3600:raise ValueError('invalid camera timeout')
        end=time.monotonic()+timeout
        while True:
            i=self.info()
            if i['job']==(7 if raw else 4):
                if not i['guard_ok']:raise ApxError('camera DMA guard changed')
                return i
            if i['job']==5:raise ApxError(f'camera failed: {i["error"]}, status {i["status"]:#x}')
            if i['job'] not in (2,3,6):raise ApxError('no camera capture is active')
            if time.monotonic()>=end:raise ApxError('camera did not finish before host timeout')
            time.sleep(.01)
    def read(self,offset,length,*,raw=False):
        limit=460800 if raw else 524288
        if type(offset)!=int or type(length)!=int or offset<0 or length<0 or offset>limit or length>limit-offset:
            raise ValueError('invalid camera buffer range')
        data=bytearray()
        while len(data)<length:
            n=min(MAX_PAYLOAD,length-len(data))
            block=self._request(7 if raw else 3,struct.pack('<II',offset+len(data),n))
            if len(block)!=n:raise TransferError('short camera buffer response')
            data.extend(block)
        return bytes(data)
    def save(self,path):self._empty(4,StorageClient._path(path))
    def download(self,output,*,raw=False):
        i=self.wait(raw=raw);data=self.read(0,i['bytes'] if raw else i['jpeg_bytes'],raw=raw)
        with open(output,'xb') as file:file.write(data)
        return i
