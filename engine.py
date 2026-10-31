"""Persistent import queue, configurable storage, history and USB inventory."""
from __future__ import annotations
import contextlib, copy, json, logging, logging.handlers, os, queue, shutil, threading, time, uuid
from pathlib import Path
from core import *
from content import *
from destinations import connect, normalize, identity, describe, DestinationUnavailable, DestinationConflict
from runtime import default_work
SUPPORTED={'.iso','.zip','.7z'}
TERMINAL={'Complete','Duplicate','Error','Interrupted','Missing','Needs review'}
ACTIVE={'Unpacking','Checking','Converting','Copying','Verifying','Finishing'}

ContentConflict=DestinationConflict

class Engine:
    def __init__(self,base,tool,drive=None,settle_seconds=15):
        self.home=safe_path(base,base);self.home.mkdir(parents=True,exist_ok=True)
        self.state_dir=self.home/'_state';self.state_dir.mkdir(exist_ok=True)
        self.state_file=self.state_dir/'queue.json';self.settings_file=self.state_dir/'settings.json'
        self.guard=threading.RLock();self.events=queue.Queue(maxsize=400)
        self.closing=threading.Event();self.cancel=threading.Event();self.enabled=False
        self.jobs=[];self.active_id=None;self.blocked='Queue paused';self.stable={};self.threads=[]
        self.drive_root=None;self.drive_free=None;self.tool=Path(tool);self.settle_seconds=settle_seconds
        self.batch_policy='default';self.maintenance=False;self.management_cancel=threading.Event();self.history_events=[];self._supplied_drive=drive
        self.settings={'input':str(self.home),'temp':str(self.home/'_work' if drive else default_work()),'games':'Games','auto':True,
                       'cleanup':'keep','clean_temp':True,'drive':None}
        if self.settings_file.exists():self.settings.update(json.loads(self.settings_file.read_text('utf-8')))
        new_destination='destination' not in self.settings
        if 'destination' not in self.settings:
            self.settings['destination']=dict(kind='usb',volume=self.settings['drive']) if self.settings['drive'] else dict(kind='folder',path=str(self.home/'Ready'))
            if not drive and self.settings.get('temp')==str(self.home/'_work'):self.settings['temp']=str(default_work())
        self.settings['destination']=normalize(self.settings['destination'])
        if new_destination and self.settings['destination']['kind']=='folder' and not drive:Path(self.settings['destination']['path']).mkdir(parents=True,exist_ok=True)
        self.base=Path(self.settings['input']);self.work=Path(self.settings['temp']);self.work.mkdir(parents=True,exist_ok=True)
        self.auto=self.settings['auto'];self.delete_sources=self.settings['cleanup']=='delete'
        self.drive=drive or SelectedDrive(self.settings['drive'])
        self.logger=logging.getLogger('xbox360-'+uuid.uuid4().hex);self.logger.setLevel(logging.INFO);self.logger.propagate=False
        h=logging.handlers.RotatingFileHandler(self.state_dir/'activity.log',maxBytes=1024*1024,backupCount=2,encoding='utf-8')
        h.setFormatter(logging.Formatter('%(asctime)s %(message)s'));self.logger.addHandler(h)
        self._load()
        self.next_retry=0;self.retry_delay=5;self.connection_state='Not checked';self.cache_revision=0
    def log(self,message):
        try:self.logger.info(message)
        except OSError:pass
        try:self.events.put_nowait((time.strftime('%H:%M:%S'),message))
        except queue.Full:pass
    def _load(self):
        if not self.state_file.exists():return
        try:
            state=json.loads(self.state_file.read_text('utf-8'));self.history_events=state.get('events',[])
            if not self.settings_file.exists():
                self.auto=bool(state.get('auto',True));self.delete_sources=bool(state.get('delete_sources',False))
                self.settings.update(auto=self.auto,cleanup='delete' if self.delete_sources else 'keep')
            for j in state.get('jobs',[]):
                if not re.fullmatch('[0-9a-f]{32}',j.get('id','')):continue
                j.setdefault('source_root',str(Path(j['source']).parent))
                if j['state'] in ACTIVE:j.update(state='Interrupted',detail='App closed during this job. Original retained; select Retry.',percent=0)
                self.jobs.append(j)
        except Exception as e:self.log('Could not read history: '+str(e))
    def save(self,required=False):
        with self.guard:return atomic_json(self.state_file,{'version':2,'auto':self.auto,'delete_sources':self.delete_sources,'jobs':self.jobs,'events':self.history_events},required)
    def update(self,job,**values):
        with self.guard:
            old=job['state'];job.update(values)
            if job['state']!=old:
                job['phase_since']=time.time()
                if job['state'] in TERMINAL:job['finished_at']=time.time()
                self.save();self.log(job['name']+' — '+job['state']+': '+job.get('detail',''))
    def snapshot(self):
        with self.guard:return dict(jobs=copy.deepcopy(self.jobs),enabled=self.enabled,auto=self.auto,delete_sources=self.delete_sources,
            active=self.active_id,blocked=self.blocked,drive=str(self.drive_root) if self.drive_root else None,free=self.drive_free,
            settings=copy.deepcopy(self.settings),batch_policy=self.batch_policy,maintenance=self.maintenance,
            destination=describe(self.settings['destination']),connection=self.connection_state,cache_revision=self.cache_revision)
    def start_threads(self):
        for fn in (self._scan_loop,self._worker_loop):
            t=threading.Thread(target=fn,daemon=True);self.threads.append(t);t.start()
        self.log('Watching '+str(self.base)+'. Queue starts paused.')
    def enable(self):
        with self.guard:
            if self.maintenance:return
            self.enabled=True
            self.next_retry=0
            for j in self.jobs:
                if j['state']=='Ready':j['state']='Queued'
            self.save()
    def pause(self):
        self.enabled=False;self.blocked='Pausing after this file…' if self.active_id else 'Queue paused'
        self.log(self.blocked)
    def stop_current(self):
        self.enabled=False;self.cancel.set()
        for j in self.jobs:
            if j['state']=='Waiting for destination':self.update(j,state='Interrupted',detail='Stopped. Local preparation and originals retained.')
    def close(self):self.enabled=False;self.closing.set();self.cancel.set()
    def retry(self,ids):
        with self.guard:
            for j in self.jobs:
                if j['id'] in ids and j['state'] in (TERMINAL-{'Complete'})|{'Waiting for destination'} and Path(j['source']).exists():
                    j.update(state='Waiting',hidden=False,detail='Checking input before retry',percent=0)
                    j['signature']=input_signature(Path(j['source']));self.stable.pop(j['source'],None)
            self.save()
    def reimport(self,ids):
        with self.guard:
            for old in list(self.jobs):
                if old['id'] not in ids or old['state'] not in TERMINAL or not Path(old['source']).exists():continue
                if any(j['source']==old['source'] and j['state'] not in TERMINAL for j in self.jobs):continue
                sig=input_signature(Path(old['source']))
                self.jobs.append(dict(id=uuid.uuid4().hex,source=old['source'],source_root=old['source_root'],name=old['name'],size=sig[0],signature=sig,state='Waiting',detail='Reimport requested',percent=0,added=time.time(),outputs=[]))
                self.stable.pop(old['source'],None)
            self.save(required=True)
    def options(self,auto=None,delete=None):
        if auto is not None:self.auto=auto;self.settings['auto']=auto
        if delete is not None:self.delete_sources=delete;self.settings['cleanup']='delete' if delete else 'keep'
        atomic_json(self.settings_file,self.settings,True);self.save()
    def configure(self,settings):
        with self.guard:
            if self.active_id or self.maintenance:raise ValueError('Pause and finish the current import before changing settings')
            new={**self.settings,**settings};inp=Path(new['input']).absolute();temp=Path(new['temp']).absolute()
            if 'drive' in settings and 'destination' not in settings and settings['drive']:
                new['destination']=dict(kind='usb',volume=settings['drive'])
            new['destination']=normalize(new['destination'])
            if new['destination']['kind']=='ftp' and new['destination']['root']=='/':raise ValueError('Choose the Xbox HDD or USB folder, not the FTP root')
            safe_path(inp,inp);safe_path(temp,temp)
            if not inp.is_dir():raise ValueError('Choose an existing source folder')
            if inp==Path(inp.anchor):raise ValueError('Choose a source folder, not an entire drive')
            if temp==inp or inp.is_relative_to(temp):raise ValueError('Temporary storage must not contain the source folder')
            if temp==self.state_dir or temp.is_relative_to(self.state_dir) or self.state_dir.is_relative_to(temp):raise ValueError('Working storage and app state must not overlap')
            if inp==self.state_dir or inp.is_relative_to(self.state_dir):raise ValueError('State storage cannot be the input folder')
            rel=Path(new['games'])
            if rel.is_absolute() or not rel.parts or any(p in ('.','..') for p in rel.parts):raise ValueError('Games subfolder must be relative to the destination')
            for p in rel.parts:valid_name(p)
            if rel.parts[0].casefold() in {'content','xbox360-conversion-staging'}:raise ValueError('Games folder conflicts with content/staging folders')
            selected=SelectedDrive(new['destination'].get('volume'))
            new['drive']=new['destination'].get('volume')
            output=None
            if new['destination']['kind']=='folder':output=Path(new['destination']['path'])
            elif new['destination']['kind']=='usb':output=selected.locate()
            if output:
                for loc in (inp,temp,self.state_dir):
                    if output==loc or loc.is_relative_to(output) or output.is_relative_to(loc):
                        if loc==inp and output==inp/'Ready' and new['destination']['kind']=='folder':continue
                        raise ValueError('Input, work, app data and output locations must not overlap')
            if str(temp).startswith('\\\\'):raise ValueError('Use local working storage, not a network location')
            for v in volumes():
                if str(temp).casefold().startswith(v['root'].casefold()) and v['filesystem']=='FAT32':
                    raise ValueError('Working storage must support large files; use a local NTFS/exFAT folder')
            if output and new['destination']['kind']=='folder':output.mkdir(parents=True,exist_ok=True)
            if new['cleanup'] not in {'keep','delete','ask'}:raise ValueError('Invalid cleanup preference')
            temp.mkdir(parents=True,exist_ok=True);new.update(input=str(inp),temp=str(temp))
            atomic_json(self.settings_file,new,True)
            self.enabled=False;self.settings=new;self.base=inp;self.work=temp;self.auto=new['auto'];self.delete_sources=new['cleanup']=='delete'
            self.drive=self._supplied_drive or selected;self.stable.clear();self.cache_revision+=1;self.save()
            self.log('Settings saved. Queue paused; existing queued jobs retained.')
    def clear_finished(self):
        with self.guard:
            count=0
            for j in self.jobs:
                if j['state'] in TERMINAL and not j.get('hidden'):
                    j['hidden']=True;count+=1
            self.save(required=True)
            return count
    def keep_originals(self,ids):
        with self.guard:
            for job in self.jobs:
                if job['id'] in ids and job['state']=='Complete':job['cleanup_pending']=False
            self.save(required=True)
    def begin_maintenance(self):
        with self.guard:
            if self.active_id or self.maintenance:raise ValueError('Wait for the current import to finish first.')
            self.enabled=False;self.maintenance=True;self.management_cancel.clear()
    def end_maintenance(self):
        with self.guard:self.maintenance=False
    def management_checkpoint(self):
        if self.management_cancel.is_set() or self.closing.is_set():raise Cancelled('Stopped. Files not yet processed were left in place.')

    def _scan_loop(self):
        while not self.closing.is_set():
            try:self.scan()
            except Exception as e:self.log('Source check: '+str(e))
            self.closing.wait(3)
    def scan(self):
        try:
            d=self.settings['destination']
            root=self._supplied_drive.locate() if self._supplied_drive else (Path(d['path']) if d['kind']=='folder' else self.drive.locate() if d['kind']=='usb' else None)
            free=shutil.disk_usage(root).free if root else None
        except Exception:root=None;free=None
        with self.guard:
            self.drive_root,self.drive_free=root,free
            if not self.base.is_dir():self.blocked='Source folder is unavailable';return
            files=[]
            for p in self.base.iterdir():
                if p.name.startswith(('_','.')) or p==self.work or p==self.state_dir or (root and p==root):continue
                if p.is_dir():
                    if self.work.is_relative_to(p) or self.state_dir.is_relative_to(p):continue
                    files.append(p)
                elif p.suffix.lower() in SUPPORTED:files.append(p)
                elif p.suffix.lower() not in {'.crdownload','.part','.tmp','.lnk'}:
                    try:
                        with p.open('rb') as f:magic=f.read(4)
                        if magic in (b'LIVE',b'PIRS',b'CON '):files.append(p)
                    except OSError:pass
            now=time.monotonic();changed=False
            for p in sorted(files,key=lambda x:x.name.casefold()):
                safe_path(p,self.base);sig=input_signature(p);source=str(p)
                matches=[j for j in self.jobs if j['source']==source]
                j=next((j for j in matches if j['state'] not in TERMINAL),None)
                if j is None and any(x.get('signature')==sig for x in matches):continue
                if j is None:
                    j=dict(id=uuid.uuid4().hex,source=source,source_root=str(self.base),name=p.name,size=sig[0],signature=sig,
                           state='Waiting',detail='Waiting for copying to finish',percent=0,added=time.time(),outputs=[])
                    self.jobs.append(j);changed=True;self.log('Found '+p.name)
                if j['state'] in ACTIVE or j['state']=='Waiting for destination':continue
                previous=self.stable.get(source)
                if previous is None or previous[0]!=sig:
                    self.stable[source]=(sig,now)
                    if j['state'] not in TERMINAL:j.update(signature=sig,size=sig[0],state='Waiting')
                    changed=True;continue
                if j['state']!='Waiting' or now-previous[1]<self.settle_seconds:continue
                try:
                    if p.is_file():
                        with locked_reader(p):pass
                except OSError:continue
                j.update(state='Queued' if self.auto else 'Ready',detail='Ready to inspect and import');changed=True
            for j in self.jobs:
                if j['state'] in {'Waiting','Queued','Ready'} and not Path(j['source']).exists():j.update(state='Missing',detail='Source moved or removed');changed=True
            if changed:self.save()
    def _worker_loop(self):
        while not self.closing.is_set():
            try:self._work_once()
            except Exception as e:self.enabled=False;self.blocked='Paused after an error';self.log(str(e))
            self.closing.wait(.6)
    def _work_once(self):
        if self.maintenance:return
        if not self.enabled:self.blocked='Queue paused';return
        if batch_running():self.blocked='Waiting for the previous conversion batch to finish';return
        with self.guard:j=next((j for j in self.jobs if j['state']=='Waiting for destination'),None) or next((j for j in self.jobs if j['state']=='Queued'),None)
        if not j:self.blocked='Watching for new files' if self.auto else 'No queued games';return
        if time.monotonic()<self.next_retry:
            self.blocked=f"Waiting for destination · retry in {max(1,int(self.next_retry-time.monotonic()))}s";return
        try:lock=NamedLock()
        except BlockingIOError:return
        with lock:
            with self.guard:
                if self.maintenance or not self.enabled:return
                self.active_id=j['id'];self.cancel.clear();self.blocked='Processing '+j['name']
            try:
                with connect(self.settings['destination'],self._supplied_drive) as session:
                    session.check()
                self.connection_state='Connected';self.process(j);self.retry_delay=5;self.next_retry=0
            except Cancelled:self.update(j,state='Interrupted',detail='Stopped safely. Original retained.',percent=0)
            except Duplicate as e:self.update(j,state='Duplicate',detail=str(e)+' Original retained.',percent=0)
            except ContentConflict as e:self.update(j,state='Needs review',detail=str(e)+' Original retained.',percent=0)
            except (OSError,DriveMissing,DestinationUnavailable) as e:
                self.connection_state='Waiting';self.next_retry=time.monotonic()+self.retry_delay;self.retry_delay=min(60,self.retry_delay*2)
                self.update(j,state='Waiting for destination',detail=str(e)+' Queue is waiting; originals and local preparation retained.',percent=0)
            except Exception as e:
                self.update(j,state='Error',detail=str(e)+' Original retained.',percent=0)
                if isinstance(e,(OSError,DriveMissing)):self.enabled=False
            finally:self.active_id=None;self.save()
    def _checkpoint(self,root):
        if self.cancel.is_set() or self.closing.is_set():raise Cancelled()
        self.drive.check(root)
    def _metadata_match(self,root,metadata,excluded,checkpoint=lambda:None):
        required=('title_id','media_id','version','disc')
        if not all(k in metadata for k in required) or metadata.get('media_id')=='00000000':return None
        def same(other):return all(other.get(k)==metadata[k] for k in required)
        games=root/self.settings['games']
        if games.exists():
            for executable in game_executables(games,checkpoint):
                safe_path(executable,games)
                if executable.parent not in excluded and same(xex_info(executable)):return executable.parent
        packages=root/'Content'/'0000000000000000'/metadata['title_id']/'00007000'
        if packages.exists():
            for p in packages.iterdir():
                safe_path(p,root)
                if p.is_file() and p not in excluded:
                    info=package_info(p)
                    if info and same(info):return p
        return None
    def process(self,j):
        from pipeline import process
        return process(self,j)
    def verify_job(self,j):
        from pipeline import verify_job
        return verify_job(self,j)
    def refresh_cache_from_import(self,j,units,local):
        from library_cache import update_import
        update_import(self,j,units,local)
    def _delete_source(self,j,expected):
        p=safe_path(j['source'],j['source_root'])
        if p.parent!=Path(j['source_root']) or input_signature(p)!=expected:raise ValueError('Original changed; retained')
        j['cleanup_planned_at']=time.time();self.save(required=True)
        if p.is_dir():safe_remove_tree(p,Path(j['source_root']))
        else:p.unlink()
        j.update(source_retained=False,source_deleted_at=time.time(),freed=expected[0],cleanup_pending=False)
        self.log('Verified destination; deleted original '+str(p)+f'; {expected[0]/GIB:.2f} GB freed.');self.save(required=True)
    def cleanup_candidates(self):
        with self.guard:
            if self.active_id:raise ValueError('Finish the active import before cleaning temporary files')
            roots={str(self.work)};candidates=[]
            for j in self.jobs:
                for p in j.get('temp_paths',[]):roots.add(str(Path(p).parent))
            if self.drive_root:roots.add(str(self.drive_root/'Xbox360-Conversion-Staging'/'Converter'))
            owned={j['id'] for j in self.jobs}
            for r in roots:
                root=Path(r)
                if not root.exists():continue
                for p in root.iterdir():
                    if p.is_dir() and p.name in owned:
                        safe_path(p,root);candidates.append((p,root,tree_bytes(p)))
            return candidates
    def clean_leftovers(self):
        if self.active_id:raise ValueError('Current import is still active')
        freed=0
        with NamedLock():
            for p,r,n in self.cleanup_candidates():
                self.management_checkpoint();safe_remove_tree(p,r);freed+=n
        self.log(f'Cleaned {freed/GIB:.2f} GB of temporary files');return freed
    def cleanup_originals(self,ids):
        if self.active_id:raise ValueError('Finish the current import first')
        with NamedLock():
            for j in self.jobs:
                if j['id'] not in ids or j['state']!='Complete' or not j.get('source_retained') or not j.get('verification_manifest'):continue
                self.verify_job(j);self.management_checkpoint();self._delete_source(j,j['signature'])
    def verify_legacy_job(self,j):
        self.management_checkpoint();root=self.drive.locate()
        if j.get('drive_identity')!=self.settings['drive']:raise ValueError('Select the original destination USB before verifying')
        records=json.loads(Path(j['verification_manifest']).read_text('utf-8'))
        for record in records:
            base=safe_path(record['root'],root)
            for rel,expected in record['files'].items():
                p=safe_path(base/Path(rel),base);sha=hashlib.sha256()
                with p.open('rb') as f:
                    while chunk:=f.read(CHUNK):
                        self.management_checkpoint();self.drive.check(root);sha.update(chunk)
                if p.stat().st_size!=expected['size'] or sha.hexdigest()!=expected['sha256']:raise ValueError('Verification mismatch: '+str(p))
        return True
