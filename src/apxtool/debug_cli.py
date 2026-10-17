import json
from pathlib import Path
import time
from .debug import USBTransport,DebugClient,Space
from .sdk import launch,scsi_port,RESERVATIONS,DISPLAY_RESERVATIONS,ALLOWED_RESERVATIONS
from .errors import ApxError,DeviceError
from .transport import iter_sg_paths,LinuxSCSITransport,inquiry
from .ram import read_ram,write_ram_experimental
from .storage import StorageClient
from .gpio import GPIOClient,button_names
from .system import SystemClient
from .jpeg import JPEGClient
from .audio import AudioClient,RATES
from .microphone import MicrophoneClient
from .camera import CameraClient
from .sdk_flash import FlashClient,CAPACITY
from .battery import BatteryClient
from .uart import UARTClient

def configure(parser,sub):
    sdk=sub.add_parser('sdk',help='launch the independent RAM SDK')
    commands=sdk.add_subparsers(dest='sdk_command',required=True)
    start=commands.add_parser('launch')
    start.add_argument('manifest',type=Path,nargs='?',default=Path(__file__).resolve().parents[3]/'appotech-sdk/build/usb-debug.manifest.json')
    start.add_argument('--timeout',type=float,default=20)
    start.add_argument('--reclaim-jpeg-ram',action='store_true',help='back up and clear the fixed JPEG workspace if vendor RAM occupies it')
    debug=sub.add_parser('debug',help='APXD vendor USB debugging')
    debug.add_argument('--port',help='physical USB port, e.g. 7-1')
    debug.add_argument('--vid',type=lambda s:int(s,0),default=0x1908)
    debug.add_argument('--pid',type=lambda s:int(s,0),default=0x3319)
    commands=debug.add_subparsers(dest='debug_command',required=True)
    commands.add_parser('info');commands.add_parser('logs');commands.add_parser('bootrom')
    integer=lambda s:int(s,0)
    for name in ('read','write'):
        command=commands.add_parser(name)
        command.add_argument('address',type=integer)
        command.add_argument('--space',choices=('memory','mmio'),default='memory')
        command.add_argument('--width',type=int,choices=(1,2,4),default=1)
        if name=='read':
            command.add_argument('length',type=integer)
            command.add_argument('--output',type=Path)
        else:
            command.add_argument('input',type=Path)
            command.add_argument('--raw',action='store_true')
    for name in ('spr-read','spr-write'):
        command=commands.add_parser(name);command.add_argument('address',type=integer)
        if name=='spr-write':command.add_argument('value',type=integer)
    sd=commands.add_parser('sd',help='SD/FatFs reads, writes and formatting through the independent SDK driver')
    storage=sd.add_subparsers(dest='storage_command',required=True)
    storage.add_parser('info');storage.add_parser('volume')
    listing=storage.add_parser('list');listing.add_argument('path',nargs='?',default='0:/')
    download=storage.add_parser('get');download.add_argument('path');download.add_argument('output',type=Path)
    sectors=storage.add_parser('read-sector');sectors.add_argument('sector',type=integer)
    sectors.add_argument('--count',type=int,default=1);sectors.add_argument('--output',type=Path,required=True)
    cp=storage.add_parser('codepage');cp.add_argument('codepage',type=int)
    upload=storage.add_parser('put');upload.add_argument('input',type=Path);upload.add_argument('path')
    upload.add_argument('--overwrite',action='store_true',help='replace an existing card file')
    raw=storage.add_parser('write-sector',help='write raw sectors; unmounts the SDK filesystem first')
    raw.add_argument('sector',type=integer);raw.add_argument('input',type=Path)
    for name in ('mkdir','unlink'):
        item=storage.add_parser(name);item.add_argument('path')
    rename=storage.add_parser('rename');rename.add_argument('path');rename.add_argument('new_path')
    label=storage.add_parser('label');label.add_argument('label')
    format_card=storage.add_parser('format',help='erase the card directory and create a new FAT/exFAT volume')
    format_card.add_argument('--filesystem',choices=('auto','fat','fat32','exfat'),default='fat')
    format_card.add_argument('--cluster-bytes',type=integer,default=0)
    format_card.add_argument('--superfloppy',action='store_true',help='omit the default MBR partition table')
    storage.add_parser('unmount');storage.add_parser('sync')
    uart=commands.add_parser('uart',help='independent UART0 binary input/output')
    serial=uart.add_subparsers(dest='uart_command',required=True);serial.add_parser('info');serial.add_parser('stop')
    setup=serial.add_parser('configure');setup.add_argument('--baud',type=int,default=115200)
    setup.add_argument('--tx-route',type=int,default=6,help='stock selector6: PA7; selector1: alternate stock route')
    setup.add_argument('--rx-route',type=int,help='explicit receive mux selector; omitted means TX only')
    mirror=serial.add_parser('console');mirror.add_argument('state',choices=('on','off'))
    write=serial.add_parser('write');write.add_argument('input',type=Path)
    read=serial.add_parser('read');read.add_argument('output',type=Path);read.add_argument('--length',type=int,default=4096)
    commands.add_parser('battery',help='battery voltage, level and external supply')
    flash=commands.add_parser('flash',help='read or replace SPI firmware using the independent SDK driver')
    nor=flash.add_subparsers(dest='flash_command',required=True);nor.add_parser('info')
    read=nor.add_parser('read');read.add_argument('output',type=Path)
    read.add_argument('--address',type=integer,default=0);read.add_argument('--length',type=integer,default=CAPACITY)
    install=nor.add_parser('install',help='replace the entire SPI firmware; preserves a new verified backup first')
    install.add_argument('image',type=Path);install.add_argument('--backup',type=Path,required=True)
    install.add_argument('--expect-current-sha256',required=True)
    gpio=commands.add_parser('gpio',help='board buttons and status LED')
    io=gpio.add_subparsers(dest='gpio_command',required=True)
    io.add_parser('info');io.add_parser('events')
    led=io.add_parser('led');led.add_argument('state',choices=('on','off'))
    watch=io.add_parser('watch');watch.add_argument('--seconds',type=float,default=10)
    jpeg=commands.add_parser('jpeg',help='hardware JPEG / stb image preview and sample encoding')
    codecs=jpeg.add_subparsers(dest='jpeg_command',required=True)
    codecs.add_parser('info');codecs.add_parser('cancel')
    preview=codecs.add_parser('preview');preview.add_argument('path')
    preview.add_argument('--allow-slow',action='store_true',help='allow larger/high-precision software images; rendering can take minutes and page to SD')
    encode=codecs.add_parser('encode-sample');encode.add_argument('path');encode.add_argument('width',type=int);encode.add_argument('height',type=int)
    encode.add_argument('--selector',type=int,choices=range(16),default=8)
    audio=commands.add_parser('audio',help='independent speaker PCM, WAV and tone playback')
    sound=audio.add_subparsers(dest='audio_command',required=True)
    sound.add_parser('info');sound.add_parser('stop')
    tone=sound.add_parser('tone');tone.add_argument('--frequency',type=int,default=440);tone.add_argument('--milliseconds',type=int,default=1000)
    tone.add_argument('--rate',type=int,choices=RATES,default=16000)
    wav=sound.add_parser('wav');wav.add_argument('path')
    pcm=sound.add_parser('pcm');pcm.add_argument('input',type=Path);pcm.add_argument('--rate',type=int,choices=RATES,default=16000)
    volume=sound.add_parser('volume');volume.add_argument('percent',type=int)
    mute=sound.add_parser('mute');mute.add_argument('state',choices=('on','off'))
    camera=commands.add_parser('camera',help='live sensor preview and still JPEG capture')
    image=camera.add_subparsers(dest='camera_command',required=True)
    image.add_parser('info')
    image.add_parser('stop')
    preview=image.add_parser('preview');preview.add_argument('state',choices=('on','off'))
    raw=image.add_parser('raw');raw.add_argument('output',type=Path)
    snap=image.add_parser('capture');snap.add_argument('--width',type=int,default=640);snap.add_argument('--height',type=int,default=480)
    snap.add_argument('--selector',type=int,default=8);snap.add_argument('--output',type=Path);snap.add_argument('--save',help='new file on the camera SD card')
    get=image.add_parser('get');get.add_argument('output',type=Path)
    save=image.add_parser('save');save.add_argument('path')
    mic=commands.add_parser('microphone',help='independent microphone capture and WAV output')
    record=mic.add_subparsers(dest='microphone_command',required=True)
    record.add_parser('info');record.add_parser('stop');record.add_parser('play')
    capture=record.add_parser('capture');capture.add_argument('--bytes',type=int,default=32000)
    capture.add_argument('--rate',type=int,choices=RATES,default=16000)
    capture.add_argument('--output',type=Path,help='save a host WAV; existing files are refused')
    get=record.add_parser('get');get.add_argument('output',type=Path)
    save=record.add_parser('save');save.add_argument('path',help='create a new WAV on the SD card')
    system=commands.add_parser('system',help='SDK watchdog, graceful reboot and board power-off')
    actions=system.add_subparsers(dest='system_command',required=True)
    actions.add_parser('info');actions.add_parser('reboot');actions.add_parser('power-off')
    watchdog=actions.add_parser('watchdog');wd=watchdog.add_subparsers(dest='watchdog_command',required=True)
    start=wd.add_parser('start');start.add_argument('--selector',type=int,choices=range(8),default=5)
    start.add_argument('--manual',action='store_true',help='only explicit feeds; polling peripherals does not feed')
    wd.add_parser('feed');wd.add_parser('stop')

def wait_bootrom(port,timeout=20,owned_ram=None):
    allowed=[[[base,base+length] for base,length in r] for r in ALLOWED_RESERVATIONS]
    if owned_ram is not None and owned_ram not in allowed:
        raise ValueError('unexpected SDK RAM ownership; release refused')
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        for path in iter_sg_paths():
            try:
                if scsi_port(path)!=port:continue
                with LinuxSCSITransport(path) as device:
                    if inquiry(device).is_loader:
                        read_ram(device,0x02000000,16)
                        if owned_ram is not None:
                            # The SDK launch preflight required zero RAM. The ACK
                            # and loader identity establish that SDK execution ended.
                            for base,end in owned_ram:
                                length=end-base
                                write_ram_experimental(device,base,bytes(length))
                                if any(read_ram(device,base,length)):
                                    raise DeviceError('SDK RAM release readback failed')
                        return path
            except (OSError,ApxError):pass
        time.sleep(.1)
    raise DeviceError('BootROM did not return with working RAM access')

def run(args):
    if args.command=='sdk':
        if not args.device:raise ValueError('sdk launch requires --device /dev/sgN')
        transport,info=launch(args.device,args.manifest,args.timeout,reclaim_jpeg_ram=args.reclaim_jpeg_ram)
        try:print(json.dumps(info,indent=2))
        finally:transport.close()
        return 0
    transport=USBTransport(port=args.port,vid=args.vid,pid=args.pid)
    client=DebugClient(transport);port=transport.port
    try:
        info=client.info()
        if args.debug_command=='info':print(json.dumps(info,indent=2))
        elif args.debug_command=='logs':print(client.logs().decode('utf-8',errors='replace'),end='')
        elif args.debug_command=='bootrom':client.bootrom()
        elif args.debug_command=='read':
            if args.length>256 and args.output is None:raise ValueError('reads over 256 bytes require --output')
            data=client.read(args.address,args.length,space=Space[args.space.upper()],width=args.width)
            if args.output:
                with args.output.open('xb') as output:output.write(data)
            else:print(data.hex(' '))
        elif args.debug_command=='write':
            data=args.input.read_bytes()
            client.write(args.address,data,space=Space[args.space.upper()],width=args.width,raw=args.raw)
            print(f'wrote {len(data)} bytes')
        elif args.debug_command=='spr-read':print(f'0x{int.from_bytes(client.read(args.address,4,space=Space.SPR,width=4),"little"):08x}')
        elif args.debug_command=='spr-write':client.write(args.address,args.value.to_bytes(4,'little'),space=Space.SPR,width=4)
        elif args.debug_command=='uart':
            uart=UARTClient(client);action=args.uart_command
            if action=='info':print(json.dumps(uart.info(),indent=2))
            elif action=='configure':uart.configure(args.baud,args.tx_route,args.rx_route)
            elif action=='stop':uart.stop()
            elif action=='console':uart.console(args.state=='on')
            elif action=='write':uart.write(args.input.read_bytes())
            elif action=='read':
                with args.output.open('xb') as file:file.write(uart.read(args.length))
        elif args.debug_command=='battery':print(json.dumps(BatteryClient(client).info(),indent=2))
        elif args.debug_command=='flash':
            flash=FlashClient(client)
            if args.flash_command=='info':print(json.dumps(flash.info(),indent=2))
            elif args.flash_command=='read':flash.download(args.output,args.address,args.length)
            else:print(json.dumps(flash.replace(args.image.read_bytes(),args.backup,expected_sha256=args.expect_current_sha256),indent=2))
        elif args.debug_command=='gpio':
            gpio=GPIOClient(client)
            if args.gpio_command=='info':print(json.dumps(gpio.info(),indent=2))
            elif args.gpio_command=='led':gpio.led(args.state=='on')
            elif args.gpio_command=='events':print(json.dumps(gpio.events(),indent=2))
            elif args.gpio_command=='watch':
                if not 0<args.seconds<=3600:raise ValueError('watch duration must be 0..3600 seconds')
                end=time.monotonic()+args.seconds
                while time.monotonic()<end:
                    for event in gpio.events():
                        print(json.dumps({**event,'pressed_names':button_names(event['pressed']),
                                          'released_names':button_names(event['released'])}),flush=True)
                    time.sleep(.01)
        elif args.debug_command=='camera':
            camera=CameraClient(client);action=args.camera_command
            if action=='info':print(json.dumps(camera.info(),indent=2))
            elif action=='stop':camera.stop()
            elif action=='preview':camera.preview(args.state=='on')
            elif action=='save':camera.save(args.path)
            elif action in ('get','raw','capture'):
                output=getattr(args,'output',None)
                if output is not None and output.exists():raise FileExistsError(output)
                if action=='raw':camera.capture_raw()
                elif action=='capture':camera.capture(args.width,args.height,args.selector)
                info=camera.wait(raw=action=='raw')
                if output is not None:camera.download(output,raw=action=='raw')
                if getattr(args,'save',None):camera.save(args.save)
                print(json.dumps(info,indent=2))
        elif args.debug_command=='microphone':
            mic=MicrophoneClient(client);action=args.microphone_command
            if action=='info':print(json.dumps(mic.info(),indent=2))
            elif action=='stop':mic.stop()
            elif action=='play':mic.play();print(json.dumps(AudioClient(client).wait(),indent=2))
            elif action=='save':mic.save(args.path)
            elif action=='capture':
                if args.output is None:mic.start(args.bytes,args.rate);print(json.dumps(mic.wait(),indent=2))
                else:
                    with args.output.open('xb') as out:
                        mic.start(args.bytes,args.rate);print(json.dumps(mic.download(out),indent=2))
            elif action=='get':
                with args.output.open('xb') as out:print(json.dumps(mic.download(out),indent=2))
        elif args.debug_command=='audio':
            audio=AudioClient(client)
            if args.audio_command=='info':print(json.dumps(audio.info(),indent=2))
            elif args.audio_command=='stop':audio.stop()
            elif args.audio_command=='volume':audio.volume(args.percent)
            elif args.audio_command=='mute':audio.mute(args.state=='on')
            else:
                if args.audio_command=='tone':audio.tone(args.frequency,args.milliseconds,args.rate)
                elif args.audio_command=='wav':audio.wav(args.path)
                else:audio.pcm(args.input.read_bytes(),args.rate)
                print(json.dumps(audio.wait(),indent=2))
        elif args.debug_command=='jpeg':
            jpeg=JPEGClient(client)
            if args.jpeg_command=='info':print(json.dumps(jpeg.info(),indent=2))
            elif args.jpeg_command=='cancel':jpeg.cancel()
            else:
                if args.jpeg_command=='preview':jpeg.preview(args.path,allow_slow=args.allow_slow)
                else:jpeg.encode_sample(args.path,args.width,args.height,selector=args.selector)
                print(json.dumps(jpeg.wait(),indent=2))
        elif args.debug_command=='system':
            system=SystemClient(client)
            if args.system_command=='info':print(json.dumps(system.info(),indent=2))
            elif args.system_command=='reboot':system.reboot()
            elif args.system_command=='power-off':system.power_off()
            elif args.watchdog_command=='start':system.watchdog_start(args.selector,automatic=not args.manual)
            elif args.watchdog_command=='feed':system.watchdog_feed()
            elif args.watchdog_command=='stop':system.watchdog_stop()
        elif args.debug_command=='sd':
            storage=StorageClient(client)
            if args.storage_command=='info':print(json.dumps(storage.info(),indent=2))
            elif args.storage_command=='volume':print(json.dumps(storage.volume(),indent=2,ensure_ascii=False))
            elif args.storage_command=='list':print(json.dumps(list(storage.list(args.path)),indent=2,ensure_ascii=False))
            elif args.storage_command=='get':
                storage.volume()
                with args.output.open('xb') as output:count=storage.download(args.path,output)
                print(f'read {count} bytes into {args.output}')
            elif args.storage_command=='read-sector':
                data=storage.sectors(args.sector,args.count)
                with args.output.open('xb') as output:output.write(data)
                print(f'read {len(data)} bytes into {args.output}')
            elif args.storage_command=='codepage':storage.codepage(args.codepage)
            elif args.storage_command=='put':
                with args.input.open('rb') as source:count=storage.upload(args.path,source,overwrite=args.overwrite)
                print(f'wrote {count} bytes to {args.path}')
            elif args.storage_command=='write-sector':
                data=args.input.read_bytes()
                if not data or len(data)%512 or not 0<=args.sector<=0xffffffff or args.sector+len(data)//512>0x100000000:
                    raise ValueError('raw write needs complete sectors and a valid range')
                storage.unmount()
                for offset in range(0,len(data),7*512):storage.write_sectors(args.sector+offset//512,data[offset:offset+7*512])
                print(f'wrote {len(data)//512} sectors')
            elif args.storage_command in ('mkdir','unlink'):
                storage.volume();getattr(storage,args.storage_command)(args.path)
            elif args.storage_command=='rename':storage.volume();storage.rename(args.path,args.new_path)
            elif args.storage_command=='label':storage.volume();storage.label(args.label)
            elif args.storage_command=='format':print(json.dumps(storage.format(args.filesystem,
                cluster_bytes=args.cluster_bytes,superfloppy=args.superfloppy),indent=2,ensure_ascii=False))
            elif args.storage_command=='unmount':storage.unmount()
            elif args.storage_command=='sync':storage.sync()
    finally:transport.close()
    if args.debug_command=='bootrom':print(f'BootROM ready at {wait_bootrom(port,owned_ram=info.get("owned_ram"))}')
    if args.debug_command=='system' and args.system_command in ('reboot','power-off'):
        print(f'{args.system_command} acknowledged on port {port}')
    return 0
