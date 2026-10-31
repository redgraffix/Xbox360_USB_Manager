"""Persistent destination inventory and bounded local thumbnail cache."""
import hashlib
import io
import json
import struct
import time
import copy
from pathlib import Path, PurePosixPath
from PIL import Image, ImageOps
from content import atomic_json, package_header, CONTENT_TYPES
from core import Cancelled, safe_path
from destinations import connect, identity, relative, walk, location_label


def cache_file(engine,destination=None):
    folder=engine.state_dir/'libraries';folder.mkdir(exist_ok=True)
    return folder/(identity(destination or engine.settings['destination'])+'.json')


def load(engine):
    try:
        data=json.loads(cache_file(engine).read_text('utf-8'))
        if data.get('destination_id')==identity(engine.settings['destination']):return data
    except (OSError,ValueError):pass
    return dict(items=[],refreshed=0)


def save(engine, items, refreshed=None,destination=None):
    destination=destination or engine.settings['destination']
    for item in items:item['location']=location_label(destination)
    atomic_json(cache_file(engine,destination),dict(version=1,destination_id=identity(destination),
                refreshed=time.time() if refreshed is None else refreshed,items=items),True)
    engine.cache_revision+=1


def thumbnail(engine, raw):
    if not raw or len(raw)>4*1024*1024:return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.width>4096 or im.height>4096:return None
            im.load();small=ImageOps.contain(im.convert('RGBA'),(40,40))
            folder=engine.state_dir/'thumbnails';folder.mkdir(exist_ok=True)
            name=hashlib.sha256(raw).hexdigest()+'.png';path=folder/name
            if not path.exists():small.save(path,format='PNG')
            return name
    except (OSError,ValueError,Image.DecompressionBombError):return None


def package_art(header):
    if len(header)<0x171A:return None
    # Both metadata versions keep these starts; v2 reduces their maximum sizes.
    for offset,size_offset in ((0x571A,0x1716),(0x171A,0x1712)):
        n=struct.unpack_from('>I',header,size_offset)[0]
        if 0<n<=0x3D00 and offset+n<=len(header):return header[offset:offset+n]
    return None


def execution_info(data):
    try:
        if data[:4]!=b'XEX2':return {}
        count=struct.unpack_from('>I',data,20)[0]
        if count>2048:return {}
        for i in range(count):
            key,offset=struct.unpack_from('>II',data,24+i*8)
            if key==0x40006:
                media,version,base,title=struct.unpack_from('>IIII',data,offset)
                return dict(title_id=f'{title:08X}',media_id=f'{media:08X}',version=f'{version:08X}',disc=data[offset+18],discs=data[offset+19])
    except (struct.error,IndexError):pass
    return {}


def group_components(components):
    groups={}
    for c in components:
        title=c['metadata'].get('title_id');key=title or c['name'].casefold()
        g=groups.setdefault(key,dict(id=key,name=c['name'],title_id=title or '',size=0,components=[],thumbnail=None,added=0))
        if c['kind'] in {'Extracted game','Games on Demand','Arcade game'}:g['name']=c['name']
        if c.get('thumbnail') and not g['thumbnail']:g['thumbnail']=c['thumbnail']
        g['components'].append(c);g['size']+=c['size'];g['added']=max(g['added'],c.get('added',0))
    for g in groups.values():
        kinds=[c['kind'] for c in g['components']];labels=[]
        game=any(x in kinds for x in ('Extracted game','Games on Demand','Arcade game'))
        if game:labels.append('Arcade' if 'Arcade game' in kinds else 'Game')
        dlc=kinds.count('DLC / install content');updates=kinds.count('Title update')
        if dlc:labels.append(f'{dlc} DLC / install')
        if updates:labels.append(f'{updates} update'+('s' if updates!=1 else ''))
        if 'Install disc' in kinds:labels.append('Install disc')
        g['formats']=' + '.join(labels) or ', '.join(sorted(set(kinds)))
        g['discs']=sum(x in {'Extracted game','Games on Demand','Arcade game'} for x in kinds)
    return sorted(groups.values(),key=lambda g:g['name'].casefold())


def make_component(name,kind,metadata,paths,files,art=None):
    if kind in {'Extracted game','Install disc'}:
        folder=PurePosixPath(paths[0]);name=folder.parent.name if folder.name.lower().startswith(('disc ','install disc')) else folder.name
    return dict(name=name,kind=kind,metadata=metadata,paths=paths,files=files,
                size=sum(x['size'] for x in files.values()),thumbnail=art,added=time.time())


def scan(engine,cancel,progress=lambda text:None):
    def checkpoint():
        if cancel.is_set() or engine.closing.is_set():raise Cancelled()
    components=[];games=relative(engine.settings['games']);destination=copy.deepcopy(engine.settings['destination'])
    with connect(destination,engine._supplied_drive) as session:
        pending=[games] if session.stat(games) else []
        while pending:
            checkpoint();folder=pending.pop();entries=session.list(folder)
            if len(PurePosixPath(folder).parts)>40:raise ValueError('Game folder nesting limit')
            exe=next((x for x in entries if x['name'].casefold()=='default.xex' and not x['directory']),None)
            if exe:
                name=PurePosixPath(folder).name
                if name.lower().startswith(('disc ','install disc')):name=PurePosixPath(folder).parent.name
                progress('Reading '+name+'…');metadata=execution_info(session.prefix(folder+'/'+exe['name'],65536))
                files={p:e for p,e in walk(session,folder,checkpoint) if not PurePosixPath(p).name.startswith('.x360-')}
                art=None
                for entry in entries:
                    if not entry['directory'] and entry['name'].casefold() in {'folder.jpg','folder.png','cover.jpg','cover.png','icon.png'} and entry['size']<=4*1024*1024:
                        art=thumbnail(engine,session.prefix(folder+'/'+entry['name'],entry['size']));break
                kind='Install disc' if PurePosixPath(folder).name.lower().startswith('install disc') else 'Extracted game'
                components.append(make_component(name,kind,metadata,[folder],files,art))
            else:pending.extend(folder+'/'+e['name'] for e in entries if e['directory'] and not e['name'].startswith('.'))
        content='Content/0000000000000000'
        if session.stat(content):
            for title in session.list(content):
                checkpoint()
                if not title['directory']:continue
                title_path=content+'/'+title['name']
                for kind in session.list(title_path):
                    if not kind['directory']:continue
                    base=title_path+'/'+kind['name']
                    for entry in session.list(base):
                        checkpoint()
                        if entry['directory'] or entry['name'].startswith('.x360-'):continue
                        path=base+'/'+entry['name'];header=session.prefix(path,0xA000);info=package_header(header)
                        if not info:continue
                        progress('Reading '+info['name']+'…');paths=[path];files={path:entry}
                        if info['kind']==0x7000 and session.stat(path+'.data'):
                            paths.append(path+'.data');files.update(walk(session,path+'.data',checkpoint))
                        art=thumbnail(engine,package_art(header))
                        components.append(make_component(info['name'],CONTENT_TYPES[info['kind']],info,paths,files,art))
    items=group_components(components);checkpoint();save(engine,items,destination=destination);return items


def update_import(engine,job,units,local):
    old=load(engine);components=[c for g in old['items'] for c in g['components']]
    incoming=[]
    for c,u in zip(job['components'],units):
        root=Path(local)/u['folder'];art=None
        if u['package']:
            with (root/u['package']).open('rb') as stream:art=thumbnail(engine,package_art(stream.read(0xA000)))
        if not art:
            for name in ('folder.jpg','folder.png','cover.jpg','cover.png','icon.png'):
                p=root/name
                if p.is_file() and p.stat().st_size<=4*1024*1024:art=thumbnail(engine,p.read_bytes());break
        files={p:dict(size=h['size'],modified='') for p,h in job['transferred'].items() if any(p==r or p.startswith(r+'/') for r in c['paths'])}
        incoming.append(make_component(c['name'],c['kind'],c['metadata'],c['paths'],files,art))
    paths={p for c in incoming for p in c['paths']};components=[c for c in components if not paths.intersection(c['paths'])]
    save(engine,group_components(components+incoming),old.get('refreshed',0))


def remove(engine,components):
    engine.management_checkpoint();games=relative(engine.settings['games']);roots=(games+'/', 'Content/0000000000000000/')
    with connect(engine.settings['destination'],engine._supplied_drive) as session:
        files={};directories=set()
        # Re-read before deleting; cached inventories never authorize new files.
        for c in components:
            for path in c['paths']:
                if not path.startswith(roots):raise ValueError('Removal path is outside game/content folders')
                st=session.stat(path)
                if not st:raise ValueError('Game list changed; refresh before removing')
                actual=dict(walk(session,path,engine.management_checkpoint,directories)) if st['directory'] else {path:st}
                expected={p:e for p,e in c['files'].items() if p==path or p.startswith(path+'/')}
                if {p:e['size'] for p,e in actual.items()}!={p:e['size'] for p,e in expected.items()}:
                    raise ValueError('Files changed since the cached list; refresh before removing')
                for p,e in actual.items():
                    if expected[p].get('modified') and expected[p]['modified']!=e.get('modified'):
                        raise ValueError('Files changed since the cached list; refresh before removing')
                    files[p]=e
                    if st['directory']:
                        parent=PurePosixPath(p).parent
                        while str(parent)==path or str(parent).startswith(path+'/'):
                            directories.add(str(parent));parent=parent.parent
                if st['directory']:directories.add(path)
        # Delete only the preflight inventory; never recursively absorb new files.
        for p,e in files.items():
            engine.management_checkpoint();now=session.stat(p)
            if not now or now['directory'] or now['size']!=e['size'] or now.get('modified')!=e.get('modified'):
                raise ValueError('Destination changed during removal; remaining files retained')
            session.remove(p)
        for p in sorted(directories,key=lambda p:len(PurePosixPath(p).parts),reverse=True):
            engine.management_checkpoint();session.remove(p,True)
    old=load(engine);removed={p for c in components for p in c['paths']}
    remain=[c for g in old['items'] for c in g['components'] if not removed.intersection(c['paths'])]
    save(engine,group_components(remain),old.get('refreshed',0))
