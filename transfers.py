"""Journalled final-file transfers; only matching files may be reused."""
import hashlib
from pathlib import Path, PurePosixPath
from core import GIB, safe_path, Cancelled
from destinations import DestinationConflict, relative, walk


def matches(actual, expected):
    return actual['size']==expected['size'] and actual['sha256']==expected['sha256']


def locate_duplicates(session,units,games,cached,checkpoint):
    """A cache suggests candidates; only a complete byte match proves a duplicate."""
    candidates=[c for g in cached for c in g['components'] if c['kind'] in {'Extracted game','Game folder'}]
    for unit in units:
        if unit['kind']!='Extracted game':continue
        for candidate in candidates:
            if candidate.get('size') is None:
                fields=('title_id','media_id','version','disc');info=unit['metadata']
                if not info.get('media_id') or info['media_id']=='00000000' or not all(k in info and candidate['metadata'].get(k)==info[k] for k in fields):continue
            elif candidate['size']!=sum(h['size'] for h in unit['hashes'].values()):continue
            for root in candidate['paths']:
                if not root.startswith(relative(games)+'/'):continue
                checkpoint();st=session.stat(root)
                if not st or not st['directory']:continue
                actual={p[len(root)+1:]:e for p,e in walk(session,root,checkpoint)}
                if {p:e['size'] for p,e in actual.items()}!={p:e['size'] for p,e in unit['hashes'].items()}:continue
                if all(matches(session.digest(root+'/'+p,checkpoint),h) for p,h in unit['hashes'].items()):
                    unit['destination']='Games'+root[len(relative(games)):]
                    break
            else:continue
            break


def upload(session, units, local, games, job, save, progress, checkpoint):
    planned={};components=[]
    for u in units:
        base=u['destination']
        if base.startswith('Games/'):base=relative(games)+base[5:]
        targets=[base+'/'+u['package']] if u['package'] else [base]
        if u['package'] and any('/' in r for r in u['hashes']):targets.append(base+'/'+u['package']+'.data')
        components.append(dict(name=u['name'],kind=u['kind'],metadata=u['metadata'],paths=targets))
        for rel,expected in u['hashes'].items():
            path=relative(base+'/'+rel)
            if path.casefold() in planned:raise DestinationConflict('Two input items use the same destination')
            planned[path.casefold()]=(path,safe_path(Path(local)/u['folder']/rel,local),expected)
    # Reject extra content at a game destination, including incomplete old copies.
    for c in components:
        for root in c['paths']:
            st=session.stat(root)
            if st and st['directory']:
                for path,_ in walk(session,root,checkpoint):
                    if path.casefold() not in planned and path not in job.get('partials',[]):
                        raise DestinationConflict('Different files already exist at '+root)
    total=sum(v[2]['size'] for v in planned.values());done=0;published=False
    descriptors={p for c in components if c['kind']=='Games on Demand' for p in c['paths'] if not p.endswith('.data')}
    entries=sorted(planned.values(),key=lambda x:(PurePosixPath(x[0]).name.lower()=='default.xex' or x[0] in descriptors,x[0].lower()))
    journal=job.setdefault('transferred',{});partials=job.setdefault('partials',[])
    for path,source,expected in entries:
        checkpoint();st=session.stat(path)
        read_existing=0
        def reading(n):
            nonlocal read_existing
            read_existing+=n;progress('Verifying',min(done+read_existing,total),total,path)
        if st:
            if st['directory'] or st['size']!=expected['size'] or not matches(session.digest(path,checkpoint,reading),expected):
                raise DestinationConflict('Different file already exists at '+path+'; nothing overwritten')
        else:
            free=session.free()
            if free is not None and free<expected['size']+GIB:raise OSError('Destination storage is full; free space then retry')
            p=PurePosixPath(path)
            temp=str(p.parent/('.x360-'+job['id'][:12]+'-'+hashlib.sha256(path.encode()).hexdigest()[:12]+'.part'))
            if temp not in partials:
                if session.stat(temp):raise DestinationConflict('Transfer temporary filename is already occupied')
                partials.append(temp);save()
            amount=0
            def written(n):
                nonlocal amount
                amount+=n;progress('Copying',done+amount,total,path)
            session.put(temp,source,checkpoint,written)
            read=0
            def checked(n):
                nonlocal read
                read+=n;progress('Verifying',done+read,total,path)
            if not matches(session.digest(temp,checkpoint,checked),expected):raise ValueError('Transferred data failed verification: '+path)
            checkpoint()
            # Persist ownership before rename so a crash just after publication
            # remains recoverable without mistaking pre-existing duplicates for ours.
            job.setdefault('owned_files',{})[path]=expected;save()
            session.rename(temp,path);published=True
            partials.remove(temp)
        journal[path]=expected;done+=expected['size'];save()
        progress('Verifying',done,total,path)
    return components,dict((v[0],v[2]) for v in entries),published
