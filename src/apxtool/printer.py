"""Cooperative Silvergear raster, photo and paper-feed jobs over APXD."""
import json
import struct
import time
from .debug import Operation,MAX_PAYLOAD
from .errors import ApxError,DebugCommandError,TransferError

class PrinterClient:
    def __init__(self,debug):self.debug=debug;self.sequence=None
    def _request(self,command,payload=b''):
        try:return self.debug.request(Operation.PRINTER,address=command,length=len(payload),payload=payload)
        except DebugCommandError as e:
            names=('OK','INVALID','BUSY','UNSUPPORTED','TEMPERATURE','TIMEOUT','PAPER','IO','POWER')
            if 0x600<=e.status<0x609:raise ApxError('Printer: '+names[e.status-0x600]) from e
            raise
    def _empty(self,command,payload=b''):
        if self._request(command,payload):raise TransferError('unexpected printer payload')
    def info(self):return json.loads(self._request(0))
    def cancel(self):self._empty(1)
    def shutdown(self):self._empty(8)
    def check(self):
        self.sequence=self.info()['sequence']+1;self._empty(7)
    def temperature_limit(self,index):
        if type(index)!=int or not 0<=index<=65:raise ValueError('temperature table limit must be 0..65')
        self._empty(9,struct.pack('<I',index))
    def energy(self,percent):
        if type(percent)!=int or not (percent==0 or 100<=percent<=400):raise ValueError('pulse energy must be 100..400 percent, or 0 for the mode default')
        self._empty(11,struct.pack('<I',percent))
    def mode(self,name):
        if type(name)!=str or name not in ('gray','dot'):raise ValueError('printer mode must be gray or dot')
        self._empty(13,struct.pack('<I',('gray','dot').index(name)))
    def probe_adc(self,channel):
        if type(channel)!=int or not 0<=channel<=11:raise ValueError('ADC channel must be 0..11')
        self.sequence=self.info()['sequence']+1;self._empty(10,struct.pack('<I',channel))
    def feed(self,steps):
        if type(steps)!=int or not 1<=steps<=4096:raise ValueError('feed steps must be 1..4096')
        self.sequence=self.info()['sequence']+1;self._empty(2,struct.pack('<I',steps))
    def photo(self):
        self.sequence=self.info()['sequence']+1;self._empty(6)
    def raster(self,data,width,height):
        if type(width)!=int or type(height)!=int or not 1<=width<=384 or not 1<=height<=1024:
            raise ValueError('raster dimensions must be 1..384 by 1..1024')
        if len(data)!=((width+7)//8)*height:raise ValueError('packed raster length mismatch')
        self._empty(3,struct.pack('<II',width,height))
        for offset in range(0,len(data),MAX_PAYLOAD-4):
            self._empty(4,struct.pack('<I',offset)+data[offset:offset+MAX_PAYLOAD-4])
        self.sequence=self.info()['sequence']+1;self._empty(5)
    def image(self,data,width,height):
        """Upload8-bit luma,0=black, rendered in the currently selected mode."""
        if type(width)!=int or type(height)!=int or not 1<=width<=384 or not 1<=height<=4096:
            raise ValueError('image dimensions must be1..384 by1..4096')
        if len(data)!=width*height or len(data)>49152:raise ValueError('luma upload must match dimensions and fit49152 bytes')
        self._empty(14,struct.pack('<II',width,height))
        for offset in range(0,len(data),MAX_PAYLOAD-4):self._empty(15,struct.pack('<I',offset)+data[offset:offset+MAX_PAYLOAD-4])
        self.sequence=self.info()['sequence']+1;self._empty(16)
    def wait(self,timeout=60):
        if not 0<timeout<=3600:raise ValueError('invalid printer timeout')
        end=time.monotonic()+timeout
        while True:
            info=self.info()
            if self.sequence is None or info['sequence']>=self.sequence:
                if info['state']==4:return info
                if info['state'] in (5,6):raise ApxError(f"printer ended with state {info['state']}, error {info['error']}")
            if time.monotonic()>=end:
                self.cancel();raise ApxError('printer did not finish before host timeout; cancellation sent')
            time.sleep(.01)
