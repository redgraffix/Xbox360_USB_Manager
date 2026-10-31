"""Destination setup and asynchronous FTP browsing; no network work on Tk's thread."""
import queue
import threading
from pathlib import Path, PurePosixPath
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from theme import FG, PANEL, GREEN, BG
from content import volumes
from destinations import connect, normalize
import credentials

POLICIES={'Keep source files':'keep','Delete source files':'delete','Ask before deleting':'ask'}
MODES={'Local folder':'folder','USB drive':'usb','Xbox FTP':'ftp'}


class Settings:
    def __init__(self,app):
        self.app=app;self.engine=app.engine;self.engine.pause();self.results=queue.Queue();self.busy=False
        self.win=tk.Toplevel(app.root);self.win.title('Settings');self.win.geometry('790x790');self.win.minsize(760,760);self.win.transient(app.root)
        footer=ttk.Frame(self.win,padding=(20,10,20,16));footer.pack(side='bottom',fill='x')
        self.status=tk.StringVar(value='Choose where prepared games will be saved.')
        ttk.Label(footer,textvariable=self.status,wraplength=710,style='Muted.TLabel').pack(anchor='w',pady=(0,10))
        buttons=ttk.Frame(footer);buttons.pack(anchor='e')
        ttk.Button(buttons,text='Cancel',command=self.win.destroy).pack(side='left',padx=8)
        self.save_button=ttk.Button(buttons,text='Save settings',command=self.save,style='Accent.TButton');self.save_button.pack(side='left')
        viewport=ttk.Frame(self.win);viewport.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(viewport,bg=BG,highlightthickness=0)
        scrollbar=ttk.Scrollbar(viewport,orient='vertical',command=self.canvas.yview);scrollbar.pack(side='right',fill='y')
        self.canvas.configure(yscrollcommand=scrollbar.set);self.canvas.pack(side='left',fill='both',expand=True)
        self.box=ttk.Frame(self.canvas,padding=20);window=self.canvas.create_window((0,0),window=self.box,anchor='nw');self.box.columnconfigure(1,weight=1)
        self.canvas.bind('<Configure>',lambda ev:self.canvas.itemconfigure(window,width=ev.width))
        self.box.bind('<Configure>',lambda _:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.win.bind('<MouseWheel>',lambda ev:self.canvas.yview_scroll(-int(ev.delta/120),'units'))
        e=self.engine;d=e.settings['destination'];box=self.box
        ttk.Label(box,text='Folders & destination',font=('Segoe UI Semibold',17),foreground=GREEN).grid(row=0,column=0,columnspan=3,sticky='w',pady=(0,14))
        self.input=tk.StringVar(value=str(e.base));self.temp=tk.StringVar(value=str(e.work));self.games=tk.StringVar(value=e.settings['games'])
        for row,label,var in ((1,'Input folder',self.input),(2,'Working folder',self.temp)):
            ttk.Label(box,text=label).grid(row=row,column=0,sticky='w',padx=(0,15),pady=7)
            ttk.Entry(box,textvariable=var).grid(row=row,column=1,sticky='ew')
            ttk.Button(box,text='Browse…',command=lambda v=var:self.browse(v)).grid(row=row,column=2,padx=(8,0))
        ttk.Label(box,text='All unpacking and preparation uses this local working folder.',style='Muted.TLabel').grid(row=3,column=0,columnspan=3,sticky='w',pady=(4,12))
        self.mode=tk.StringVar(value=next(k for k,v in MODES.items() if v==d['kind']))
        ttk.Label(box,text='Destination').grid(row=4,column=0,sticky='w')
        cb=ttk.Combobox(box,textvariable=self.mode,values=list(MODES),state='readonly');cb.grid(row=4,column=1,columnspan=2,sticky='ew');cb.bind('<<ComboboxSelected>>',lambda _:self.switch())
        self.dest_box=ttk.Frame(box);self.dest_box.grid(row=5,column=0,columnspan=3,sticky='nsew',pady=12)
        self.path=tk.StringVar(value=d.get('path',str(e.home/'Ready')))
        self.drives=volumes();self.drive_text=tk.StringVar();self.drive_box=None
        self.host=tk.StringVar(value=d.get('host',''));self.port=tk.StringVar(value=str(d.get('port',21)))
        self.user=tk.StringVar(value=d.get('user','xboxftp'));self.password=tk.StringVar();self.remote=tk.StringVar(value=d.get('root','/'))
        self.switch()
        ttk.Label(box,text='Games subfolder').grid(row=6,column=0,sticky='w',pady=7)
        ttk.Entry(box,textvariable=self.games).grid(row=6,column=1,columnspan=2,sticky='ew')
        ttk.Label(box,text='DLC, GOD and updates use the Content folder beside Games.',style='Muted.TLabel').grid(row=7,column=0,columnspan=3,sticky='w',pady=(3,12))
        self.policy=tk.StringVar(value=next(k for k,v in POLICIES.items() if v==e.settings['cleanup']))
        ttk.Label(box,text='After verification').grid(row=8,column=0,sticky='w')
        ttk.Combobox(box,textvariable=self.policy,values=list(POLICIES),state='readonly').grid(row=8,column=1,columnspan=2,sticky='ew')
        self.auto=tk.BooleanVar(value=e.auto)
        ttk.Checkbutton(box,text='Add completed input files automatically',variable=self.auto).grid(row=9,column=0,columnspan=3,sticky='w',pady=10)
        ttk.Label(box,text='Temporary work is cleaned after completion. Interrupted transfers retain their prepared files until resumed or cleaned.',wraplength=710,style='Muted.TLabel').grid(row=10,column=0,columnspan=3,sticky='w',pady=(0,10))
        ttk.Button(box,text='Clean temporary files…',command=self.cleanup).grid(row=11,column=0,columnspan=3,sticky='w')
        self.win.after(100,self.poll)
    def browse(self,var):
        selected=filedialog.askdirectory(parent=self.win,initialdir=var.get() if Path(var.get()).is_dir() else None)
        if selected:var.set(selected)
    def switch(self):
        for child in self.dest_box.winfo_children():child.destroy()
        box=self.dest_box;box.columnconfigure(1,weight=1);mode=MODES[self.mode.get()]
        if mode=='folder':
            ttk.Label(box,text='Output folder').grid(row=0,column=0,padx=(0,15))
            ttk.Entry(box,textvariable=self.path).grid(row=0,column=1,sticky='ew')
            ttk.Button(box,text='Browse…',command=lambda:self.browse(self.path)).grid(row=0,column=2,padx=(8,0))
        elif mode=='usb':
            ttk.Label(box,text='USB drive').grid(row=0,column=0,padx=(0,15))
            self.drive_box=ttk.Combobox(box,textvariable=self.drive_text,state='readonly',values=[f"{v['root']} {v['label'] or 'Unnamed'} · {v['filesystem']}" for v in self.drives])
            self.drive_box.grid(row=0,column=1,columnspan=2,sticky='ew')
            old=self.engine.settings['destination'].get('volume')
            for i,v in enumerate(self.drives):
                if old and v['serial']==old['serial']:self.drive_box.current(i)
        else:
            for row,label,var,secret in ((0,'Xbox address',self.host,False),(1,'Port',self.port,False),(2,'Username',self.user,False),(3,'Password',self.password,True),(4,'Xbox folder',self.remote,False)):
                ttk.Label(box,text=label).grid(row=row,column=0,sticky='w',padx=(0,15),pady=4)
                ttk.Entry(box,textvariable=var,show='•' if secret else '').grid(row=row,column=1,columnspan=2,sticky='ew')
            row=ttk.Frame(box);row.grid(row=5,column=0,columnspan=3,sticky='w',pady=7)
            ttk.Button(row,text='Test connection',command=self.test).pack(side='left')
            ttk.Button(row,text='Browse Xbox…',command=self.browse_remote).pack(side='left',padx=8)
            ttk.Label(box,text='Blank password keeps the saved Windows credential. FTP is for your trusted local network.',style='Muted.TLabel',wraplength=700).grid(row=6,column=0,columnspan=3,sticky='w')
    def ftp_config(self,root=None):return normalize(dict(kind='ftp',host=self.host.get(),port=self.port.get(),user=self.user.get(),root=root if root is not None else self.remote.get()))
    def run(self,fn,done):
        if self.busy:return
        self.busy=True;self.status.set('Connecting…')
        def worker():
            try:self.results.put((done,fn(),None))
            except Exception as e:self.results.put((done,None,e))
        threading.Thread(target=worker,daemon=True).start()
    def poll(self):
        if not self.win.winfo_exists():return
        while not self.results.empty():
            done,result,error=self.results.get();self.busy=False
            if error:self.status.set(str(error))
            else:done(result)
        self.win.after(100,self.poll)
    def test(self):
        try:c=self.ftp_config();secret=self.password.get() or None
        except ValueError as e:self.status.set(str(e));return
        def check():
            with connect(c,secret=secret) as session:session.check();return len(session.list())
        self.run(check,lambda n:self.status.set(f'Connected. Selected folder contains {n} entries.'))
    def browse_remote(self):
        try:c=self.ftp_config('/');secret=self.password.get() or None
        except ValueError as e:self.status.set(str(e));return
        win=tk.Toplevel(self.win);win.title('Choose Xbox storage folder');win.geometry('620x440');box=ttk.Frame(win,padding=16);box.pack(fill='both',expand=True)
        path=tk.StringVar(value='/');ttk.Label(box,textvariable=path).pack(anchor='w',pady=(0,10))
        tree=ttk.Treeview(box,columns=('name',),show='headings');tree.heading('name',text='Folder');tree.pack(fill='both',expand=True)
        entries={}
        def load(current):
            if self.busy:return
            path.set(current)
            def fetch():
                with connect({**c,'root':current},secret=secret) as session:return [e['name'] for e in session.list() if e['directory']]
            def loaded(names):
                if not win.winfo_exists():return
                tree.delete(*tree.get_children());entries.clear()
                for i,name in enumerate(names):tree.insert('','end',iid=str(i),values=(name,));entries[str(i)]=name
                self.status.set('Choose the HDD, USB, or a folder within it.')
            self.run(fetch,loaded)
        def enter(_=None):
            selected=tree.selection()
            if selected:load(path.get().rstrip('/')+'/'+entries[selected[0]])
        def choose():
            if self.busy:return
            if path.get()=='/':self.status.set('Open Hdd1 or Usb0 before selecting.');return
            self.remote.set(path.get());win.destroy()
        tree.bind('<Double-1>',enter)
        row=ttk.Frame(box);row.pack(fill='x',pady=(10,0))
        ttk.Button(row,text='Up',command=lambda:load(str(PurePosixPath(path.get()).parent))).pack(side='left')
        ttk.Button(row,text='Open folder',command=enter).pack(side='left',padx=7)
        ttk.Button(row,text='Use this folder',command=choose).pack(side='right');load('/')
    def save(self):
        if self.busy:self.status.set('Wait for the connection check to finish.');return
        try:
            mode=MODES[self.mode.get()]
            if mode=='ftp':
                d=self.ftp_config()
                if self.password.get():credentials.save(d,self.password.get())
            elif mode=='usb':
                index=self.drive_box.current()
                if index<0:raise ValueError('Choose a connected USB drive')
                v=self.drives[index]
                if v['filesystem']!='FAT32':raise ValueError('Xbox USB output requires FAT32. The app does not format drives.')
                d=dict(kind='usb',volume=dict(serial=v['serial'],label=v['label']))
            else:d=dict(kind='folder',path=self.path.get())
            self.engine.configure(dict(input=self.input.get(),temp=self.temp.get(),destination=d,games=self.games.get(),cleanup=POLICIES[self.policy.get()],clean_temp=True,auto=self.auto.get()))
            self.app.batch_var.set(self.policy.get());self.engine.batch_policy='default';self.app.library_loaded=False;self.app.load_cached_library();self.win.destroy()
        except Exception as e:messagebox.showerror('Settings not saved',str(e),parent=self.win)
    def cleanup(self):
        try:
            candidates=self.engine.cleanup_candidates();total=sum(n for _,_,n in candidates)
            if not candidates:self.status.set('No leftover working files.');return
            if messagebox.askyesno('Clean working files?',f'Delete {total/1024**3:.2f} GB of app-owned working files? Interrupted jobs will need preparation again. Originals and destination files are kept.',parent=self.win):
                self.win.destroy();self.app.background(self.engine.clean_leftovers,lambda _:self.app.game_detail.set('Working files cleaned.'))
        except Exception as e:messagebox.showerror('Cleanup unavailable',str(e),parent=self.win)
