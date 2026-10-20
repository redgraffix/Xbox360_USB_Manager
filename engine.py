"""Persistent import queue, configurable storage, history and USB inventory."""
from __future__ import annotations
import contextlib, copy, json, logging, logging.handlers, os, queue, shutil, threading, time, uuid
from pathlib import Path
from core import *
from content import *
SUPPORTED={'.iso','.zip','.7z'}
TERMINAL={'Complete','Duplicate','Error','Interrupted','Missing','Needs review'}
ACTIVE={'Unpacking','Checking','Converting','Copying','Verifying','Finishing'}

class ContentConflict(Exception):pass

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
        self.settings={'input':str(self.home),'temp':str(self.home/'_work'),'games':'Games','auto':True,
                       'cleanup':'keep','clean_temp':True,'drive':None}
        if self.settings_file.exists():self.settings.update(json.loads(self.settings_file.read_text('utf-8')))
        self.base=Path(self.settings['input']);self.work=Path(self.settings['temp']);self.work.mkdir(parents=True,exist_ok=True)
        self.auto=self.settings['auto'];self.delete_sources=self.settings['cleanup']=='delete'
        self.drive=drive or SelectedDrive(self.settings['drive'])
        self.logger=logging.getLogger('xbox360-'+uuid.uuid4().hex);self.logger.setLevel(logging.INFO);self.logger.propagate=False
        h=logging.handlers.RotatingFileHandler(self.state_dir/'activity.log',maxBytes=1024*1024,backupCount=2,encoding='utf-8')
        h.setFormatter(logging.Formatter('%(asctime)s %(message)s'));self.logger.addHandler(h)
        self._load()
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
            settings=copy.deepcopy(self.settings),batch_policy=self.batch_policy,maintenance=self.maintenance)
    def start_threads(self):
        for fn in (self._scan_loop,self._worker_loop):
            t=threading.Thread(target=fn,daemon=True);self.threads.append(t);t.start()
        self.log('Watching '+str(self.base)+'. Queue starts paused.')
    def enable(self):
        with self.guard:
            if self.maintenance:return
            self.enabled=True
            for j in self.jobs:
                if j['state']=='Ready':j['state']='Queued'
            self.save()
    def pause(self):
        self.enabled=False;self.blocked='Pausing after this file…' if self.active_id else 'Queue paused'
        self.log(self.blocked)
    def stop_current(self):self.enabled=False;self.cancel.set()
    def close(self):self.enabled=False;self.closing.set();self.cancel.set()
    def retry(self,ids):
        with self.guard:
            for j in self.jobs:
                if j['id'] in ids and j['state'] in TERMINAL-{'Complete'} and Path(j['source']).exists():
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
            if self.active_id or self.maintenance:raise ValueError('Pause and finish the current operation before changing settings')
            new={**self.settings,**settings};inp=Path(new['input']).absolute();temp=Path(new['temp']).absolute()
            safe_path(inp,inp);safe_path(temp,temp)
            if not inp.is_dir():raise ValueError('Choose an existing source folder')
            if inp==Path(inp.anchor):raise ValueError('Choose a source folder, not an entire drive')
            if temp==inp or inp.is_relative_to(temp):raise ValueError('Temporary storage must not contain the source folder')
            if inp==self.state_dir or inp.is_relative_to(self.state_dir):raise ValueError('State storage cannot be the input folder')
            rel=Path(new['games'])
            if rel.is_absolute() or not rel.parts or any(p in ('.','..') for p in rel.parts):raise ValueError('Games folder must be relative to the USB drive')
            for p in rel.parts:valid_name(p)
            if rel.parts[0].casefold() in {'content','xbox360-conversion-staging'}:raise ValueError('Games folder conflicts with content/staging folders')
            selected=SelectedDrive(new['drive'])
            if new['drive']:
                root=selected.locate()
                for dest in (root/rel,root/'Content',root/'Xbox360-Conversion-Staging'):
                    if inp==dest or inp.is_relative_to(dest) or dest.is_relative_to(inp) or temp==dest or temp.is_relative_to(dest):
                        raise ValueError('Source/temp folders overlap USB output folders')
            if new['cleanup'] not in {'keep','delete','ask'}:raise ValueError('Invalid cleanup preference')
            temp.mkdir(parents=True,exist_ok=True);new.update(input=str(inp),temp=str(temp))
            atomic_json(self.settings_file,new,True)
            self.enabled=False;self.settings=new;self.base=inp;self.work=temp;self.auto=new['auto'];self.delete_sources=new['cleanup']=='delete'
            self.drive=self._supplied_drive or selected;self.stable.clear();self.save()
            self.log('Settings saved. Queue paused; existing queued jobs retained.')
    def clear_finished(self):
        with self.guard:
            count=0
            for j in self.jobs:
                if j['state'] in TERMINAL and not j.get('hidden'):
                    j['hidden']=True;count+=1
            self.save(required=True)
            return count
    def begin_maintenance(self):
        with self.guard:
            if self.active_id or self.maintenance:raise ValueError('Wait for the current file to finish first.')
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
        try:root=self.drive.locate();free=shutil.disk_usage(root).free
        except Exception:root=None;free=None
        with self.guard:
            self.drive_root,self.drive_free=root,free
            if not self.base.is_dir():self.blocked='Source folder is unavailable';return
            files=[]
            for p in self.base.iterdir():
                if p.name.startswith(('_','.')) or p==self.work or p==self.state_dir:continue
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
                if j['state'] in ACTIVE:continue
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
        if not self.drive_root:self.blocked='Connect the selected USB drive or choose one in Settings';return
        if batch_running():self.blocked='Waiting for the previous conversion batch to finish';return
        with self.guard:j=next((j for j in self.jobs if j['state']=='Queued'),None)
        if not j:self.blocked='Watching for new files' if self.auto else 'No queued games';return
        try:lock=NamedLock()
        except BlockingIOError:return
        with lock:
            with self.guard:
                if self.maintenance or not self.enabled:return
                self.active_id=j['id'];self.cancel.clear();self.blocked='Processing '+j['name']
            try:self.process(j)
            except Cancelled:self.update(j,state='Interrupted',detail='Stopped safely. Original retained.',percent=0)
            except Duplicate as e:self.update(j,state='Duplicate',detail=str(e)+' Original retained.',percent=0)
            except ContentConflict as e:self.update(j,state='Needs review',detail=str(e)+' Original retained.',percent=0)
            except Exception as e:
                self.update(j,state='Error',detail=str(e)+' Original retained.',percent=0)
                if isinstance(e,(OSError,DriveMissing)):self.enabled=False
            finally:self.active_id=None;self.save()
    def _checkpoint(self,root):
        if self.cancel.is_set() or self.closing.is_set():raise Cancelled()
        self.drive.check(root)
    def _metadata_match(self,root,metadata,excluded):
        required=('title_id','media_id','version','disc')
        if not all(k in metadata for k in required) or metadata.get('media_id')=='00000000':return None
        def same(other):return all(other.get(k)==metadata[k] for k in required)
        games=root/self.settings['games']
        if games.exists():
            for executable in games.rglob('default.xex'):
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
        j.setdefault('source_root',str(self.base))
        source=safe_path(j['source'],j.get('source_root',self.base))
        if source.parent!=Path(j.get('source_root',self.base)):raise ValueError('Source is outside its selected folder')
        initial=input_signature(source)
        if initial!=j['signature']:raise ValueError('Input changed; retry after copying finishes')
        root=self.drive.locate();self.drive.check(root)
        staging_root=safe_path(root/'Xbox360-Conversion-Staging'/'Converter',root)
        staging=safe_path(staging_root/j['id'],staging_root);local=safe_path(self.work/j['id'],self.work)
        for p,r in ((staging,staging_root),(local,self.work)):safe_remove_tree(p,r)
        j['temp_paths']=[str(local),str(staging)]
        policy=self.settings['cleanup'] if self.batch_policy=='default' else self.batch_policy
        if self.batch_policy=='default' and self.settings['cleanup']!='ask':policy='delete' if self.delete_sources else 'keep'
        j['cleanup_policy']=policy;j['drive_identity']=copy.deepcopy(self.settings['drive'])
        owned_before=set(j.get('outputs',[]));last=[0.0];last_drive=[0.0];started=time.monotonic();units=[];unknown=[];iso_list=[];manifest_records=[]
        def checkpoint():
            if self.cancel.is_set() or self.closing.is_set():raise Cancelled()
            if time.monotonic()-last_drive[0]>1:self.drive.check(root);last_drive[0]=time.monotonic()
        def progress(phase,done,total,detail):
            now=time.monotonic()
            if now-last[0]<.25 and done!=total and j['state']==phase:return
            last[0]=now;self.update(j,state=phase,percent=min(100,100*done/total) if total else 100,done=done,total=total,detail=detail,elapsed=now-started)
        def convert_iso(iso,label,index):
            with locked_reader(iso) as image:
                entries=manifest(image);total=sum(n for _,_,n in entries)
                if shutil.disk_usage(root).free<2*total+GIB:raise OSError('USB needs space for prepared files and verified output: '+f'{(2*total+GIB)/GIB:.1f} GB')
                target=staging/'converted'/str(index)
                extract_and_verify(image,entries,target,progress,checkpoint)
            got,nested,unhandled=inspect_tree(target,label)
            if nested:raise ValueError('Unexpected nested disc images inside ISO')
            units.extend(got);unknown.extend(unhandled)
        self.update(j,state='Checking',detail='Detecting ISO, prepared game, GOD, DLC or update content',percent=0)
        try:
            with (locked_reader(source) if source.is_file() else contextlib.nullcontext()):
                if source.suffix.lower()=='.iso':convert_iso(source,source.stem,0)
                elif source.suffix.lower() in {'.zip','.7z'}:
                    records=archive_inventory(self.tool,source)
                    iso_records=[r for r in records if r['relative'].suffix.lower()=='.iso']
                    other=[r for r in records if r not in iso_records and r['relative'].suffix.lower() not in AUX]
                    if iso_records and not other:
                        for i,r in enumerate(iso_records):
                            checkpoint()
                            if shutil.disk_usage(self.work).free<r['size']+GIB:raise OSError('Temporary location needs '+f"{(r['size']+GIB)/GIB:.1f} GB free")
                            if r['size']>0xFFFFFFFF and any(str(self.work).startswith(v['root']) and v['filesystem']=='FAT32' for v in volumes()):raise ValueError('Choose NTFS/exFAT temporary storage for ISO files larger than 4 GB')
                            iso=unpack_member(self.tool,source,r['name'],r['size'],local,progress,checkpoint)
                            convert_iso(iso,Path(r['name'].replace('\\','/')).stem,i)
                            safe_path(iso,local).unlink() if self.settings['clean_temp'] else None
                    else:
                        total=sum(r['size'] for r in records)
                        # Prepared archives can unpack on USB; mixed archives with large ISOs use selected temp storage.
                        unpack=local/'archive' if any(r['size']>0xFFFFFFFF for r in records) else staging/'archive'
                        disk=self.work if unpack.is_relative_to(local) else root
                        if shutil.disk_usage(disk).free<total+GIB:raise OSError('Not enough extraction space')
                        if unpack.is_relative_to(staging) and shutil.disk_usage(root).free<2*total+GIB:raise OSError('USB needs room for extraction and verified content')
                        if any(r['size']>0xFFFFFFFF for r in records) and any(str(disk).startswith(v['root']) and v['filesystem']=='FAT32' for v in volumes()):raise ValueError('Select NTFS/exFAT temporary storage for files over 4 GB')
                        unpack_all(self.tool,source,unpack,records,progress,checkpoint)
                        got,isos,extra=inspect_tree(unpack,source.stem);units.extend(got);unknown.extend(extra)
                        for i,iso in enumerate(isos):convert_iso(iso,iso.stem,i)
                elif source.is_dir():
                    got,isos,extra=inspect_tree(source,source.name);units.extend(got);unknown.extend(extra)
                    for i,iso in enumerate(isos):convert_iso(iso,iso.stem,i)
                else:
                    info=package_info(source)
                    if not info or info['kind']==0x7000:raise ValueError('Place a GOD descriptor and its .data folder together in one input folder')
                    units=[dict(name=info['name'],kind=CONTENT_TYPES[info['kind']],metadata=info,files=[(source,Path(source.name))],destination=Path('Content')/'0000000000000000'/info['title_id']/f"{info['kind']:08X}",package=source.name)]
                if not units:raise ValueError('No supported Xbox game/content found. Input kept for review.')
                if any(u['metadata'].get('title_id')=='FFFE07D1' for u in units):
                    self.update(j,state='Needs review',percent=0,detail='Standalone console update found. Original retained for manual review.');return
                if any(u['kind']=='Title update' and (u.get('package') or '').upper().startswith('TU_') for u in units):
                    self.update(j,state='Needs review',percent=0,detail='Legacy TU_ update needs manual placement; this input is retained. No content was published.');return
                if unknown:
                    self.update(j,state='Needs review',percent=0,detail='Unrecognized files; original retained: '+', '.join(unknown[:6]));return
                all_targets=set();published=0;j['format']=', '.join(sorted({u['kind'] for u in units}));j['components']=[]
                for i,u in enumerate(units):
                    checkpoint();rel=u['destination']
                    if rel.parts[0]=='Games':rel=Path(self.settings['games']).joinpath(*rel.parts[1:])
                    dest=safe_path(root/rel,root);package=u['package']
                    targets=[dest/Path(package)] if package else [dest]
                    if package and any(len(r.parts)>1 for _,r in u['files']):targets.append(dest/(package+'.data'))
                    for target in targets:
                        if str(target).casefold() in all_targets:raise ContentConflict('Two items in this input use the same destination')
                        all_targets.add(str(target).casefold())
                    if u['kind'] in {'Extracted game','Games on Demand'}:
                        match=self._metadata_match(root,u['metadata'],targets)
                        if match:self.log('Similar game metadata at '+str(match)+'; metadata alone does not establish a duplicate.')
                    exists=[p.exists() for p in targets]
                    if any(exists):
                        if not all(exists):raise ContentConflict('An incomplete copy already exists at '+str(dest))
                        if not package:
                            expected={r.as_posix().casefold():p.stat().st_size for p,r in u['files']}
                            if actual_files(dest)!=expected:raise ContentConflict('Different files already exist at '+str(dest))
                        try:hashes=verify_files(u['files'],dest,progress,checkpoint)
                        except ValueError as e:raise ContentConflict('Files at '+str(dest)+' differ from this input; nothing was overwritten.') from e
                    else:
                        if not package:
                            exe=next((p for p,r in u['files'] if r.as_posix().lower()=='default.xex'),None)
                            fingerprint=identity([(r,0,p.stat().st_size) for p,r in u['files']],exe.read_bytes()) if exe and exe.stat().st_size<=128*1024*1024 else None
                            games=root/self.settings['games']
                            if fingerprint and games.exists():
                                for existing in games.rglob('default.xex'):
                                    checkpoint();safe_path(existing,games)
                                    if folder_identity(existing.parent)==fingerprint:
                                        try:verify_files(u['files'],existing.parent,progress,checkpoint)
                                        except ValueError:continue
                                        raise Duplicate('Identical game content verified at '+str(existing.parent))
                        need=sum(p.stat().st_size for p,_ in u['files'])
                        if shutil.disk_usage(root).free<need+GIB:raise OSError('Not enough USB free space')
                        stage=staging/'publish'/str(i);hashes=verify_files(u['files'],stage,progress,checkpoint,True)
                        checkpoint();dest.parent.mkdir(parents=True,exist_ok=True)
                        if package:
                            dest.mkdir(exist_ok=True)
                            for target in targets:
                                if target.exists():raise ContentConflict('The destination changed during transfer; nothing was overwritten')
                                (stage/target.name).rename(target)
                        else:
                            if dest.exists():raise ContentConflict('The destination changed during transfer; nothing was overwritten')
                            stage.rename(dest)
                        published+=1
                    manifest_records.append(dict(root=str(dest),files=hashes,metadata=u['metadata'],kind=u['kind'],name=u['name']))
                    j['outputs']=sorted(set(j.get('outputs',[]))|{str(p) for p in targets});j['components'].append(dict(name=u['name'],kind=u['kind'],metadata=u['metadata'],paths=[str(p) for p in targets]))
                    self.save(required=True)
                if input_signature(source)!=initial:raise ValueError('Source changed during import; original retained')
                checkpoint();self.update(j,state='Finishing',percent=100,detail='Verified files saved; recording results')
                manifests=self.state_dir/'manifests';manifests.mkdir(exist_ok=True)
                atomic_json(manifests/(j['id']+'.json'),manifest_records,True);j['verification_manifest']=str(manifests/(j['id']+'.json'))
                j['verified_at']=time.time();j['cleanup_pending']=policy=='ask' and published>0
                j['source_retained']=True;self.save(required=True)
            if not published and not owned_before:raise Duplicate('All content is already present and verified.')
            if policy=='delete':self._delete_source(j,initial)
            text=f"Imported {len(units)} item(s). "+('Original removed after verification.' if not j.get('source_retained',True) else 'Original retained.' )
            j['setup_note']='Check/enable title updates on Xbox.' if any(u['kind']=='Title update' for u in units) else ('Installation content included; some titles need Xbox setup.' if any(u['kind'] in {'Install disc','DLC / install content'} for u in units) else 'Files verified; launch on Xbox to test.')
            self.update(j,state='Complete',percent=100,detail=text,elapsed=time.monotonic()-started)
        finally:
            if self.settings['clean_temp']:
                for p,r in ((local,self.work),(staging,staging_root)):
                    try:
                        if p==staging:self.drive.check(root)
                        safe_remove_tree(p,r)
                    except Exception as e:self.log('Temporary cleanup deferred: '+str(e))
    def _delete_source(self,j,expected):
        p=safe_path(j['source'],j['source_root'])
        if p.parent!=Path(j['source_root']) or input_signature(p)!=expected:raise ValueError('Original changed; retained')
        j['cleanup_planned_at']=time.time();self.save(required=True)
        if p.is_dir():safe_remove_tree(p,Path(j['source_root']))
        else:p.unlink()
        j.update(source_retained=False,source_deleted_at=time.time(),freed=expected[0],cleanup_pending=False)
        self.log('Verified USB output; deleted original '+str(p)+f'; {expected[0]/GIB:.2f} GB freed.');self.save(required=True)
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
    def verify_job(self,j):
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
