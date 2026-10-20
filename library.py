"""Read-only USB library discovery and explicitly selected component removal."""
import hashlib, json, os, time, stat
from pathlib import Path
from core import safe_path, safe_remove_tree, NamedLock, actual_files, GIB, Cancelled
from content import package_info, xex_info, CONTENT_TYPES, tree_bytes, atomic_json, input_signature

def inventory(path, checkpoint):
    """One metadata walk for size and removal checks; never read game contents."""
    path=Path(path);checkpoint();safe_path(path,path)
    if path.is_file():
        s=path.stat();return s.st_size,[s.st_size,s.st_mtime_ns,s.st_ino]
    rows=[];total=0;pending=[path]
    while pending:
        checkpoint();parent=pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                checkpoint();s=entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(s.st_mode) or getattr(s,'st_file_attributes',0)&0x400:raise ValueError('Linked game folders are unsupported: '+entry.path)
                if stat.S_ISDIR(s.st_mode):pending.append(Path(entry.path))
                elif stat.S_ISREG(s.st_mode):
                    rel=Path(entry.path).relative_to(path).as_posix().lower()
                    rows.append((rel,s.st_size,s.st_mtime_ns));total+=s.st_size
    return total,[total,hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(),'folder']


def scan_library(root,games='Games',jobs=(),cancel=None,progress=None):
    root=Path(root);groups={}
    def checkpoint():
        if cancel is not None and cancel.is_set():raise Cancelled('Game list scan stopped.')
    checkpoint()
    def add(name,kind,metadata,paths):
        title=metadata.get('title_id');key=title or name.casefold()
        if not title and paths and Path(paths[0]).parent.name==games and Path(paths[0]).name.lower().startswith('disc '):key=Path(paths[0]).parent.name.casefold()
        group=groups.setdefault(key,dict(id=key,name=name,title_id=title or '',components=[],size=0,formats=set(),added=0))
        if kind in {'Extracted game','Games on Demand','Arcade game'}:group['name']=name
        component=dict(name=name,kind=kind,paths=[str(p) for p in paths],metadata=metadata,size=0)
        component['signatures']={}
        if progress:progress('Reading '+name+'…')
        for p in paths:
            checkpoint();safe_path(p,root);amount,sig=inventory(p,checkpoint)
            component['size']+=amount;component['signatures'][str(p)]=sig
            group['added']=max(group['added'],p.stat().st_mtime)
        component['token']=hashlib.sha256('\n'.join(component['paths']).encode()).hexdigest()[:16]
        group['components'].append(component);group['size']+=component['size'];group['formats'].add(kind)
    game_root=safe_path(root/games,root)
    if game_root.exists():
        for parent,dirs,files in os.walk(game_root,followlinks=False):
            checkpoint()
            for name in dirs:safe_path(Path(parent)/name,game_root)
            exe=next((f for f in files if f.lower()=='default.xex'),None)
            if exe:
                safe_path(Path(parent)/exe,game_root)
                p=Path(parent);meta=xex_info(p/exe);name=p.parent.name if p.name.lower().startswith(('disc ','install disc')) else p.name
                kind='Install disc' if p.name.lower()=='install disc' else 'Extracted game'
                # Parent game name groups multidisc folders even when an executable lacks metadata.
                add(name,kind,meta,[p]);dirs[:]=[]
    content=safe_path(root/'Content'/'0000000000000000',root)
    if content.exists():
        for title in content.iterdir():
            checkpoint()
            if not title.is_dir():continue
            safe_path(title,content)
            for typ in title.iterdir():
                if not typ.is_dir():continue
                safe_path(typ,content)
                for p in typ.iterdir():
                    checkpoint();safe_path(p,content)
                    if not p.is_file():continue
                    try:info=package_info(p)
                    except (OSError,ValueError):continue
                    if not info:continue
                    paths=[p]
                    if info['kind']==0x7000 and p.with_name(p.name+'.data').is_dir():paths.append(p.with_name(p.name+'.data'))
                    name=info['name'].split(' - Disc ',1)[0]
                    add(name,CONTENT_TYPES[info['kind']],info,paths)
    for g in groups.values():
        g['formats']=', '.join(sorted(g['formats']));g['discs']=sum(c['kind'] in {'Extracted game','Games on Demand','Arcade game','Install disc'} for c in g['components'])
    return sorted(groups.values(),key=lambda g:g['name'].casefold())

def remove_components(engine,components):
    """Caller presents exact component paths and confirms before invoking."""
    if engine.active_id:raise ValueError('Finish the current import before removing games')
    root=engine.drive.locate();allowed=[root/engine.settings['games'],root/'Content'/'0000000000000000']
    targets=[]
    for c in components:
        for raw in c['paths']:
            p=safe_path(raw,root)
            if not any(p!=a and p.is_relative_to(a) for a in allowed):raise ValueError('Removal path is outside game/content folders')
            if not p.exists():raise ValueError('Library changed; refresh the game list first')
            if str(p) in c.get('signatures',{}) and input_signature(p)!=c['signatures'][str(p)]:raise ValueError('Game changed since library scan; refresh before removal')
            targets.append(p)
    with NamedLock():
        engine.drive.check(root)
        event=dict(at=time.time(),action='Remove from USB',paths=[str(p) for p in targets],state='Started')
        engine.history_events.append(event);engine.save(required=True)
        for p in targets:
            engine.management_checkpoint();engine.drive.check(root)
            if p.is_dir():safe_remove_tree(p,root)
            else:p.unlink()
        event['state']='Complete';engine.save(required=True)
        engine.log('Removed selected USB components: '+', '.join(p.name for p in targets))
