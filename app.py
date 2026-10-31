"""Xbox 360 Game Manager — local Windows utility."""
from __future__ import annotations
import argparse, ctypes, os, sys, threading, time, traceback, queue as queues
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from core import NamedLock, GIB, Cancelled
from engine import Engine, ACTIVE
from runtime import find_7zip, default_base
from content import volumes, tree_bytes
import library_cache
import theme
from destinations import connect, normalize, describe, identity, location_label
from PIL import Image, ImageTk, ImageDraw
import credentials
BG='#151b18';PANEL='#202a24';FG='#edf1ed';MUTED='#a9b8ad';GREEN='#92d34f';LINE='#35453a'
POLICIES={'Keep source files':'keep','Delete source files':'delete','Ask before deleting':'ask'}
def resource(name):return Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/name
def size(n):
    if n is None:return '—'
    return f'{n/GIB:.2f} GB' if n>=GIB else f'{n/1024**2:.1f} MB' if n>=1024**2 else f'{n/1024:.0f} KB'
def duration(n):
    n=max(0,int(n));return f'{n//3600}h {(n%3600)//60}m' if n>=3600 else f'{n//60}m {n%60}s'
def date(n):return time.strftime('%b %d, %H:%M',time.localtime(n)) if n else '—'

class App:
    def __init__(self,root,engine):
        self.root,self.engine=root,engine;self.rows={};self.last_snapshot=None;self.closing=False;self.library=[];self.callbacks=queues.Queue();self.busy=False;self.asked=set();self.last_history=None;self.library_loaded=False
        self.library_loading=False;self.library_cancel=threading.Event();self.library_events=queues.Queue();self.library_progress='';self.library_thread=None;self.operation_message=''
        root.title('Xbox 360 Game Manager');root.configure(bg=BG);root.geometry(f'{min(1420,root.winfo_screenwidth()-100)}x{min(940,root.winfo_screenheight()-100)}+40+30');root.minsize(1020,780);root.protocol('WM_DELETE_WINDOW',self.close)
        self.style=theme.apply(root)
        outer=ttk.Frame(root,padding=(20,16));outer.pack(fill='both',expand=True)
        header=ttk.Frame(outer);header.pack(fill='x')
        ttk.Button(header,text='Settings',command=self.settings).pack(side='right')
        ttk.Label(header,text='Xbox 360 Game Manager',font=('Segoe UI Semibold',20),foreground=GREEN).pack(anchor='w')
        ttk.Label(header,text='Prepare games locally. Transfer to a folder, USB drive, or Xbox FTP.',style='Muted.TLabel').pack(anchor='w',pady=(3,12))
        locations=ttk.Frame(outer);locations.pack(fill='x',pady=(0,18));locations.columnconfigure(1,weight=1)
        self.source_var=tk.StringVar(value=str(engine.base));self.drive_var=tk.StringVar(value='Checking selected drive…')
        for row,label,var in [(0,'Input folder',self.source_var),(1,'Destination',self.drive_var)]:
            ttk.Label(locations,text=label,style='Muted.TLabel',width=14).grid(row=row,column=0,sticky='w',pady=3)
            ttk.Label(locations,textvariable=var).grid(row=row,column=1,sticky='w')
        ttk.Button(locations,text='Open input folder',command=self.open_source).grid(row=0,column=2,padx=(12,0),pady=(0,5),sticky='ew')
        ttk.Button(locations,text='Open destination',command=self.open_games).grid(row=1,column=2,padx=(12,0),pady=(5,0),sticky='ew')
        self.footer_var=tk.StringVar();self.footer=ttk.Label(outer,textvariable=self.footer_var,style='Muted.TLabel',font=('Segoe UI',9));self.footer.pack(side='bottom',anchor='w',pady=(6,0))
        self.tabs=ttk.Notebook(outer);self.tabs.pack(fill='both',expand=True)
        self.queue_tab=ttk.Frame(self.tabs,padding=16);self.games_tab=ttk.Frame(self.tabs,padding=16);self.history_tab=ttk.Frame(self.tabs,padding=16)
        for frame,name in [(self.queue_tab,'Queue'),(self.games_tab,'Games'),(self.history_tab,'History')]:self.tabs.add(frame,text=name)
        self.tabs.bind('<<NotebookTabChanged>>',self.tab_changed)
        self.build_queue();self.build_games();self.build_history()
        self.thumbnail_images={};self.cache_revision=-1;self.load_cached_library();engine.start_threads();self.tick()
    def table(self,parent,columns):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
        tree=ttk.Treeview(frame,columns=[c[0] for c in columns],show='headings',height=5,selectmode='extended')
        for key,label,width in columns:tree.heading(key,text=label);tree.column(key,width=width,minwidth=65,stretch=key=='name')
        sb=ttk.Scrollbar(frame,orient='vertical',command=tree.yview);tree.configure(yscrollcommand=sb.set);tree.pack(side='left',fill='both',expand=True);sb.pack(side='right',fill='y')
        for tag,color in [('Complete',GREEN),('Error','#ffb5a4'),('Needs review','#f1d48b'),('Duplicate','#f1d48b')]:tree.tag_configure(tag,foreground=color)
        return tree
    def build_queue(self):
        p=self.queue_tab;toolbar=ttk.Frame(p);toolbar.pack(fill='x',pady=(0,10))
        self.start=ttk.Button(toolbar,text='Start queue',style='Accent.TButton',command=self.start_queue);self.start.pack(side='left')
        self.pause=ttk.Button(toolbar,text='Pause',command=self.engine.pause);self.pause.pack(side='left',padx=7)
        self.stop=ttk.Button(toolbar,text='Stop',command=self.stop_work);self.stop.pack(side='left')
        self.clear_button=ttk.Button(toolbar,text='Clear completed & errors',command=self.clear_finished);self.clear_button.pack(side='right')
        row=ttk.Frame(p);row.pack(fill='x',pady=(2,8))
        self.auto_var=tk.BooleanVar(value=self.engine.auto)
        ttk.Checkbutton(row,text='Add new files automatically',variable=self.auto_var,command=lambda:self.call(lambda:self.engine.options(auto=self.auto_var.get()))).pack(side='left')
        ttk.Label(row,text='After transfer:',style='Muted.TLabel').pack(side='left',padx=(28,10))
        self.batch_var=tk.StringVar(value=next(k for k,v in POLICIES.items() if v==self.engine.settings['cleanup']))
        self.batch_combo=ttk.Combobox(row,textvariable=self.batch_var,values=list(POLICIES),state='readonly',width=23)
        self.batch_combo.pack(side='left')
        self.batch_combo.bind('<<ComboboxSelected>>',lambda e:setattr(self.engine,'batch_policy',POLICIES[self.batch_var.get()]))
        ttk.Label(p,text='Source files are only deleted after the destination copy is verified.',style='Muted.TLabel',font=('Segoe UI',9)).pack(anchor='w',pady=(0,12))
        self.status_var=tk.StringVar(value='Queue paused');ttk.Label(p,textvariable=self.status_var,font=('Segoe UI Semibold',11),foreground=GREEN).pack(anchor='w',pady=(0,7))
        bottom=ttk.Frame(p);bottom.pack(side='bottom',fill='x',pady=(8,0))
        self.tree=self.table(p,[('name','File / game',600),('size','Size',90),('state','Status',115),('progress','Progress',85)])
        self.tree.bind('<<TreeviewSelect>>',lambda e:self.show_selection())
        detail=ttk.Frame(bottom);detail.pack(fill='x',pady=8)
        self.detail_var=tk.StringVar(value='Watching the source folder. Start the queue when ready.')
        ttk.Label(detail,textvariable=self.detail_var,style='Muted.TLabel',wraplength=820).pack(side='left',fill='x',expand=True)
        self.retry_button=ttk.Button(detail,text='Retry selected',command=lambda:self.engine.retry(list(self.tree.selection())));self.retry_button.pack(side='right',padx=(12,0))
        self.progress_var=tk.StringVar(value='Ready when you are. Click Start queue.');ttk.Label(bottom,textvariable=self.progress_var,style='Muted.TLabel').pack(anchor='w',pady=(4,4))
        self.bar=ttk.Progressbar(bottom,maximum=100);self.bar.pack(fill='x')
        ttk.Label(bottom,text='Activity',style='Muted.TLabel').pack(anchor='w',pady=(10,3))
        self.log=tk.Text(bottom,height=3,bg=PANEL,fg=MUTED,font=('Consolas',9),relief='flat',padx=12,pady=10,wrap='word',state='disabled');self.log.pack(fill='x')
    def build_games(self):
        row=ttk.Frame(self.games_tab);row.pack(fill='x',pady=(0,10))
        self.search=tk.StringVar();ttk.Entry(row,textvariable=self.search,width=36).pack(side='left');self.search.trace_add('write',lambda *a:self.render_library())
        ttk.Label(row,text='  Search games',style='Muted.TLabel').pack(side='left')
        self.refresh_button=ttk.Button(row,text='Refresh list',command=self.refresh_library);self.refresh_button.pack(side='right')
        self.cancel_scan_button=ttk.Button(row,text='Stop scan',command=self.cancel_library,state='disabled');self.cancel_scan_button.pack(side='right',padx=8)
        self.games_tree=self.table(self.games_tab,[('size','Size',90),('format','Content',220),('discs','Discs',65),('location','Location',120)])
        self.games_tree.configure(show='tree headings',style='Games.Treeview');self.games_tree.heading('#0',text='Game');self.games_tree.column('#0',width=390,minwidth=220)
        self.games_tree.bind('<<TreeviewSelect>>',lambda e:self.game_details());self.games_tree.bind('<Double-1>',lambda e:self.open_selected_game())
        self.game_detail=tk.StringVar(value='Refresh to list games and content on the selected destination.')
        ttk.Label(self.games_tab,textvariable=self.game_detail,style='Muted.TLabel',wraplength=1050).pack(anchor='w',pady=10)
        row=ttk.Frame(self.games_tab);row.pack(fill='x')
        ttk.Button(row,text='Open folder',command=self.open_selected_game).pack(side='left')
        ttk.Button(row,text='Verify files',command=self.verify_selected_game).pack(side='left',padx=7)
        ttk.Button(row,text='Remove…',command=self.remove_game).pack(side='right')
    def build_history(self):
        row=ttk.Frame(self.history_tab);row.pack(fill='x',pady=(0,10))
        ttk.Label(row,text='Import history stays here when you clear the queue.',style='Muted.TLabel').pack(side='left')
        ttk.Button(row,text='Open logs',command=lambda:self.open_path(self.engine.state_dir)).pack(side='right')
        self.history=self.table(self.history_tab,[('date','Date',130),('name','File / game',420),('format','Action / format',200),('state','Result',105),('cleanup','Original',110)])
        self.history.bind('<<TreeviewSelect>>',lambda e:self.history_details())
        self.history_detail=tk.StringVar(value='Select an entry for source, destination, verification and cleanup details.')
        ttk.Label(self.history_tab,textvariable=self.history_detail,style='Muted.TLabel',wraplength=1050).pack(anchor='w',pady=10)
        row=ttk.Frame(self.history_tab);row.pack(fill='x')
        ttk.Button(row,text='Open original location',command=self.open_history_source).pack(side='left')
        ttk.Button(row,text='Import again',command=lambda:self.call(lambda:self.engine.reimport(list(self.history.selection())))).pack(side='left',padx=7)
        ttk.Button(row,text='Delete verified originals…',command=self.cleanup_selected).pack(side='right')
    def call(self,fn):
        try:return fn()
        except Exception as e:messagebox.showerror('Xbox 360 Game Manager',str(e),parent=self.root)
    def open_path(self,p):
        p=Path(p)
        if p.exists():os.startfile(p)
        else:messagebox.showinfo('Unavailable','This location is not currently available:\n'+str(p),parent=self.root)
    def open_source(self):self.open_path(self.engine.base)
    def open_games(self):
        d=self.engine.settings['destination']
        if d['kind']=='ftp':
            self.tabs.select(self.games_tab)
            messagebox.showinfo('Xbox destination',describe(d)+'\nUse Settings → Browse Xbox to choose a remote location.',parent=self.root)
        elif self.engine.drive_root:self.open_path(self.engine.drive_root)
        else:messagebox.showinfo('Destination unavailable','Connect the selected destination or choose it in Settings.',parent=self.root)
    def start_queue(self):
        self.cancel_library();self.engine.enable()
    def stop_work(self):
        if self.busy:self.engine.management_cancel.set();self.operation_message='Stopping…'
        else:self.engine.stop_current()
    def clear_finished(self):
        count=self.call(self.engine.clear_finished);self.last_history=None
        if count is not None:self.detail_var.set(f'Cleared {count} completed, failed or duplicate entries. History and files are unchanged.')

    def show_selection(self):
        ids=self.tree.selection()
        if ids and self.last_snapshot:
            j=next((x for x in self.last_snapshot['jobs'] if x['id']==ids[0]),None)
            if j:self.detail_var.set('Selected: '+j.get('detail',''))
    def tab_changed(self,event=None):
        if self.tabs.select()==str(self.games_tab) and not self.library_loaded:self.load_cached_library()
    def load_cached_library(self):
        data=library_cache.load(self.engine);self.library=data['items'];self.library_loaded=True
        self.cache_revision=self.engine.cache_revision;self.render_library()
        stamp=date(data.get('refreshed')) if data.get('refreshed') else 'not yet scanned'
        self.game_detail.set(f"{len(self.library)} cached games · Last refreshed: {stamp}. Refresh lists folders; sizes and artwork use cached details.")
    def background(self,fn,done=None,message='Working…'):
        if self.busy:
            messagebox.showinfo('Please wait','Another file operation is still running.',parent=self.root);return
        try:self.engine.begin_maintenance()
        except ValueError as e:messagebox.showinfo('Please wait',str(e),parent=self.root);return
        self.cancel_library();self.busy=True;self.operation_message=message;self.game_detail.set(message)
        def run():
            result=None;error=None
            try:result=fn()
            except Exception as e:error=e
            finally:
                self.engine.end_maintenance()
                self.callbacks.put((done,result,error))
        threading.Thread(target=run,daemon=True).start()
    def cancel_library(self):
        if self.library_loading:self.library_cancel.set()
    def refresh_library(self):
        if self.library_loading:return
        self.library_cancel=threading.Event();cancel=self.library_cancel;self.library_loading=True
        self.library_progress='Refreshing destination game list…';self.game_detail.set(self.library_progress)
        self.refresh_button.configure(state='disabled');self.cancel_scan_button.configure(state='normal')
        expected=identity(self.engine.settings['destination'])
        def progress(message):self.library_progress=message
        def preview(items):self.library_events.put((items,'preview',cancel))
        def run():
            try:
                result=library_cache.scan(self.engine,cancel,progress,preview)
                if identity(self.engine.settings['destination'])!=expected:raise Cancelled()
                self.library_events.put((result,None,cancel))
            except Exception as e:self.library_events.put((None,e,cancel))
        self.library_thread=threading.Thread(target=run,daemon=True);self.library_thread.start()
    def finish_library(self):
        while not self.library_events.empty():
            items,error,cancel=self.library_events.get()
            if cancel is not self.library_cancel:continue
            if error=='preview':
                if not cancel.is_set():self.library=items;self.library_loaded=True;self.render_library()
                continue
            self.library_loading=False;self.refresh_button.configure(state='normal');self.cancel_scan_button.configure(state='disabled')
            if cancel.is_set() or isinstance(error,Cancelled):self.game_detail.set('Scan stopped. Click Refresh list to try again.')
            elif error:self.game_detail.set('Could not refresh; cached list kept. '+str(error));self.engine.log('Game list: '+str(error))
            else:
                self.library=items;self.library_loaded=True;self.render_library();self.game_detail.set(f'{len(items)} games · {location_label(self.engine.settings["destination"])} · Quick refresh complete. Sizes and artwork use cached details; — means unknown.')
        if self.library_loading:self.game_detail.set(self.library_progress)
    def game_image(self,g):
        name=g.get('thumbnail') or 'placeholder'
        if name not in self.thumbnail_images:
            if name!='placeholder':
                try:
                    path=self.engine.state_dir/'thumbnails'/Path(name).name
                    with Image.open(path) as image:picture=image.convert('RGBA').resize((36,36))
                except (OSError,ValueError):name='placeholder'
            if name=='placeholder':
                picture=Image.new('RGBA',(36,36),PANEL);draw=ImageDraw.Draw(picture)
                draw.rounded_rectangle((1,1,34,34),radius=7,fill=LINE)
                draw.line((10,10,26,26),fill=GREEN,width=3);draw.line((26,10,10,26),fill=GREEN,width=3)
            self.thumbnail_images[name]=ImageTk.PhotoImage(picture,master=self.root)
        return self.thumbnail_images[name]
    def render_library(self):
        if not hasattr(self,'games_tree'):return
        selected=self.games_tree.selection();self.games_tree.delete(*self.games_tree.get_children())
        for g in self.library:
            if self.search.get().casefold() not in (g['name']+' '+g['title_id']).casefold():continue
            label=g.get('location',location_label(self.engine.settings['destination']))
            self.games_tree.insert('','end',iid=g['id'],text='  '+g['name'],image=self.game_image(g),
                values=(size(g['size']),g['formats'],g['discs'] if g['discs']>1 else '—',label))
        for ident in selected:
            if self.games_tree.exists(ident):self.games_tree.selection_add(ident)
    def selected_game(self):
        ids=self.games_tree.selection();return next((g for g in self.library if ids and g['id']==ids[0]),None)
    def game_details(self):
        g=self.selected_game()
        if g:self.game_detail.set(g['name']+' · '+size(g['size'])+'\n'+'; '.join(c['kind']+': '+c['name'] for c in g['components']))
    def open_selected_game(self):
        g=self.selected_game()
        if g:
            rel=g['components'][0]['paths'][0];dest=self.engine.settings['destination']
            if dest['kind']=='ftp':
                messagebox.showinfo('Xbox game location',describe(dest)+'/'+rel,parent=self.root);return
            root=self.engine.drive_root
            if not root:messagebox.showinfo('Destination unavailable','Connect the selected destination first.',parent=self.root);return
            p=Path(root)/rel;self.open_path(p if p.is_dir() else p.parent)
    def verify_selected_game(self):
        g=self.selected_game()
        if not g:return
        paths={p for c in g['components'] for p in c['paths']}
        jobs=[j for j in self.engine.jobs if j.get('verification_manifest') and j.get('destination_id')==identity(self.engine.settings['destination']) and paths.intersection(j.get('outputs',[]))]
        if not jobs:messagebox.showinfo('Verification unavailable','This game predates saved checksums or was added outside the app. Reimport its original to verify it fully.',parent=self.root);return
        self.background(lambda:[self.engine.verify_job(j) for j in jobs],lambda _:self.game_detail.set('Verification passed for '+g['name']),'Verifying destination files…')
    def remove_game(self):
        g=self.selected_game()
        if not g:return
        if self.engine.active_id or self.busy:messagebox.showinfo('Import running','Finish the current operation first.',parent=self.root);return
        win=tk.Toplevel(self.root);win.title('Remove from destination');win.geometry('640x410');win.transient(self.root)
        box=ttk.Frame(win,padding=18);box.pack(fill='both',expand=True)
        ttk.Label(box,text='Select exactly what to remove',font=('Segoe UI Semibold',15)).pack(anchor='w')
        ttk.Label(box,text=g['name'],foreground=GREEN).pack(anchor='w',pady=8)
        listing=tk.Listbox(box,selectmode='extended',exportselection=False,bg=PANEL,fg=FG,font=('Segoe UI',10),height=9);listing.pack(fill='both',expand=True)
        for i,c in enumerate(g['components']):
            listing.insert('end',c['kind']+' · '+c['name']+' · '+size(c['size']))
            if c['kind'] in {'Extracted game','Game folder','Games on Demand','Arcade game','Install disc'}:listing.selection_set(i)
        ttk.Label(box,text='DLC and updates require their own selection. Removal is permanent.',wraplength=590,style='Muted.TLabel').pack(anchor='w',pady=10)
        def remove():
            chosen=[g['components'][i] for i in listing.curselection()]
            if not chosen:return
            win.destroy()
            def done(_):self.library_loaded=False;self.game_detail.set('Selected content removed. Refresh the list.');self.last_history=None
            def confirm(checked):
                paths='\n'.join(p for c in checked for p in c['paths'])
                count=sum(len(c['files']) for c in checked)
                if messagebox.askyesno('Remove selected content?',f"Permanently remove {count} files ({size(sum(c['size'] for c in checked))}) from the selected destination?\n\n{paths}",parent=self.root):
                    self.background(lambda:library_cache.remove(self.engine,checked),done,'Removing selected content…')
            self.background(lambda:library_cache.prepare_removal(self.engine,chosen),confirm,'Checking selected content before removal…')
        ttk.Button(box,text='Remove selected…',command=remove).pack(anchor='e')
    def history_details(self):
        ids=self.history.selection()
        if not ids:return
        j=next((x for x in self.engine.jobs if x['id']==ids[0]),None)
        if j:self.history_detail.set(f"Source: {j['source']}\nOutput: {'; '.join(j.get('outputs',[])) or '—'}\n{j.get('detail','')} "+j.get('setup_note','')+(f"\nOriginal deleted {date(j['source_deleted_at'])}; {size(j.get('freed',0))} freed." if j.get('source_deleted_at') else ''))
        elif ids[0].startswith('event-'):
            e=self.engine.history_events[int(ids[0].split('-')[1])];self.history_detail.set(e['action']+'\n'+'\n'.join(e['paths']))
    def open_history_source(self):
        ids=self.history.selection();j=next((x for x in self.engine.jobs if ids and x['id']==ids[0]),None)
        if j:self.open_path(Path(j['source']).parent)
    def cleanup_selected(self,ids=None):
        ids=list(ids or self.history.selection());jobs=[j for j in self.engine.jobs if j['id'] in ids and j['state']=='Complete' and j.get('source_retained') and j.get('verification_manifest') and Path(j['source']).exists()]
        if not jobs:messagebox.showinfo('No originals selected','Select completed imports with retained originals and saved verification records.',parent=self.root);return
        text='\n'.join(j['source'] for j in jobs)
        if not messagebox.askyesno('Delete verified originals?',f"Recheck USB files, then permanently delete these originals?\nChoose No to keep them.\n\n{text}",parent=self.root):
            self.call(lambda:self.engine.keep_originals([j['id'] for j in jobs]));return
        self.background(lambda:self.engine.cleanup_originals([j['id'] for j in jobs]),lambda _:self.finish_cleanup(),'Verifying USB before original cleanup…')
    def finish_cleanup(self):self.last_history=None;self.game_detail.set('Original cleanup complete; destination files retained.')
    def settings(self):
        if self.engine.active_id or self.busy:messagebox.showinfo('File operation running','Pause and finish the current import before changing Settings.',parent=self.root);return
        self.cancel_library();Settings(self)
    def render_history(self,snapshot):
        signature=tuple((j['id'],j['state'],j.get('source_retained'),j.get('hidden')) for j in snapshot['jobs'])+tuple(str(e) for e in self.engine.history_events)
        if signature==self.last_history:return
        self.last_history=signature;selected=self.history.selection();self.history.delete(*self.history.get_children())
        for j in reversed(snapshot['jobs']):
            original='Deleted' if j.get('source_retained') is False or j.get('freed',0)>0 else 'Kept' if Path(j['source']).exists() else 'Unavailable'
            self.history.insert('','end',iid=j['id'],values=(date(j.get('finished_at',j['added'])),j['name'],j.get('format','ISO import (legacy)' if j['state']=='Complete' else 'Import'),j['state'],original),tags=(j['state'],))
        for i,e in enumerate(self.engine.history_events):self.history.insert('','end',iid='event-'+str(i),values=(date(e['at']),'; '.join(Path(p).name for p in e['paths']),e['action'],e['state'],'—'))
        for ident in selected:
            if self.history.exists(ident):self.history.selection_add(ident)
    def tick(self):
        try:self.update_ui()
        except Exception as e:
            self.engine.log('Interface update: '+str(e))
        finally:
            try:
                if self.root.winfo_exists():self.root.after(300,self.tick)
            except tk.TclError:pass
    def update_ui(self):
        while not self.callbacks.empty():
            fn,result,error=self.callbacks.get();self.busy=False;self.engine.end_maintenance()
            if isinstance(error,Cancelled):self.game_detail.set('Stopped. Remaining files were kept.')
            elif error:messagebox.showerror('Could not finish',str(error),parent=self.root);self.game_detail.set(str(error))
            elif fn:
                try:fn(result)
                except Exception as e:self.game_detail.set(str(e));self.engine.log('File operation display: '+str(e))
        self.finish_library()
        if self.cache_revision!=self.engine.cache_revision and not self.library_loading:self.load_cached_library()
        s=self.engine.snapshot();self.last_snapshot=s
        if self.closing:
            if self.busy or any(t.is_alive() for t in self.engine.threads) or (self.library_thread and self.library_thread.is_alive()):return
            self.root.destroy();return
        self.source_var.set(str(self.engine.base));self.auto_var.set(s['auto'])
        self.drive_var.set(s["destination"] + (" · "+size(s["free"])+" free" if s["free"] is not None else " · "+s["connection"]))
        ready=any(j['state']=='Ready' for j in s['jobs'])
        self.start.configure(state='disabled' if self.busy or (s['enabled'] and not ready and not any(j['state']=='Waiting for destination' for j in s['jobs'])) else 'normal',text='Retry now' if any(j['state']=='Waiting for destination' for j in s['jobs']) else 'Start queue')
        self.pause.configure(state='normal' if s['enabled'] else 'disabled');self.stop.configure(state='normal' if s['active'] or self.busy or any(j['state']=='Waiting for destination' for j in s['jobs']) else 'disabled')
        self.batch_combo.configure(state='disabled' if s['active'] or self.busy else 'readonly')
        selected=set(self.tree.selection());retryable=any(j['id'] in selected and j['state'] in {'Error','Needs review','Interrupted','Missing','Duplicate','Waiting for destination'} and Path(j['source']).exists() for j in s['jobs'])
        self.retry_button.configure(state='normal' if retryable else 'disabled')
        self.status_var.set(self.operation_message if self.busy else ('Pausing after this file…' if s['active'] and not s['enabled'] else s['blocked']))
        current=set()
        for j in s['jobs']:
            if j.get('hidden'):continue
            ident=j['id'];current.add(ident);progress=f"{j['percent']:.0f}%" if j['state'] in ACTIVE|{'Complete'} else '—'
            values=(j['name'],size(j['size']),j['state'],progress)
            if ident not in self.rows:self.tree.insert('','end',iid=ident,values=values,tags=(j['state'],))
            elif self.rows[ident]!=values:self.tree.item(ident,values=values,tags=(j['state'],))
            self.rows[ident]=values
        for ident in set(self.rows)-current:self.tree.delete(ident);del self.rows[ident]
        active=next((j for j in s['jobs'] if j['id']==s['active']),None)
        if active:
            self.bar['value']=active['percent'];elapsed=time.time()-active.get('phase_since',time.time())
            amount=active.get('detail','') if active['state']=='Checking' else f"{size(active.get('done',0))} / {size(active.get('total',0))}"
            self.progress_var.set(f"{active['name']} · {active['state']} {active['percent']:.0f}% · {duration(elapsed)} · {amount}")
            if not self.tree.selection():self.detail_var.set(active['detail'])
        else:
            self.bar['value']=0;self.progress_var.set(self.operation_message if self.busy else ('Waiting for new files.' if s['enabled'] else 'Ready when you are. Click Start queue.'))
        self.render_history(s)
        visible=[j for j in s['jobs'] if not j.get('hidden')]
        self.footer_var.set(f"{len(visible)} queue entries · {sum(j['state']=='Complete' for j in visible)} complete · {sum(j['state'] in {'Error','Needs review','Interrupted'} for j in visible)} need attention · {size(sum(j.get('freed',0) for j in s['jobs']))} freed · {len(s['jobs'])} history entries")
        changed=False
        while not self.engine.events.empty():
            stamp,msg=self.engine.events.get();self.log.configure(state='normal');self.log.insert('end',stamp+'  '+msg+'\n');changed=True
            if int(self.log.index('end-1c').split('.')[0])>150:self.log.delete('1.0','2.0')
        if changed:self.log.see('end');self.log.configure(state='disabled')
        pending=[j['id'] for j in s['jobs'] if j.get('cleanup_pending') and j['id'] not in self.asked and j['state']=='Complete']
        if pending and not s['active'] and not self.busy and not any(j['state'] in ACTIVE|{'Queued','Waiting'} for j in s['jobs']):
            self.asked.update(pending);self.root.after(100,lambda:self.cleanup_selected(pending))
    def close(self):
        if self.busy:
            self.engine.management_cancel.set()
            self.closing=True;self.cancel_library();self.engine.close();return
        if self.engine.active_id and not messagebox.askyesno('Stop and close?','Stop the current import? Original files will be retained.',parent=self.root):return
        self.closing=True;self.cancel_library();self.engine.close()

from settings_ui import Settings

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default=str(default_base()),help='Local input, settings and history folder')
    parser.add_argument('--seven-zip',help='Absolute path to a trusted 7z.exe installation')
    parser.add_argument('--start-queue',action='store_true',help='Start queued work after opening the app')
    parser.add_argument('--retry-interrupted',action='store_true',help='Retry interrupted jobs whose original inputs remain')
    args=parser.parse_args()
    if os.name != 'nt':parser.error('This app currently supports Windows only')
    try:tool=find_7zip(args.seven_zip)
    except (ValueError,FileNotFoundError) as exc:
        ctypes.windll.user32.MessageBoxW(None,str(exc),'7-Zip required',0x10)
        return 1
    try:instance=NamedLock(r'Local\Xbox360ConverterApp')
    except BlockingIOError:ctypes.windll.user32.MessageBoxW(None,'Xbox 360 Game Manager is already open.','Xbox 360 Game Manager',0x40);return
    try:
        # Let interactive applications take priority during disk-heavy imports.
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),0x4000)
        try:ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:pass
        engine=Engine(Path(args.base),tool)
        if args.retry_interrupted:engine.retry([j['id'] for j in engine.jobs if j['state']=='Interrupted'])
        root=tk.Tk();app=App(root,engine)
        if args.start_queue:root.after(0,engine.enable)
        root.mainloop()
    except Exception:ctypes.windll.user32.MessageBoxW(None,traceback.format_exc()[-3500:],'Could not start Xbox 360 Game Manager',0x10);raise
    finally:instance.close()
if __name__=='__main__':sys.exit(main())
