"""Independent finite PCM16 microphone capture over APXD."""
import json,struct,time,wave
from .debug import Operation,MAX_PAYLOAD
from .audio import RATES
from .storage import StorageClient
from .errors import ApxError,DebugCommandError,TransferError
class MicrophoneClient:
    def __init__(self,debug):self.debug=debug
    def _request(self,command,data=b''):
        try:return self.debug.request(Operation.MICROPHONE,address=command,length=len(data),payload=data)
        except DebugCommandError as e:
            names=('OK','INVALID','RANGE','BUSY','TIMEOUT','HARDWARE','UNSUPPORTED')
            if 0x400<=e.status<0x400+len(names):raise ApxError('Microphone: '+names[e.status-0x400]) from e
            if 0x100<=e.status<0x200:raise ApxError(f'Microphone storage status: {e.status:#x}') from e
            raise
    def _empty(self,command,data=b''):
        if self._request(command,data):raise TransferError('unexpected microphone payload')
    def info(self):return json.loads(self._request(0))
    def start(self,bytes=32000,rate=16000):
        if type(bytes)!=int or type(rate)!=int or not 32<=bytes<=65536 or bytes%32 or rate not in RATES:
            raise ValueError('capture requires 32..65536 bytes aligned to 32, at 8/16/32 kHz')
        self._empty(1,struct.pack('<II',bytes,rate))
    def stop(self):self._empty(2)
    def save(self,path):self._empty(4,StorageClient._path(path))
    def play(self):self._empty(5)
    def wait(self,timeout=10):
        if not 0<timeout<=3600:raise ValueError('invalid microphone timeout')
        end=time.monotonic()+timeout
        while True:
            i=self.info()
            if i['state']==3:
                if not i['guard_ok']:raise ApxError('microphone DMA guard changed')
                return i
            if i['state']==4:raise ApxError(f'Microphone job failed: {i["error"]}')
            if i['state'] in (0,1):raise ApxError('no microphone capture is active')
            if time.monotonic()>=end:raise ApxError('microphone did not finish before host timeout')
            time.sleep(.01)
    def read(self,offset,length):
        if type(offset)!=int or type(length)!=int or offset<0 or length<0 or (offset|length)&1 or offset+length>65536:
            raise ValueError('invalid captured PCM range')
        data=bytearray()
        while len(data)<length:
            n=min(MAX_PAYLOAD,length-len(data))
            block=self._request(3,struct.pack('<II',offset+len(data),n))
            if len(block)!=n:raise TransferError('short captured PCM response')
            data.extend(block)
        return bytes(data)
    def download(self,output):
        i=self.wait();data=self.read(0,i['bytes'])
        with wave.open(output,'wb') as file:
            file.setnchannels(1);file.setsampwidth(2);file.setframerate(i['rate']);file.writeframes(data)
        return i
