import hashlib
from types import SimpleNamespace
import struct
import pytest
from apxtool import spi_boot
from apxtool.sdk_flash import FlashClient,CAPACITY
from apxtool.errors import TransferError
from apxtool.debug import Operation

class NOR:
    def __init__(self):
        self.flash=bytearray(b'\xff'*CAPACITY);self.erased=[];self.fail=False
        self.transport=SimpleNamespace(timeout_ms=5000)
    def request(self,operation,*,address,length,payload):
        assert operation==Operation.SPI and len(payload)==length
        if address==0:return b'{"jedec":6176789,"host_read_only":false,"write_protocol":1}'
        if address==1:
            a,n=struct.unpack('<II',payload);return bytes(self.flash[a:a+n])
        assert payload[:4]==b'APXF';a=struct.unpack_from('<I',payload,4)[0]
        if self.fail:raise TransferError('injected disconnect')
        if address==3:self.erased.append(a);self.flash[a:a+4096]=b'\xff'*4096
        else:
            assert address==2 and 0<len(payload)-8<=256-a%256
            for i,b in enumerate(payload[8:]):self.flash[a+i]&=b
        return b''

def test_backup_first_header_last_and_full_verification(tmp_path):
    dev=NOR();client=FlashClient(dev);old=bytes(dev.flash)
    image=bytearray(old);image[1]=42;image[4096:4352]=bytes(range(256))
    state=client.replace(bytes(image),tmp_path/'original.bin',expected_sha256=hashlib.sha256(old).hexdigest())
    assert (tmp_path/'original.bin').read_bytes()==old
    assert dev.erased==[4096,0] and state['status']=='verified' and state['boot_sector_committed']
    assert dev.transport.timeout_ms==5000 and bytes(dev.flash)==image

def test_refusals_and_interrupted_journal(tmp_path):
    dev=NOR();client=FlashClient(dev);image=bytes(CAPACITY)
    with pytest.raises(ValueError):client.replace(b'',tmp_path/'short',expected_sha256='0'*64)
    with pytest.raises(TransferError):client.replace(image,tmp_path/'wrong',expected_sha256='0'*64)
    assert not dev.erased and not (tmp_path/'wrong').exists()
    backup=tmp_path/'original';backup.write_bytes(b'keep')
    with pytest.raises(FileExistsError):client.replace(image,backup,expected_sha256='0'*64)
    assert backup.read_bytes()==b'keep' and not dev.erased
    for a,d in [(255,b'12'),(-1,b'a'),(CAPACITY,b'a'),(False,b'a')]:
        with pytest.raises(ValueError):client.program_page(a,d)
    for a in [1,-1,CAPACITY,True]:
        with pytest.raises(ValueError):client.erase_sector(a)
    old=bytes(dev.flash);dev.fail=True
    with pytest.raises(TransferError):client.replace(image,tmp_path/'interrupted',expected_sha256=hashlib.sha256(old).hexdigest())
    import json
    journal=json.loads((tmp_path/'interrupted.json').read_text())
    assert journal['status']=='interrupted' and not journal['boot_sector_committed']
    assert (tmp_path/'interrupted').read_bytes()==old and dev.transport.timeout_ms==5000

def test_pack_bootstrap_and_flat_ram(monkeypatch,tmp_path):
    data=bytearray((i*37)&255 for i in range(CAPACITY));struct.pack_into('<I',data,0x24,17);original=bytes(data)
    monkeypatch.setattr(spi_boot,'PREFIX_SHA256',hashlib.sha256(original[:0x2200]).hexdigest())
    manifest=tmp_path/'camera-demo.manifest.json';manifest.write_text('{}')
    segments=[(0x02180000,b'CODE'*100),(0x02220000,b'TABLE'*100)]
    monkeypatch.setattr(spi_boot,'load_manifest',lambda p:({'spi_entry':0x02180010,'build_id':'test'},segments))
    image,report=spi_boot.build_image(original,manifest)
    assert struct.unpack_from('<II',image,0x20)==(0x02180000,17)
    assert struct.unpack_from('<II',image,0x528)==(0x18400218,0xa8420010)
    assert image[0x2200:0x2390]==b'CODE'*100
    assert image[0xa2200:0xa2200+500]==b'TABLE'*100
    assert not any(image[0x2390:0xa2200])
    changed={*range(0x20,0x24),*range(0x28,0x2c),*range(0x528,0x530)}
    assert all(a in changed or image[a]==original[a] for a in range(0x2200))
    assert image[0x2200+report['load_bytes']:]==original[0x2200+report['load_bytes']:]
    with pytest.raises(ValueError):spi_boot.build_image(b'',manifest)
    segments.append((0x02410000,b'invalid'))
    with pytest.raises(ValueError):spi_boot.build_image(original,manifest)
    segments.pop()
    segments.append((0x02300000,b''))
    with pytest.raises(ValueError):spi_boot.build_image(original,manifest)


def test_overlay_segments_are_stored_in_free_flash_with_a_checked_table(monkeypatch,tmp_path):
    import zlib
    data=bytearray((i*37)&255 for i in range(CAPACITY));struct.pack_into('<I',data,0x24,17)
    data[spi_boot.OVERLAY_TABLE:spi_boot.OVERLAY_TABLE+4096]=b'\xff'*4096
    original=bytes(data)
    monkeypatch.setattr(spi_boot,'PREFIX_SHA256',hashlib.sha256(original[:0x2200]).hexdigest())
    manifest=tmp_path/'lvgl.manifest.json';manifest.write_text('{}')
    code=bytes(range(256))*40;assets=b'ASSET'*3001
    segments=[(0x02180000,b'CODE'*100),(0x02220000,b'T'),(0x022e0000,code),(0x02300000,assets)]
    meta={'spi_entry':0x02180010,'build_id':'test'}
    monkeypatch.setattr(spi_boot,'load_manifest',lambda p:(meta,segments))
    image,report=spi_boot.build_image(original,manifest)
    assert report['load_bytes']==0xa0200 and len(report['overlays'])==2
    first,second=report['overlays']
    assert first['flash_offset']%4096==0 and first['flash_offset']>=0x2200+report['load_bytes']
    assert image[first['flash_offset']:first['flash_offset']+len(code)]==code
    assert image[second['flash_offset']:second['flash_offset']+len(assets)]==assets
    assert second['flash_offset']>=first['flash_offset']+len(code)
    magic,version,count,crc=struct.unpack_from('<IIII',image,spi_boot.OVERLAY_TABLE)
    assert (magic,version,count)==(0x4f585041,1,2)
    body=image[spi_boot.OVERLAY_TABLE+16:spi_boot.OVERLAY_TABLE+16+32]
    assert crc==zlib.crc32(body)
    assert struct.unpack('<IIII',body[:16])==(first['flash_offset'],0x022e0000,len(code),zlib.crc32(code))
    assert struct.unpack('<IIII',body[16:])==(second['flash_offset'],0x02300000,len(assets),zlib.crc32(assets))
    # Images without overlays leave the table sector alone.
    segments[:]=segments[:2]
    plain,_=spi_boot.build_image(original,manifest)
    assert plain[spi_boot.OVERLAY_TABLE:spi_boot.OVERLAY_TABLE+4096]==original[spi_boot.OVERLAY_TABLE:spi_boot.OVERLAY_TABLE+4096]
    # Refusals (each case is otherwise valid, so the named rule is the one that fires).
    base=(0x02180000,b'CODE'*100)
    def refuses(*extra,original=original,message):
        segments[:]=[base,*extra]
        with pytest.raises(ValueError,match=message):spi_boot.build_image(original,manifest)
    refuses((0x022e0000,code),(0x022e0100,assets),message='overlap')
    refuses((0x022e0000,b''),message='fully initialised')
    refuses((0x02410000,b'x'),message='codec-RAM bank')
    refuses((0x02100000,b'x'),message='camera/JPEG')
    meta['segments']=[{'address':0x022e0000,'bytes':100,'memory_bytes':4096}]
    refuses((0x022e0000,b'x'*100),message='fully initialised')
    del meta['segments']
    used=bytearray(original);used[spi_boot.OVERLAY_TABLE]=0
    refuses((0x022e0000,code),original=bytes(used),message='not erased')
    refuses((0x022dfff0,b'M'*16),(0x022e0000,b'y'*0x120000),message='exceeds the free flash')
