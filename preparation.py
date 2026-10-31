"""Detect, unpack and prepare game content entirely in local working storage."""
import json
import shutil
from pathlib import Path
from core import (GIB, safe_path, safe_remove_tree, locked_reader, manifest,
                  extract_and_verify, walk_files)
from content import (input_signature, archive_inventory, unpack_member, unpack_all,
                     inspect_tree, package_info, game_unit, verify_files, atomic_json,
                     CONTENT_TYPES, AUX, volumes)

class NeedsReview(ValueError): pass


def prepare(source, local, tool, progress, checkpoint):
    source=Path(source);local=Path(local);local.mkdir(parents=True,exist_ok=True)
    signature=input_signature(source);record=local/'prepared.json'
    if record.exists():
        saved=json.loads(record.read_text('utf-8'))
        if saved.get('signature')==signature:
            for unit in saved['units']:
                folder=safe_path(local/unit['folder'],local)
                files=[(safe_path(folder/r,folder),Path(r)) for r in unit['hashes']]
                hashes=verify_files(files,folder,progress,checkpoint)
                if any(hashes[r]!=unit['hashes'][r] for r in hashes):
                    raise ValueError('Prepared files changed; original retained. Clean temporary files before retrying.')
            progress('Checking',1,1,'Reusing verified local preparation')
            return saved['units']
        raise ValueError('Original changed since preparation; clean temporary files before retrying')
    units=[];unknown=[]
    def space(n):
        if shutil.disk_usage(local).free<n+GIB:raise OSError('Local working storage is full; free space or choose another local work folder')
    def convert(iso,label,index):
        target=local/'converted'/str(index)
        with locked_reader(iso) as image:
            entries=manifest(image)
            if not target.exists():space(sum(n for _,_,n in entries))
            hashes=extract_and_verify(image,entries,target,progress,checkpoint)
        unit=game_unit(target,label,[(target/p,p) for p,_,_ in entries])
        unit['_prepared']=target;unit['_hashes']={r:{k:v for k,v in h.items() if k!='mtime_ns'} for r,h in hashes.items()}
        units.append(unit)
    with locked_reader(source) if source.is_file() else __import__('contextlib').nullcontext():
        if source.suffix.lower()=='.iso':convert(source,source.stem,0)
        elif source.suffix.lower() in {'.zip','.7z'}:
            records=archive_inventory(tool,source)
            iso_records=[r for r in records if r['relative'].suffix.lower()=='.iso']
            other=[r for r in records if r not in iso_records and r['relative'].suffix.lower() not in AUX]
            if iso_records and not other:
                for i,r in enumerate(iso_records):
                    space(r['size']);checkpoint()
                    iso=unpack_member(tool,source,r['name'],r['size'],local/'archives',progress,checkpoint)
                    convert(iso,Path(r['name'].replace('\\','/')).stem,i)
                    safe_path(iso,local).unlink()
            else:
                space(sum(r['size'] for r in records));unpack=local/'archive'
                if unpack.exists():safe_remove_tree(unpack,local,checkpoint)
                unpack_all(tool,source,unpack,records,progress,checkpoint)
                got,isos,extra=inspect_tree(unpack,source.stem,checkpoint);units.extend(got);unknown.extend(extra)
                for i,iso in enumerate(isos):convert(iso,iso.stem,i)
        elif source.is_dir():
            got,isos,extra=inspect_tree(source,source.name,checkpoint);units.extend(got);unknown.extend(extra)
            for i,iso in enumerate(isos):convert(iso,iso.stem,i)
        else:
            info=package_info(source)
            if not info or info['kind']==0x7000:raise ValueError('Keep a GOD descriptor and its .data folder together inside one input folder')
            units=[dict(name=info['name'],kind=CONTENT_TYPES[info['kind']],metadata=info,
                        files=[(source,Path(source.name))],destination=Path('Content')/'0000000000000000'/info['title_id']/f"{info['kind']:08X}",package=source.name)]
        if not units:raise NeedsReview('No supported Xbox game/content found')
        if unknown:raise NeedsReview('Unrecognized content; original retained: '+', '.join(unknown[:6]))
        if any(u['metadata'].get('title_id')=='FFFE07D1' for u in units):raise NeedsReview('Standalone console update requires manual review')
        if any(u['kind']=='Title update' and (u.get('package') or '').upper().startswith('TU_') for u in units):raise NeedsReview('Legacy title update requires manual placement')
        result=[]
        for i,u in enumerate(units):
            checkpoint();folder=u.get('_prepared',local/'prepared'/str(i))
            if '_prepared' in u:hashes=u['_hashes']
            else:
                space(sum(p.stat().st_size for p,_ in u['files']))
                if folder.exists():safe_remove_tree(folder,local,checkpoint)
                hashes=verify_files(u['files'],folder,progress,checkpoint,True)
            result.append(dict(name=u['name'],kind=u['kind'],metadata=u['metadata'],package=u['package'],
                               folder=folder.relative_to(local).as_posix(),destination=u['destination'].as_posix(),hashes=hashes))
        if input_signature(source)!=signature:raise ValueError('Original changed during preparation')
        atomic_json(record,dict(signature=signature,units=result),True)
    return result
