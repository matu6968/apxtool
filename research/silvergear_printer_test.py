#!/usr/bin/env python3
"""RAM-only printer bench. Starts with sensor check only; JSON stdin selects later jobs.

Record the actual paper and ribbon connections with --setup. The startup CHECK
does not feed or heat. No SPI/storage/camera/display commands are issued by this
bench. Keep USB connected unless the selected diagnostic explicitly requires removal.
Commands: {"op":"check"}, {"op":"feed","steps":16},
{"op":"raster","file":"/absolute/raster.bin","width":384,"height":32},
{"op":"limit","index":10}, {"op":"cancel"}, {"op":"shutdown"}, {"op":"recover"}.
{"op":"reload","manifest":"/absolute/new.manifest.json","output":"/absolute/new-run"}
performs verified recovery and starts the new RAM bench without an interactive gap.
Lowered temperature limits exercise the fault path without heating the head.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import select
import shutil
import struct
import sys
import time

from apxtool.battery import BatteryClient
from apxtool.camera import CameraClient
from apxtool.debug import DebugClient, Operation, Space, USBTransport
from apxtool.debug_cli import wait_bootrom
from apxtool.errors import ApxError
from apxtool.printer import PrinterClient
from apxtool.rom_rescue import loader_sg_paths
from apxtool.sdk import launch, load_manifest, scsi_port

SIGNAL_PHASES=('idle','supply-idle','data-high','clock-high','latch-low','strobe-high')

def gpio_snapshot(client):
    registers=[base+4*bank for base in (0x8000,0x8040,0x8080,0x80c0,0x8100,0x8140,0x81c0,0x8200,0x8380)
               for bank in (0,1,3,4)]
    registers.append(0x8340)
    return {hex(reg):hex(struct.unpack('<I',client.read(reg,4,space=Space.SPR,width=4))[0]) for reg in registers}

def signal_phase(client,printer,command):
    """Diagnostic-only protocol, never silently sent to a production printer."""
    if printer.info().get('diagnostic')!='signals-v1':
        raise ValueError('diagnostic phases require printer-signal-test firmware')
    phase=command.get('phase')
    if phase not in SIGNAL_PHASES:raise ValueError('unknown signal phase')
    verified=command.get('heater_power_off_verified',False)
    if type(verified)!=bool:raise ValueError('heater_power_off_verified must be a boolean')
    if phase=='strobe-high' and not verified:
        raise ValueError('strobe-high requires a fresh electrical measurement confirming heater power is off')
    if phase!='strobe-high' and verified:raise ValueError('heater-off evidence is only used for strobe-high')
    payload=struct.pack('<II',SIGNAL_PHASES.index(phase),int(verified))
    if client.request(Operation.PRINTER,address=12,length=8,payload=payload):
        raise ValueError('unexpected signal diagnostic response')
    return dict(printer=printer.info(),spr_readback=gpio_snapshot(client))

def measurement(printer,command):
    """Keep operator readings separate from firmware observations."""
    voltage=command.get('voltage_v')
    if type(voltage) not in (int,float) or not math.isfinite(voltage):
        raise ValueError('voltage_v must be a finite number')
    for field in ('point','ground'):
        if not isinstance(command.get(field),str) or not command[field].strip():
            raise ValueError(field+' must identify the actual probe contact')
    observed=command.get('observed_phase')
    if observed is not None and observed not in SIGNAL_PHASES:raise ValueError('unknown observed_phase')
    return dict(operator_report=dict(point=command['point'],ground=command['ground'],voltage_v=voltage,
                observed_phase=observed,note=command.get('note','')),sdk_readback=printer.info(),
                timing='SDK status captured when the operator submitted the reading; not a synchronized waveform')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--stock',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--timeout',type=float,default=300)
    parser.add_argument('--attach',action='store_true')
    parser.add_argument('--no-check',action='store_true',help='after a fresh launch do not start the automatic CHECK (the firmware runs its own sequence)')
    parser.add_argument('--observe',action='store_true',help='with --attach, preserve the running diagnostic result instead of starting CHECK')
    parser.add_argument('--recovered',type=Path,help='reuse DDR immediately after this verified SDK recovery record, without a power cycle')
    parser.add_argument('--setup',type=Path,help='operator-recorded connections, battery conditions, probe map and prior stock measurements (JSON object)')
    args=parser.parse_args(argv)
    if args.observe and not args.attach:parser.error('--observe requires --attach')
    manifest,_=load_manifest(args.manifest)
    setup=json.loads(args.setup.read_text()) if args.setup else None
    if setup is not None and not isinstance(setup,dict):raise ValueError('--setup must contain a JSON object')
    if args.recovered:
        previous=json.loads(args.recovered.read_text())
        if not previous.get('bootrom_after') or previous.get('error') or previous.get('apxd_info',{}).get('board')!='silvergear-3222882-jt5295':
            raise ValueError('a successful Silvergear SDK BootROM recovery record is required')
        if time.time()-args.recovered.stat().st_mtime>60:
            raise ValueError('DDR reuse requires the immediately preceding recovery, within 60 seconds')
        if setup is None:setup=previous.get('physical_setup',{}).get('details')
    if manifest.get('board')!='silvergear-3222882':raise ValueError('Silvergear manifest required')
    args.output.mkdir(parents=True,exist_ok=False)
    archive=args.output/'firmware';archive.mkdir()
    for name in [args.manifest.name]+[s['file'] for s in manifest['segments']]:
        shutil.copy2(args.manifest.parent/name,archive/name)
    elf=args.manifest.with_name(args.manifest.name.replace('.manifest.json','.elf'))
    if elf.exists():shutil.copy2(elf,archive/elf.name)
    result={'build_id':manifest['build_id'],'flash_access':'none','display_camera_commands':'none','events':[]}
    result['physical_setup']={'source':'operator-provided' if setup is not None else 'unrecorded','details':setup}
    if args.recovered and not args.setup and setup is not None:
        result['physical_setup']['source']='inherited operator report'
        result['physical_setup']['inherited_from']=str(args.recovered.resolve())
    if setup is not None:(args.output/'physical-setup.json').write_text(json.dumps(setup,indent=2)+'\n')
    report=args.output/'result.json'
    started=time.monotonic()
    def log(event,detail=None):
        result['events'].append({'seconds':round(time.monotonic()-started,3),'event':event,'detail':detail})
        report.write_text(json.dumps(result,indent=2)+'\n')
        print(event,detail if detail is not None else '',flush=True)
    transport=client=printer=None
    try:
        if args.attach:
            transport=USBTransport();port=transport.port;info=DebugClient(transport).info()
        else:
            log('WAITING_FOR_BOOTROM')
            deadline=time.monotonic()+args.timeout
            while time.monotonic()<deadline:
                paths=loader_sg_paths()
                if len(paths)>1:raise ApxError('multiple BootROM cameras')
                if paths:break
                time.sleep(.01)
            else:raise ApxError('no BootROM device appeared')
            port=scsi_port(paths[0]);log('VERIFIED_UPLOAD_REUSING_RECOVERED_DDR' if args.recovered else 'POWER_DDR_VERIFIED_UPLOAD')
            if args.recovered and previous.get('port',port)!=port:raise ApxError('recovered physical USB port mismatch')
            transport,info=launch(paths[0],args.manifest,timeout=30,stock_backup=None if args.recovered else args.stock)
        if info.get('build_id')!=manifest['build_id'] or info.get('board')!='silvergear-3222882-jt5295' or not info.get('printer'):
            raise ApxError('APXD identity mismatch')
        result['apxd_info']=info;result['port']=port
        if args.recovered:result['ddr_recovery_record']=str(args.recovered)
        log('APXD_HANDSHAKE_PASSED',info['build_id'])
        client=DebugClient(transport);printer=PrinterClient(client);battery=BatteryClient(client)
        last_log_text=client.logs().decode('utf-8','replace');last_logs=time.monotonic()
        log('SDK_LOG',last_log_text)
        if args.observe:log('OBSERVING_RUNNING_PRINTER')
        elif args.no_check:log('NO_AUTOMATIC_CHECK')
        else:printer.check();log('SENSOR_ONLY_CHECK_STARTED')
        last=None;last_report=0;last_battery=0
        pause_until=0;waiting=not(args.observe or args.no_check)  # script ops 'sleep'/'wait' hold back stdin; the first command waits for the startup CHECK
        while True:
            now=time.monotonic();status=printer.info()
            key=tuple(status.get(k) for k in ('state','error','sequence'))
            if key!=last or status['state'] in (2,3) and now-last_report>=1:
                log('PRINTER_STATUS',status);last=key;last_report=now
                if status['state'] not in (2,3):
                    if any(status.get(k) for k in ('head_on','motor_on','power_on')):
                        raise ApxError('terminal job retained an active output')
            if now-last_logs>=2:
                # Diagnostics print after the CHECK finishes; keep new SDK log text in the report.
                text=client.logs().decode('utf-8','replace');last_logs=now
                if text!=last_log_text:
                    log('SDK_LOG',text[len(last_log_text):] if text.startswith(last_log_text) else text);last_log_text=text
            if now-last_battery>=5:
                last_battery=now
                try:result['last_battery']=battery.info()
                except ApxError:pass  # a diagnostic may shut the battery service down on purpose
                else:
                    if 'initial_battery' not in result:
                        result['initial_battery']=result['last_battery'];log('BATTERY',result['last_battery'])
            if waiting and status['state'] not in (2,3) and status.get('sequence',0)>=1:waiting=False
            if waiting or now<pause_until:time.sleep(.05)
            elif select.select([sys.stdin],[],[],.05)[0]:
                line=sys.stdin.readline()
                if not line:raise ApxError('stdin closed; shutting printer down')
                try:
                    command=json.loads(line);op=command['op'];log('COMMAND',command)
                    if op=='check':printer.check()
                    elif op=='adc':printer.probe_adc(command['channel'])
                    elif op=='feed':printer.feed(command['steps'])
                    elif op in ('raster','image'):
                        path=Path(command['file']);data=path.read_bytes()
                        copy=args.output/('raster-%d.bin'%len(result['events']))
                        copy.write_bytes(data)
                        log('RASTER',{'file':str(copy),'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'format':'luma8' if op=='image' else 'mono1'})
                        if op=='image':printer.image(data,command['width'],command['height'])
                        else:printer.raster(data,command['width'],command['height'])
                    elif op=='limit':printer.temperature_limit(command['index'])
                    elif op=='energy':printer.energy(command['percent'])
                    elif op=='mode':printer.mode(command['mode'])
                    elif op=='sleep':pause_until=time.monotonic()+float(command['seconds'])
                    elif op=='wait':waiting=True
                    elif op=='cancel':printer.cancel()
                    elif op=='shutdown':printer.shutdown()
                    elif op=='diagnostic':log('SIGNAL_PHASE',signal_phase(client,printer,command))
                    elif op=='measurement':log('METER_READING',measurement(printer,command))
                    elif op=='camera-info':
                        result['display_camera_commands']='camera INFO (read-only)'
                        log('CAMERA_INFO',CameraClient(client).info())
                    elif op=='detach':
                        status=printer.info()
                        if status['state'] in (2,3) or any(status.get(k) for k in ('head_on','motor_on','power_on')):
                            raise ValueError('detach requires an idle printer with all actuator outputs off')
                        log('DETACHED_IDLE',status);transport.close();transport=None
                        return 0
                    elif op=='info':
                        log('INFO',{'printer':printer.info(),'battery':battery.info(),'sdk':client.info()})
                        log('SDK_LOG',client.logs().decode('utf-8','replace'))
                    elif op in ('recover','reload'):
                        reload_args=None
                        if op=='reload':
                            target=Path(command['manifest']).resolve();target_manifest,_=load_manifest(target)
                            destination=Path(command['output']).resolve()
                            if target_manifest.get('board')!='silvergear-3222882':raise ValueError('Silvergear reload manifest required')
                            if destination.exists():raise ValueError('reload output already exists')
                            reload_args=['--manifest',str(target),'--stock',str(args.stock.resolve()),
                                '--output',str(destination),'--recovered',str(report.resolve()),'--timeout','10']
                        printer.shutdown();log('FINAL_PRINTER',printer.info())
                        result['last_battery']=battery.info()
                        client.bootrom();transport.close();transport=None
                        result['bootrom_after']=wait_bootrom(port,timeout=20,owned_ram=info.get('owned_ram'))
                        log('BOOTROM_RECOVERY_VERIFIED',result['bootrom_after'])
                        # Return through finally before entering the next bench. Imports
                        # stay warm and there is no interactive gap in the ROM loader.
                        return reload_args if reload_args is not None else 0
                    else:raise ValueError('unknown command')
                except (ApxError,ValueError,KeyError,OSError) as error:
                    log('COMMAND_REJECTED',str(error))
            time.sleep(.01)
    except (ApxError,OSError,ValueError,KeyboardInterrupt) as error:
        result['error']=str(error);log('STOPPED',str(error));return 1
    finally:
        if transport:
            if printer:
                try:printer.shutdown();log('SHUTDOWN',printer.info())
                except Exception as error:result['shutdown_error']=str(error)
            transport.close()
        report.write_text(json.dumps(result,indent=2)+'\n')
        print('Result:',report,flush=True)


if __name__=='__main__':
    outcome=main()
    while isinstance(outcome,list):outcome=main(outcome)
    raise SystemExit(outcome)
