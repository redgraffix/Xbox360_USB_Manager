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
        g['components'].append(c);g['size']=g['size']+c['size'] if g['size'] is not None and c.get('size') is not None else None;g['added']=max(g['added'],c.get('added',0))
    for g in groups.values():
        kinds=[c['kind'] for c in g['components']];labels=[]
        game=any(x in kinds for x in ('Extracted game','Game folder','Games on Demand','Arcade game'))
        if game:labels.append('Arcade' if 'Arcade game' in kinds else 'Game')
        dlc=kinds.count('DLC / install content');updates=kinds.count('Title update')
        if dlc:labels.append(f'{dlc} DLC / install')
        if updates:labels.append(f'{updates} update'+('s' if updates!=1 else ''))
        if 'Install disc' in kinds:labels.append('Install disc')
        g['formats']=' + '.join(labels) or ', '.join(sorted(set(kinds)))
        g['discs']=sum(x in {'Extracted game','Game folder','Games on Demand','Arcade game'} for x in kinds)
    return sorted(groups.values(),key=lambda g:g['name'].casefold())


def make_component(name,kind,metadata,paths,files,art=None):
    if kind in {'Extracted game','Install disc'}:
        folder=PurePosixPath(paths[0]);name=folder.parent.name if folder.name.lower().startswith(('disc ','install disc')) else folder.name
    return dict(name=name,kind=kind,metadata=metadata,paths=paths,files=files,
                size=sum(x['size'] for x in files.values()),thumbnail=art,added=time.time())


def history_hints(engine,games):
    """Reuse local display metadata only; history never proves remote contents."""
    folders={};packages={};titles={}
    with engine.guard:records=[copy.deepcopy(c) for job in engine.jobs if job.get('state')=='Complete' for c in job.get('components',[])]
    for c in records:
        info=c.get('metadata',{})
        if info.get('title_id'):titles[info['title_id']]=info.get('name') or c['name']
        for path in c['paths']:
            text='/'+path.replace('\\','/').lstrip('/')
            marker='/'+games+'/'
            if marker.casefold() in text.casefold():
                tail=text[text.casefold().index(marker.casefold())+len(marker):]
                folders[tail.split('/')[0].casefold()]=c
            marker='/Content/0000000000000000/'
            if marker.casefold() in text.casefold():
                tail=text[text.casefold().index(marker.casefold())+1:]
                packages[tail.casefold()]=c
    return folders,packages,titles


def scan(engine,cancel,progress=lambda text:None,on_items=lambda items:None):
    """Quick inventory: directory listings and cached metadata, never payload reads."""
    def checkpoint():
        if cancel.is_set() or engine.closing.is_set():raise Cancelled()
    destination=copy.deepcopy(engine.settings['destination']);games=relative(engine.settings['games'])
    old=[c for g in load(engine)['items'] for c in g['components']]
    folders,packages,titles=history_hints(engine,games)
    known={p.casefold():c for c in old for p in c['paths']}
    components=[]
    def publish(extra=()):
        checkpoint();items=group_components(components+list(extra))
        for item in items:item['location']=location_label(destination)
        on_items(items)
        checkpoint()
    with connect(destination,engine._supplied_drive) as session:
        root_entries=session.list()
        has_games=any(e['directory'] and e['name'].casefold()==games.casefold() for e in root_entries)
        if '/' in games:has_games=bool(session.stat(games))
        if has_games:
            progress('Listing game folders...')
            for entry in session.list(games):
                checkpoint()
                if not entry['directory'] or entry['name'].startswith('.'):continue
                path=games+'/'+entry['name']
                cached=[copy.deepcopy(c) for c in old if c['kind'] in {'Extracted game','Game folder','Install disc'}
                        and any(p.casefold()==path.casefold() or p.casefold().startswith(path.casefold()+'/') for p in c['paths'])]
                if cached:components.extend(cached);continue
                hint=folders.get(entry['name'].casefold(),{})
                components.append(dict(name=entry['name'],kind='Game folder',metadata=hint.get('metadata',{}),
                    paths=[path],files={},size=None,thumbnail=None,added=0,inventory_complete=False))
        content='Content/0000000000000000'
        cached_content=[c for c in old if any(p.startswith(content+'/') for p in c['paths'])]
        publish(cached_content)
        progress(f'{len(components)} game folders found; checking Content folders...')
        if any(e['directory'] and e['name'].casefold()=='content' for e in root_entries) and session.stat(content):
            title_dirs=[e for e in session.list(content) if e['directory'] and len(e['name'])==8 and all(x in '0123456789abcdefABCDEF' for x in e['name'])]
            for index,title in enumerate(title_dirs,1):
                checkpoint();title_id=title['name'].upper();title_path=content+'/'+title['name']
                progress(f'Checking content folders {index}/{len(title_dirs)}...')
                for kind in session.list(title_path):
                    if not kind['directory']:continue
                    try:code=int(kind['name'],16)
                    except ValueError:continue
                    if code not in CONTENT_TYPES or title_id=='FFFE07D1':continue
                    base=title_path+'/'+kind['name'];entries=session.list(base)
                    directories={e['name'].casefold() for e in entries if e['directory']}
                    for entry in entries:
                        checkpoint()
                        if entry['directory'] or entry['name'].startswith('.x360-'):continue
                        path=base+'/'+entry['name'];cached=known.get(path.casefold());hint=cached or packages.get(path.casefold(),{})
                        info=dict(hint.get('metadata',{}),title_id=title_id,kind=code)
                        name=hint.get('name') or titles.get(title_id) or ('Title '+title_id)
                        paths=[path];files={path:entry};complete=True;amount=entry['size']
                        if code==0x7000:
                            if (entry['name']+'.data').casefold() in directories:paths.append(path+'.data')
                            complete=False;amount=cached.get('size') if cached else None
                            if cached and set(cached['paths'])==set(paths):files=cached['files'];complete=cached.get('inventory_complete',True)
                        component=make_component(name,CONTENT_TYPES[code],info,paths,files,hint.get('thumbnail'))
                        component.update(size=amount,inventory_complete=complete)
                        components.append(component)
                if index%5==0:publish()
    items=group_components(components);checkpoint();save(engine,items,destination=destination);return items


def prepare_removal(engine,components):
    """Deep inventory only for the user's selected removal, before confirmation."""
    engine.management_checkpoint();games=relative(engine.settings['games']);roots=(games+'/', 'Content/0000000000000000/')
    result=[]
    with connect(engine.settings['destination'],engine._supplied_drive) as session:
        for c in components:
            files={}
            for path in c['paths']:
                engine.management_checkpoint()
                if not path.startswith(roots):raise ValueError('Removal path is outside game/content folders')
                st=session.stat(path)
                if not st:raise ValueError('Selected content is no longer present; refresh the list')
                if st['directory']:files.update(walk(session,path,engine.management_checkpoint))
                else:files[path]=st
            current=copy.deepcopy(c);current.update(files=files,size=sum(e['size'] for e in files.values()),inventory_complete=True)
            result.append(current)
    return result


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
    if any(not c.get('inventory_complete',True) for c in components):raise ValueError('Inspect selected content before confirming removal')
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
