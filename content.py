"""Prepared Xbox content, archive inspection and verified USB copies."""
from __future__ import annotations
import contextlib, ctypes, hashlib, json, os, re, shutil, struct, subprocess, time
from pathlib import Path, PurePosixPath
from core import *

CONTENT_TYPES={2:'DLC / install content',0x7000:'Games on Demand',0xD0000:'Arcade game',0xB0000:'Title update'}
AUX={'.txt','.nfo','.jpg','.jpeg','.png','.sfv','.md','.url'}

def atomic_json(path,data,required=False):
    path=Path(path); temp=path.with_name(path.name+'.pending')
    try:
        temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        for attempt in range(5):
            try: os.replace(temp,path); return True
            except OSError:
                if attempt==4:
                    if required: raise
                    return False
                time.sleep(.05*(attempt+1))
    except OSError:
        if required: raise
        return False

def volumes():
    if os.name!='nt': return []
    k=ctypes.WinDLL('kernel32',use_last_error=True); result=[]
    for letter in 'DEFGHIJKLMNOPQRSTUVWXYZABC':
        root=letter+':\\'
        if k.GetDriveTypeW(root) not in (2,3): continue
        label,fs=ctypes.create_unicode_buffer(261),ctypes.create_unicode_buffer(261)
        serial,maximum,flags=ctypes.c_uint32(),ctypes.c_uint32(),ctypes.c_uint32()
        if not k.GetVolumeInformationW(root,label,261,ctypes.byref(serial),ctypes.byref(maximum),ctypes.byref(flags),fs,261):continue
        try: free=shutil.disk_usage(root).free
        except OSError: continue
        result.append(dict(root=root,label=label.value,serial=serial.value,filesystem=fs.value,free=free))
    return result

class SelectedDrive:
    def __init__(self,config):self.config=config
    def locate(self):
        if not self.config: raise DriveMissing('Choose a USB drive in Settings')
        for v in volumes():
            if v['serial']==self.config['serial'] and v['label']==self.config['label']:
                if v['filesystem']!='FAT32':raise DriveMissing('The selected Xbox USB drive must use FAT32')
                return Path(v['root'])
        raise DriveMissing('Connect the selected USB drive: '+self.config.get('label',''))
    def check(self,root):
        if self.locate()!=Path(root):raise DriveMissing('Selected USB was disconnected or changed')

def input_signature(path):
    path=Path(path)
    if path.is_file():return signature(path)
    rows=[]; total=0
    for rel,n in actual_files(path).items():
        p=safe_path(path/Path(rel),path); s=p.stat();total+=n
        rows.append((rel,n,s.st_mtime_ns))
    return [total,hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(),'folder']

def clean_title(name):
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','-',name).strip().rstrip('.')
    return valid_name(name[:42].rstrip('. ') or 'Xbox game')

def xex_info(path):
    try:
        with Path(path).open('rb') as f:
            h=f.read(0x18)
            if len(h)<0x18 or h[:4]!=b'XEX2':return {}
            count=struct.unpack_from('>I',h,0x14)[0]
            if count>2048:return {}
            table=f.read(count*8)
            for i in range(count):
                key,off=struct.unpack_from('>II',table,i*8)
                if key==0x00040006:
                    f.seek(off);e=f.read(24)
                    media,version,base,title=struct.unpack_from('>IIII',e)
                    return dict(title_id=f'{title:08X}',media_id=f'{media:08X}',version=f'{version:08X}',disc=e[18],discs=e[19])
    except (OSError,ValueError,struct.error):pass
    return {}

def package_info(path):
    with Path(path).open('rb') as f:h=f.read(0x1711)
    if len(h)<0x3AD or h[:4] not in (b'LIVE',b'PIRS',b'CON '):return None
    get=lambda offset:struct.unpack_from('>I',h,offset)[0]
    kind=get(0x344)
    if kind not in CONTENT_TYPES:return None
    name=h[0x411:0x491].decode('utf-16-be',errors='replace').split('\0')[0].strip()
    return dict(title_id=f'{get(0x360):08X}',media_id=f'{get(0x354):08X}',version=f'{get(0x358):08X}',
                kind=kind,name=name or f'{get(0x360):08X}',disc=h[0x366],discs=h[0x367],
                data_files=get(0x39D),data_bytes=struct.unpack_from('>Q',h,0x3A1)[0])

def archive_inventory(tool,path):
    r=subprocess.run([str(tool),'l','-slt','-ba','-sccUTF-8','--',str(path)],capture_output=True,timeout=90,creationflags=0x08000000)
    if r.returncode:raise ValueError('Cannot read archive: '+r.stderr.decode('utf-8','replace')[-500:])
    records=[];seen=set()
    for block in re.split(r'\r?\n\r?\n',r.stdout.decode('utf-8','replace').strip()):
        d=dict(line.split(' = ',1) for line in block.splitlines() if ' = ' in line)
        if 'Path' not in d:continue
        name=d['Path'].replace('\\','/');parts=PurePosixPath(name).parts
        if not parts or name.startswith('/') or any(p in ('.','..') for p in name.split('/')):raise ValueError('Unsafe archive path')
        for p in parts:valid_name(p)
        if name.casefold() in seen:raise ValueError('Duplicate archive path')
        seen.add(name.casefold())
        if d.get('Encrypted')=='+' or any('Link' in k for k in d) or re.search(r'(?:^|\s)l[rwx-]',d.get('Attributes','')):raise ValueError('Linked or encrypted archive content is unsupported')
        if d.get('Folder')=='+' or d.get('Attributes','').startswith('D'):continue
        size=int(d.get('Size',0))
        if size < 0:raise ValueError('Invalid archive member size')
        if len(records) >= 250000:raise ValueError('Archive contains too many files')
        records.append(dict(name=d['Path'],relative=Path(*parts),size=size,crc=d.get('CRC')))
    if not records:raise ValueError('Archive contains no files')
    return records

def unpack_all(tool,archive,folder,records,callback,checkpoint):
    folder.mkdir(parents=True,exist_ok=True);total=sum(r['size'] for r in records)
    # All names and link attributes were checked before allowing 7-Zip to create files.
    log=folder.parent/'unpack.log'
    with log.open('wb') as output:
        proc=subprocess.Popen([str(tool),'x','-y','-aoa','-spd','-bsp0','-bb0','-o'+str(folder),'--',str(archive)],stdout=output,stderr=subprocess.STDOUT,creationflags=0x08000000|0x4000)
        try:
            while proc.poll() is None:
                checkpoint();done=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
                callback('Unpacking',done,total,'Extracting archive contents');time.sleep(.3)
            if proc.returncode:raise ValueError('Archive extraction failed: '+log.read_text(errors='replace')[-800:])
        finally:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=10)
    expected={r['relative'].as_posix().casefold():r['size'] for r in records}
    if actual_files(folder)!=expected:raise ValueError('Extracted archive inventory differs from its listing')
    return folder

def inspect_tree(folder,label=None):
    """Return independent game/content units, ISO inputs, and unhandled files."""
    folder=Path(folder);all_files=[safe_path(p,folder) for p in folder.rglob('*') if p.is_file()]
    # Validate empty directories and reparse points as well.
    actual_files(folder)
    consumed=set();units=[]
    roots=sorted({p.parent for p in all_files if p.name.lower()=='default.xex'},key=lambda p:len(p.parts))
    for p in all_files:
        # A runnable game's files belong to that game, including embedded system
        # updates, installer packages and STFS assets. Only standalone packages
        # outside game folders are routed into the console Content hierarchy.
        if p in consumed or p.suffix.lower()=='.iso' or any(p.is_relative_to(game) for game in roots):continue
        if p.stat().st_size<0x3AD:continue
        with p.open('rb') as f:magic=f.read(4)
        if magic not in (b'LIVE',b'PIRS',b'CON '):continue
        info=package_info(p)
        if not info:continue
        files=[(p,Path(p.name))]
        if info['kind']==0x7000:
            if info['data_files']<1 or info['data_bytes']<1:raise ValueError('GOD descriptor contains no game data')
            data=p.with_name(p.name+'.data')
            if not data.is_dir():raise ValueError('GOD package is missing its .data folder: '+p.name)
            pieces=sorted(data.iterdir())
            if any(not x.is_file() for x in pieces) or [x.name for x in pieces]!=[f'Data{i:04d}' for i in range(len(pieces))]:raise ValueError('GOD data files are missing or not consecutive')
            if len(pieces)!=info['data_files'] or sum(x.stat().st_size for x in pieces)!=info['data_bytes']:raise ValueError('GOD descriptor and data files disagree')
            files.extend((x,Path(data.name)/x.name) for x in pieces)
        consumed.update(x for x,_ in files)
        units.append(dict(name=info['name'],kind=CONTENT_TYPES[info['kind']],metadata=info,files=files,
                          destination=Path('Content')/'0000000000000000'/info['title_id']/f"{info['kind']:08X}",package=p.name))
    for game in roots:
        if game/'default.xex' in consumed:continue
        files=[(p,p.relative_to(game)) for p in all_files if p.is_relative_to(game) and p not in consumed]
        if not files:continue
        metadata=xex_info(game/'default.xex')
        display=label if game==folder and label else game.name
        relative=game_relative(display+'.iso')
        # Installer/add-on discs must not collide with a base game of the same name.
        if re.search(r'add.on|install(?:ation)? disc',display,re.I):relative=Path(clean_title(display.split(' (',1)[0]))/'Install disc'
        if metadata.get('discs',0)>1 and len(relative.parts)==1:relative=relative/('Disc '+str(metadata['disc']))
        consumed.update(p for p,_ in files)
        units.append(dict(name=display,kind='Install disc' if relative.name=='Install disc' else 'Extracted game',metadata=metadata,files=files,destination=Path('Games')/relative,package=None))
    isos=[p for p in all_files if p.suffix.lower()=='.iso' and p not in consumed]
    consumed.update(isos)
    unknown=[str(p.relative_to(folder)) for p in all_files if p not in consumed and p.suffix.lower() not in AUX]
    return units,isos,unknown

def verify_files(files,dest,callback,checkpoint,copy_files=False):
    total=sum(p.stat().st_size for p,_ in files);done=0;hashes={}
    if copy_files:
        for p,rel in files:
            checkpoint();target=safe_path(dest/rel,dest)
            if p.stat().st_size>0xFFFFFFFF:raise ValueError('A file exceeds the FAT32 4 GB limit: '+p.name)
            target.parent.mkdir(parents=True,exist_ok=True)
            with locked_reader(p) as source,target.open('xb') as out:
                while chunk:=source.read(CHUNK):
                    checkpoint();out.write(chunk);done+=len(chunk);callback('Copying',done,total,p.name)
                out.flush();os.fsync(out.fileno())
    done=0
    for p,rel in files:
        checkpoint();target=safe_path(dest/rel,dest);sha=hashlib.sha256();count=0
        with locked_reader(p) as source,target.open('rb') as actual:
            while chunk:=source.read(CHUNK):
                checkpoint()
                if actual.read(len(chunk))!=chunk:raise ValueError('Verification mismatch: '+str(rel))
                sha.update(chunk);count+=len(chunk);done+=len(chunk);callback('Verifying',done,total,p.name)
            if actual.read(1):raise ValueError('Verification size mismatch: '+str(rel))
        hashes[rel.as_posix()]={'size':count,'sha256':sha.hexdigest()}
    return hashes


def tree_bytes(path):
    if not Path(path).exists():return 0
    return sum(actual_files(path).values()) if Path(path).is_dir() else Path(path).stat().st_size
