"""APXD independent speaker PCM/tone/WAV output."""
import json,struct,time
from .debug import Operation,MAX_PAYLOAD
from .storage import StorageClient
from .errors import ApxError,DebugCommandError,TransferError
RATES=(8000,16000,32000)
class AudioClient:
    def __init__(self,debug):self.debug=debug
    def _request(self,command,data=b''):
        try:return self.debug.request(Operation.AUDIO,address=command,length=len(data),payload=data)
        except DebugCommandError as e:
            names=('OK','INVALID','RANGE','BUSY','TIMEOUT','HARDWARE','UNSUPPORTED')
            if 0x300<=e.status<0x300+len(names):raise ApxError('Audio: '+names[e.status-0x300]) from e
            if 0x100<=e.status<0x200:raise ApxError(f'Audio storage status: {e.status:#x}') from e
            raise
    def _empty(self,command,data=b''):
        if self._request(command,data):raise TransferError('unexpected audio payload')
    def info(self):return json.loads(self._request(0))
    def volume(self,percent):
        if type(percent)!=int or not 0<=percent<=100:raise ValueError('volume must be 0..100')
        self._empty(4,struct.pack('<I',percent))
    def mute(self,muted=True):
        if type(muted)!=bool:raise ValueError('mute must be boolean')
        self._empty(5,struct.pack('<I',int(muted)))
    def stop(self):self._empty(3)
    def tone(self,frequency=440,milliseconds=1000,rate=16000):
        if any(type(v)!=int for v in (frequency,milliseconds,rate)) or rate not in RATES or not 40<=frequency<=rate//4 or not 1<=milliseconds<=4000 or not 1<=rate*milliseconds//1000<=32752:
            raise ValueError('invalid audio tone frequency/duration/rate')
        self._empty(1,struct.pack('<III',frequency,milliseconds,rate))
    def wav(self,path):self._empty(2,StorageClient._path(path))
    def pcm(self,data,rate=16000):
        if type(rate)!=int or rate not in RATES or not data or len(data)&1 or len(data)>65504:
            raise ValueError('PCM requires 1..32752 signed little-endian mono samples at 8/16/32 kHz')
        for offset in range(0,len(data),MAX_PAYLOAD-4):
            self._empty(6,struct.pack('<I',offset)+data[offset:offset+MAX_PAYLOAD-4])
        self._empty(7,struct.pack('<I',rate))
    def wait(self,timeout=10):
        if not 0<timeout<=3600:raise ValueError('invalid audio timeout')
        end=time.monotonic()+timeout
        while True:
            i=self.info()
            if i['state']==4:return i
            if i['state']==5:raise ApxError(f'Audio job failed: {i["error"]}')
            if i['state'] in (0,1):raise ApxError('no audio playback is active')
            if time.monotonic()>=end:raise ApxError('audio did not finish before host timeout')
            time.sleep(.01)
