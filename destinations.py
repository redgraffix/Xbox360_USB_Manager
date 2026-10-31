"""Destination I/O. Preparation never uses these locations as scratch space."""
from __future__ import annotations
import contextlib
import ftplib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import time
from core import CHUNK, DriveMissing, safe_path, valid_name, checked_entries, locked_reader
from content import SelectedDrive
import credentials


class DestinationUnavailable(DriveMissing, OSError): pass
class DestinationConflict(ValueError): pass


def relative(raw):
    text = str(raw).replace('\\', '/')
    if text in ('', '.'): return ''
    if text.startswith('/') or any(p in ('', '.', '..') for p in text.split('/')):
        raise ValueError('Invalid destination-relative path')
    for p in text.split('/'): valid_name(p)
    return text


def normalize(config):
    c = dict(config); kind = c.get('kind', 'folder')
    if kind == 'folder':
        p = Path(c.get('path', '')).expanduser()
        if not p.is_absolute(): raise ValueError('Select an absolute output folder')
        safe_path(p, p)
        if p == Path(p.anchor): raise ValueError('Select an output folder, not the whole disk')
        return dict(kind=kind, path=str(p.absolute()))
    if kind == 'usb':
        if not isinstance(c.get('volume'), dict): raise ValueError('Choose a USB drive')
        return dict(kind=kind, volume=c['volume'])
    if kind == 'ftp':
        host = c.get('host', '').strip(); user = c.get('user', '').strip()
        if not host or any(x in host for x in ('/', '@', '\\', '\r', '\n')):
            raise ValueError('Enter the Xbox hostname or IP address, without a URL')
        if any(x in user for x in ('\r', '\n')): raise ValueError('Invalid FTP username')
        port = int(c.get('port', 21))
        if not 1 <= port <= 65535: raise ValueError('FTP port must be 1–65535')
        raw = c.get('root', '/').replace('\\', '/')
        path = '/' + relative(raw.strip('/'))
        return dict(kind=kind, host=host, port=port, user=user, root=path)
    raise ValueError('Unknown destination type')


def identity(config):
    return hashlib.sha256(json.dumps(normalize(config), sort_keys=True).encode()).hexdigest()


def describe(config):
    c = normalize(config)
    if c['kind'] == 'ftp': return f"FTP · {c['host']}:{c['port']}{c['root']}"
    if c['kind'] == 'usb': return 'USB · ' + c['volume'].get('label', 'Selected drive')
    return c['path']


def location_label(config):
    c=normalize(config)
    if c['kind']=='usb':return 'PC USB'
    if c['kind']=='folder':return 'Local folder'
    first=c['root'].strip('/').split('/')[0].casefold()
    if first.startswith('usb'):return 'Xbox USB'
    if first.startswith('hdd'):return 'Xbox HDD'
    return 'Xbox FTP'


class FileSession:
    def __init__(self, root, drive=None):
        self.root = Path(root); self.drive = drive; self.last_check = 0
        self.check()
    def check(self):
        if self.drive: self.drive.check(self.root)
        if not self.root.is_dir(): raise DestinationUnavailable('Output folder is unavailable')
    def path(self, rel):
        if time.monotonic()-self.last_check > 1: self.check(); self.last_check=time.monotonic()
        return safe_path(self.root / relative(rel), self.root)
    def list(self, rel=''):
        p=self.path(rel)
        if not p.exists(): return []
        import stat
        return [dict(name=f.name, directory=stat.S_ISDIR(s.st_mode), size=s.st_size,
                     modified=str(s.st_mtime_ns)) for f,s in checked_entries(p)]
    def stat(self, rel):
        p=self.path(rel)
        if not p.exists(): return None
        s=p.stat();return dict(directory=p.is_dir(),size=s.st_size,modified=str(s.st_mtime_ns))
    def prefix(self, rel, limit):
        with self.path(rel).open('rb') as f:return f.read(limit)
    def digest(self, rel, checkpoint=lambda:None, progress=lambda n:None):
        sha=hashlib.sha256();n=0
        with locked_reader(self.path(rel)) as f:
            while data:=f.read(CHUNK):
                checkpoint();sha.update(data);n+=len(data);progress(len(data))
        return dict(size=n,sha256=sha.hexdigest())
    def mkdir(self, rel):self.path(rel).mkdir(parents=True,exist_ok=True)
    def put(self, rel, source, checkpoint, progress):
        self.mkdir(str(PurePosixPath(relative(rel)).parent))
        with locked_reader(source) as src, self.path(rel).open('wb') as dst:
            while data:=src.read(CHUNK):checkpoint();dst.write(data);progress(len(data))
            dst.flush();os.fsync(dst.fileno())
    def rename(self, old, new):
        if self.stat(new): raise DestinationConflict('Destination changed; existing file retained')
        self.path(old).rename(self.path(new))
    def remove(self, rel, directory=False):
        if not relative(rel):raise ValueError('Cannot remove destination root')
        p=self.path(rel)
        p.rmdir() if directory else p.unlink()
    def free(self):return shutil.disk_usage(self.root).free


class FTPSession:
    def __init__(self, config, secret):
        self.config=normalize(config);self.secret=secret;self.ftp=self.connect();self.created=set();self.mlsd_supported=True
        try:self.ftp.cwd(self.config['root'])
        except Exception:self.ftp.close();raise
    def connect(self):
        f=ftplib.FTP(timeout=12,encoding='utf-8')
        try:
            f.connect(self.config['host'],self.config['port']);f.login(self.config['user'],self.secret)
            f.set_pasv(True);return f
        except Exception:f.close();raise
    def path(self, rel):
        r=relative(rel);return self.config['root'].rstrip('/') + ('/'+r if r else '') or '/'
    def check(self):self.ftp.voidcmd('NOOP');self.ftp.cwd(self.config['root'])
    def list(self, rel=''):
        path=self.path(rel)
        try:
            if not self.mlsd_supported:raise ftplib.error_perm('502 MLSD unavailable')
            rows=list(self.ftp.mlsd(path, facts=['type','size','modify']))
            result=[]
            for name,info in rows:
                if info.get('type') in ('cdir','pdir') or name in ('.','..'):continue
                valid_name(name)
                if info.get('type') not in ('file','dir'):raise ValueError('Unsupported remote entry type')
                result.append(dict(name=name,directory=info['type']=='dir',size=int(info.get('size',0)),modified=info.get('modify','')))
            return result
        except ftplib.error_perm as e:
            if not str(e).startswith(('500','501','502','504')):raise
            self.mlsd_supported=False
        # Aurora commonly implements LIST rather than MLSD.
        lines=[];old=self.ftp.pwd()
        try:
            self.ftp.cwd(path);self.ftp.retrlines('LIST',lines.append)
        finally:self.ftp.cwd(old)
        result=[]
        for line in lines:
            if not line or line.startswith('total '):continue
            unix=re.match(r'^([d-])[rwxstST-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\S+\s+\S+\s+\S+)\s+(.+)$',line)
            dos=re.match(r'^(\d{2}-\d{2}-\d{2,4})\s+(\S+)\s+(<DIR>|\d+)\s+(.+)$',line)
            if unix:
                kind,n,stamp,name=unix.groups();directory=kind=='d'
            elif dos:
                day,hour,n,name=dos.groups();directory=n=='<DIR>';stamp=day+' '+hour;n=0 if directory else n
            else:raise ValueError('Unsupported FTP directory listing')
            if name in ('.','..'):continue
            valid_name(name);result.append(dict(name=name,directory=directory,size=int(n),modified=stamp))
        return result
    def stat(self, rel):
        r=relative(rel)
        if not r:return dict(directory=True,size=0,modified='')
        p=PurePosixPath(r)
        try:return next((x for x in self.list(str(p.parent)) if x['name'].casefold()==p.name.casefold()),None)
        except ftplib.error_perm as e:
            if str(e).startswith('550'):return None
            raise
    def prefix(self, rel, limit):
        # A separate connection allows bounded metadata reads without leaving a
        # cancelled RETR response in the transfer session's control channel.
        f=self.connect()
        try:
            f.voidcmd('TYPE I');data=bytearray()
            with f.transfercmd('RETR '+self.path(rel)) as sock:
                while len(data)<limit:
                    block=sock.recv(min(65536,limit-len(data)))
                    if not block:break
                    data.extend(block)
            return bytes(data)
        finally:f.close()
    def digest(self, rel, checkpoint=lambda:None, progress=lambda n:None):
        sha=hashlib.sha256();amount=0
        def receive(block):
            nonlocal amount
            checkpoint();sha.update(block);amount+=len(block);progress(len(block))
        self.ftp.retrbinary('RETR '+self.path(rel),receive,blocksize=256*1024)
        return dict(size=amount,sha256=sha.hexdigest())
    def mkdir(self, rel):
        built=''
        for part in PurePosixPath(relative(rel)).parts:
            built=(built+'/' if built else '')+part
            if built in self.created:continue
            try:self.ftp.mkd(self.path(built))
            except ftplib.error_perm:
                old=self.ftp.pwd()
                try:self.ftp.cwd(self.path(built))
                finally:self.ftp.cwd(old)
            self.created.add(built)
    def put(self, rel, source, checkpoint, progress):
        self.mkdir(str(PurePosixPath(relative(rel)).parent))
        def sent(block):checkpoint();progress(len(block))
        with locked_reader(source) as f:self.ftp.storbinary('STOR '+self.path(rel),f,blocksize=256*1024,callback=sent)
    def rename(self, old, new):
        if self.stat(new):raise DestinationConflict('Remote file appeared during transfer; original retained')
        self.ftp.rename(self.path(old),self.path(new))
    def remove(self, rel, directory=False):
        if not relative(rel):raise ValueError('Cannot remove destination root')
        self.ftp.rmd(self.path(rel)) if directory else self.ftp.delete(self.path(rel))
    def free(self):return None


@contextlib.contextmanager
def connect(config, drive=None, secret=None):
    c=normalize(config);session=None
    try:
        if drive:session=FileSession(drive.locate(),drive)
        elif c['kind']=='folder':session=FileSession(c['path'])
        elif c['kind']=='usb':
            selected=SelectedDrive(c['volume']);session=FileSession(selected.locate(),selected)
        else:
            value=secret if secret is not None else credentials.read(c)
            if value is None:raise DestinationUnavailable('Enter the FTP password in Settings')
            session=FTPSession(c,value)
        yield session
    except (ftplib.Error, socket.timeout, ConnectionError, EOFError, DriveMissing) as e:
        # Never include login commands, passwords, or a URL with credentials.
        raise DestinationUnavailable('Destination unavailable; check the connection, login and selected folder') from e
    finally:
        if isinstance(session,FTPSession):session.ftp.close()


def walk(session, rel='', checkpoint=lambda:None, directories=None):
    pending=[relative(rel)];seen=set();count=0
    while pending:
        checkpoint();parent=pending.pop()
        if len(PurePosixPath(parent).parts)>40:raise ValueError('Destination nesting limit exceeded')
        for entry in session.list(parent):
            checkpoint();name=parent+'/'+entry['name'] if parent else entry['name'];key=name.casefold()
            if key in seen:raise ValueError('Duplicate destination entry')
            seen.add(key);count+=1
            if count>250000:raise ValueError('Destination file limit exceeded')
            if entry['directory']:
                if directories is not None:directories.add(name)
                pending.append(name)
            else:yield name,entry
