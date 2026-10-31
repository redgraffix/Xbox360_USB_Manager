"""Local preparation followed by a bound, verified destination transfer."""
import copy
import json
import time
from pathlib import Path
from core import Cancelled, Duplicate, safe_path, safe_remove_tree
from content import input_signature, atomic_json
from destinations import connect, identity, normalize, DestinationConflict
from preparation import prepare, NeedsReview
from transfers import upload, matches, locate_duplicates


def process(engine, job):
    job.setdefault('source_root',str(Path(job['source']).parent))
    source=safe_path(job['source'],job.get('source_root',Path(job['source']).parent))
    initial=input_signature(source)
    if initial!=job['signature']:raise ValueError('Original changed; retry after copying finishes')
    dest=normalize(engine.settings['destination']);dest_id=identity(dest)
    if job.get('destination_id') and job['destination_id']!=dest_id:
        raise DestinationConflict('This interrupted job belongs to a different destination. Select its original destination or import it again.')
    if job.get('games_folder',engine.settings['games'])!=engine.settings['games']:
        raise DestinationConflict('Restore this interrupted job\'s original games subfolder before retrying.')
    local=safe_path(engine.work/job['id'],engine.work)
    job.update(destination=copy.deepcopy(dest),destination_id=dest_id,games_folder=engine.settings['games'],temp_paths=[str(local)])
    if 'cleanup_policy' not in job:
        job['cleanup_policy']=engine.settings['cleanup'] if engine.batch_policy=='default' else engine.batch_policy
        if engine.batch_policy=='default' and engine.settings['cleanup']!='ask':
            job['cleanup_policy']='delete' if engine.delete_sources else 'keep'
    engine.save(required=True);last=[0.0];started=time.monotonic()
    def checkpoint():
        if engine.cancel.is_set() or engine.closing.is_set():raise Cancelled()
    def progress(phase,done,total,detail):
        now=time.monotonic()
        if now-last[0]<.25 and done!=total and job['state']==phase:return
        last[0]=now;engine.update(job,state=phase,percent=min(100,100*done/total) if total else 100,
                                done=done,total=total,detail=detail,elapsed=now-started)
    success=False
    try:
        engine.update(job,state='Checking',detail='Preparing game files in local working storage',percent=0)
        units=prepare(source,local,engine.tool,progress,checkpoint)
        with connect(dest,engine._supplied_drive) as session:
            import library_cache
            locate_duplicates(session,units,engine.settings['games'],library_cache.load(engine)['items'],checkpoint)
            components,hashes,published=upload(session,units,local,engine.settings['games'],job,
                                             lambda:engine.save(required=True),progress,checkpoint)
            session.check();checkpoint()
            if input_signature(source)!=initial:raise ValueError('Original changed during transfer; retained')
            if not published and not job.get('owned_files'):raise Duplicate('Identical destination files verified. Original retained.')
            record=dict(version=3,destination=dest,destination_id=dest_id,files=hashes,components=components)
            folder=engine.state_dir/'manifests';folder.mkdir(exist_ok=True)
            filename=folder/(job['id']+'.json');atomic_json(filename,record,True)
            job.update(outputs=sorted({p for c in components for p in c['paths']}),components=components,
                       verification_manifest=str(filename),verified_at=time.time(),source_retained=True,
                       format=', '.join(sorted({u['kind'] for u in units})),cleanup_pending=job['cleanup_policy']=='ask')
            if any(u['kind']=='Title update' for u in units):
                job['setup_note']='Scan and enable title updates in Aurora; select the update matching this game.'
            engine.save(required=True)
            engine.update(job,state='Finishing',detail='Destination verified; recording completion',percent=100)
            # Keep the original through all destination checks and durable writes.
            session.check();checkpoint()
        if job['cleanup_policy']=='delete':engine._delete_source(job,initial)
        engine.update(job,state='Complete',percent=100,detail='Files verified at the selected destination. '+
                      ('Original retained.' if job.get('source_retained',True) else 'Original removed.'),elapsed=time.monotonic()-started)
        success=True
        try:engine.refresh_cache_from_import(job,units,local)
        except Exception as exc:engine.log('Game-list cache update deferred: '+str(exc))
    except NeedsReview as exc:
        engine.update(job,state='Needs review',detail=str(exc),percent=0)
    except (ValueError,Duplicate):
        try:safe_remove_tree(local,engine.work)
        except OSError:pass
        raise
    finally:
        if success or job.get('state') in {'Duplicate','Error','Needs review'}:
            try:safe_remove_tree(local,engine.work)
            except OSError:engine.log('Local temporary cleanup will retry on next launch')


def verify_job(engine, job):
    engine.management_checkpoint()
    record=json.loads(Path(job['verification_manifest']).read_text('utf-8'))
    if not isinstance(record,dict) or record.get('version')!=3:
        return engine.verify_legacy_job(job)
    if identity(engine.settings['destination'])!=record['destination_id']:
        raise ValueError('Select the destination originally used for this import')
    with connect(record['destination'],engine._supplied_drive) as session:
        for path,expected in record['files'].items():
            engine.management_checkpoint()
            if not matches(session.digest(path,engine.management_checkpoint),expected):
                raise ValueError('Destination verification mismatch: '+path)
    return True
