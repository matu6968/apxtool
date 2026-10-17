"""Explicit UART routing and binary queues through APXD; USB stays independent."""
import json,struct,time
from .debug import Operation
from .errors import TransferError
class UARTClient:
    def __init__(self,debug):self.debug=debug
    def _request(self,command,payload=b''):
        return self.debug.request(Operation.UART,address=command,length=len(payload),payload=payload)
    def info(self):return json.loads(self._request(0))
    def configure(self,baud=115200,tx_route=6,rx_route=None):
        if type(baud)!=int or not 1200<=baud<=1000000 or type(tx_route)!=int or not 1<=tx_route<=31 or (rx_route is not None and (type(rx_route)!=int or not 0<=rx_route<=31)):
            raise ValueError('invalid UART baud/route')
        if self._request(1,struct.pack('<IIi',baud,tx_route,-1 if rx_route is None else rx_route)):raise TransferError('unexpected UART configure payload')
    def console(self,enabled=True):
        if type(enabled)!=bool:raise ValueError('console requires a boolean')
        if self._request(5,struct.pack('<I',int(enabled))):raise TransferError('unexpected UART console payload')
    def stop(self):
        if self._request(4):raise TransferError('unexpected UART stop payload')
    def read(self,length=4096):
        if type(length)!=int or not 0<=length<=4096:raise ValueError('invalid UART read length')
        data=self._request(3,struct.pack('<I',length))
        if len(data)>length:raise TransferError('oversized UART response')
        return data
    def write(self,data,timeout=10):
        if not 0<timeout<=3600:raise ValueError('invalid UART timeout')
        end=time.monotonic()+timeout
        for offset in range(0,len(data),256):
            block=data[offset:offset+256]
            while self.info()['tx_queued']>512-len(block):
                if time.monotonic()>end:raise TransferError('UART TX queue did not drain')
                time.sleep(.001)
            if self._request(2,block):raise TransferError('unexpected UART write payload')
        while self.info()['tx_queued']:
            if time.monotonic()>end:raise TransferError('UART TX did not complete')
            time.sleep(.001)
        if self.info()['timeouts']:raise TransferError('UART hardware TX timeout')
