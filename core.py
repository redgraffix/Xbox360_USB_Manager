"""Bounded Xbox 360 filesystem extraction and byte-for-byte verification."""
from __future__ import annotations
import contextlib, ctypes, hashlib, json, os, re, shutil, stat, struct, subprocess, threading, time
from pathlib import Path, PurePosixPath

CHUNK = 4 * 1024 * 1024
GIB = 1024 ** 3
MAGIC = b'MICROSOFT*XBOX*MEDIA'
MUTEX_NAME = r'Local\CodexXbox360IsoConversion'
RESERVED = {'CON','PRN','AUX','NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))}

class Cancelled(Exception): pass
class DriveMissing(Exception): pass
class Duplicate(Exception): pass


def safe_path(path, root):
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if path != root and not path.is_relative_to(root):
        raise ValueError('Path is outside the allowed folder: ' + str(path))
    for p in (path, *path.parents):
        try: s = p.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked folders are not supported: ' + str(p))
    if path.resolve() != path:
        raise ValueError('Unexpected resolved path: ' + str(path))
    return path


def checked_entries(parent):
    """Use directory enumeration metadata (cached on Windows), rejecting links."""
    with os.scandir(parent) as entries:
        for entry in entries:
            s = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked folders are not supported: ' + entry.path)
            yield Path(entry.path), s


def walk_files(root, checkpoint=lambda: None):
    """Validate directories once, rather than resolving every file and ancestor."""
    root = safe_path(root, root); pending = [root]
    while pending:
        checkpoint(); parent = pending.pop(); safe_path(parent, root)
        for path, s in checked_entries(parent):
            checkpoint()
            if stat.S_ISDIR(s.st_mode): pending.append(path)
            elif stat.S_ISREG(s.st_mode): yield path, s
            else: raise ValueError('Unsupported file type: ' + str(path))


def game_executables(root, checkpoint=lambda: None):
    """Find game roots without descending into each game's thousands of assets."""
    root = safe_path(root, root); pending = [root]
    while pending:
        checkpoint(); parent = pending.pop(); safe_path(parent, root)
        children = list(checked_entries(parent))
        exe = next((p for p, s in children if p.name.lower() == 'default.xex' and stat.S_ISREG(s.st_mode)), None)
        if exe is not None: yield exe
        else: pending.extend(p for p, s in children if stat.S_ISDIR(s.st_mode))


class StageWriter:
    """Exclusive writes into an owned stage; validate/create each parent once."""
    def __init__(self, root):
        self.root = safe_path(root, root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.parents = {self.root}

    def target(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or not relative.parts: raise ValueError('Invalid stage path')
        for part in relative.parts: valid_name(part)
        target = self.root / relative
        parent = target.parent
        if parent not in self.parents:
            safe_path(parent, self.root); parent.mkdir(parents=True, exist_ok=True)
            self.parents.add(parent)
        # Callers use 'xb', so existing files or links are never overwritten.
        return target


def safe_remove_tree(path, root, checkpoint=lambda: None):
    path = safe_path(path, root)
    if path == Path(root).absolute(): raise ValueError('Cannot remove the work root')
    if not path.exists(): return
    def remove(parent):
        checkpoint(); safe_path(parent, root)
        for child, s in checked_entries(parent):
            checkpoint()
            if stat.S_ISDIR(s.st_mode): remove(child)
            elif stat.S_ISREG(s.st_mode): child.unlink()
            else: raise ValueError('Unsupported file type: ' + str(child))
        parent.rmdir()
    remove(path)


def signature(path):
    s = Path(path).stat()
    return [s.st_size, s.st_mtime_ns, s.st_ino]


def valid_name(name):
    if (not name or name in ('.','..') or name.endswith((' ','.')) or
        any(ord(c)<32 or c in '/\\:<>"|?*' for c in name) or
        name.split('.')[0].upper() in RESERVED):
        raise ValueError('Unsupported or unsafe filename: ' + repr(name))
    return name


def game_relative(filename):
    stem = Path(filename.replace('\\','/')).stem
    disc = re.search(r'\(disc\s*(\d+)(?:[^)]*)\)', stem, re.I)
    title = stem.split(' (', 1)[0].strip().rstrip('.')
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', title).strip()[:42].rstrip('. ')
    valid_name(title)
    return Path(title) / ('Disc ' + disc.group(1)) if disc else Path(title)


@contextlib.contextmanager
def locked_reader(path):
    """Permit other readers; deny writers/deletion while work is in progress."""
    if os.name != 'nt':
        with open(path, 'rb') as stream: yield stream
        return
    import msvcrt
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    k.CreateFileW.restype = ctypes.c_void_p
    handle = k.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    with os.fdopen(fd, 'rb') as stream: yield stream


class NamedLock:
    def __init__(self, name=MUTEX_NAME):
        self.handle = None
        if os.name != 'nt': return
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k.CreateMutexW(None, False, name)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            k.CloseHandle(handle)
            raise BlockingIOError('Previous conversion batch is still running')
        self.handle, self.kernel = handle, k
    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle = None
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def batch_running():
    if os.name != 'nt': return False
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k.OpenMutexW.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenMutexW(0x100000, False, MUTEX_NAME)
    if h: k.CloseHandle(h)
    return bool(h)


def manifest(image):
    image.seek(0, 2); image_size = image.tell()
    entries, dirs, names, directory_names = [], set(), set(), set()
    def read_at(pos, n):
        if pos < 0 or n < 0 or pos+n > image_size: raise ValueError('ISO data extends beyond the image')
        image.seek(pos); data = image.read(n)
        if len(data) != n: raise ValueError('Incomplete ISO read')
        return data
    base = None
    for offset in (0, 0xFD90000, 0x2080000, 0x18300000):
        if offset+0x10000+28 <= image_size and read_at(offset+0x10000,20) == MAGIC:
            base = offset; break
    if base is None: raise ValueError('Not a supported Xbox ISO: Xbox filesystem header not found')
    root_sector, root_size = struct.unpack('<II', read_at(base+0x10000+20,8))
    def directory(sec, length, prefix, depth):
        if depth > 40: raise ValueError('ISO directory nesting is too deep')
        if not length: return
        if length > 64*1024*1024 or (sec,length) in dirs: raise ValueError('Invalid or looping ISO directory')
        dirs.add((sec,length)); data = read_at(base+sec*2048,length)
        nodes, pending = set(), [0]
        while pending:
            pos = pending.pop()
            if pos in nodes or pos+14 > len(data): raise ValueError('Invalid ISO directory entry')
            nodes.add(pos)
            left,right,start,n,attrs,namelen = struct.unpack_from('<HHIIBB',data,pos)
            if not namelen or pos+14+namelen > len(data): raise ValueError('Invalid ISO filename length')
            name = valid_name(data[pos+14:pos+14+namelen].decode('ascii'))
            rel = prefix/name; key = rel.as_posix().lower()
            if key in names or key in directory_names: raise ValueError('Duplicate ISO path: '+str(rel))
            if attrs & 16:
                directory_names.add(key); directory(start,n,rel,depth+1)
            else:
                if n > 0xFFFFFFFF: raise ValueError('Game file exceeds FAT32 size limit')
                if base+start*2048+n > image_size: raise ValueError('Game data extends beyond the ISO')
                names.add(key); entries.append((rel,base+start*2048,n))
                if len(entries)>250000: raise ValueError('Too many ISO entries')
            if right: pending.append(right*4)
            if left: pending.append(left*4)
    directory(root_sector,root_size,Path(),0)
    if 'default.xex' not in names: raise ValueError('No root default.xex; this is not a runnable Xbox 360 game image')
    return sorted(entries, key=lambda item: item[0].as_posix().lower())


def actual_files(dest, checkpoint=lambda: None, callback=None):
    dest = Path(dest)
    found = {}
    for p, s in walk_files(dest, checkpoint):
        key = p.relative_to(dest).as_posix().lower()
        if key in found: raise ValueError('Duplicate file path: ' + str(p))
        found[key] = s.st_size
        if callback: callback(len(found))
    return found


def identity(entries, executable_bytes):
    # Candidate identity, not proof of equality: source cleanup always requires full verification.
    layout = [(p.as_posix().lower(),n) for p,_,n in entries]
    payload = json.dumps(sorted(layout),separators=(',',':')).encode()
    return hashlib.sha256(payload + hashlib.sha256(executable_bytes).digest()).hexdigest()


def iso_identity(image, entries):
    rel,offset,n = next(e for e in entries if e[0].as_posix().lower() == 'default.xex')
    if n > 128*1024*1024: raise ValueError('Unusually large default.xex')
    image.seek(offset); return identity(entries,image.read(n))


def folder_identity(folder, checkpoint=lambda: None):
    layout = actual_files(folder, checkpoint)
    executable = next((p for p in folder.iterdir() if p.name.lower() == 'default.xex'), None)
    if executable is None or executable.stat().st_size>128*1024*1024: return None
    return identity([(Path(p),0,n) for p,n in layout.items()],executable.read_bytes())


def extract_and_verify(image, entries, stage, callback, checkpoint):
    total = sum(n for _,_,n in entries)
    if stage.exists():
        # Interrupted conversions are reusable only after comparison to the ISO.
        callback('Checking', 0, len(entries), 'Checking interrupted conversion')
        expected = {p.as_posix().lower(): n for p, _, n in entries}
        if actual_files(stage, checkpoint) == expected:
            return verify(image, entries, stage, callback, checkpoint)
        safe_remove_tree(stage, stage.parent, checkpoint)
    stage.mkdir(parents=True)
    writer = StageWriter(stage)
    done = 0
    for index,(rel,offset,size) in enumerate(entries,1):
        checkpoint()
        target = writer.target(rel)
        image.seek(offset)
        with target.open('xb') as output:
            remaining = size
            while remaining:
                checkpoint(); count = min(CHUNK,remaining); data = image.read(count)
                if len(data) != count: raise ValueError('Incomplete ISO data: '+str(rel))
                output.write(data); remaining -= count; done += count
                callback('Converting',done,total,f'{index:,} / {len(entries):,} files · {rel.name}')
            output.flush(); os.fsync(output.fileno())
    return verify(image,entries,stage,callback,checkpoint)


def verify(image,entries,dest,callback,checkpoint):
    expected = {p.as_posix().lower():n for p,_,n in entries}
    def inventory():
        callback('Checking', 0, len(entries), 'Checking USB file list')
        found = {}
        for p, s in walk_files(dest, checkpoint):
            key = p.relative_to(dest).as_posix().lower()
            if key in found: raise ValueError('Duplicate file path: ' + str(p))
            found[key] = (s.st_size, s.st_mtime_ns)
            callback('Checking', len(found), len(entries), f'Checking USB file list: {len(found):,} / {len(entries):,} files')
        return found
    before = inventory()
    if {p: s[0] for p, s in before.items()} != expected: raise ValueError('Verification failed: file inventory or sizes differ')
    total = sum(expected.values()); done = 0; hashes = {}
    for index,(rel,offset,size) in enumerate(entries,1):
        checkpoint(); image.seek(offset)
        sha = hashlib.sha256()
        with locked_reader(dest/rel) as actual:
            remaining = size
            while remaining:
                checkpoint(); count = min(CHUNK,remaining)
                a,b = image.read(count),actual.read(count)
                if len(a)!=count or a!=b: raise ValueError('Verification failed: data mismatch in '+str(rel))
                sha.update(b)
                remaining-=count; done+=count
                callback('Verifying',done,total,f'{index:,} / {len(entries):,} files · {rel.name}')
            if actual.read(1): raise ValueError('Verification failed: file size changed in '+str(rel))
        hashes[rel.as_posix()] = {'size': size, 'sha256': sha.hexdigest(), 'mtime_ns': before[rel.as_posix().lower()][1]}
    if inventory() != before: raise ValueError('Game files changed during verification')
    return hashes


def archive_members(tool, archive):
    args = [str(tool),'l','-slt','-ba','-sccUTF-8','--',str(archive)]
    result = subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode: raise ValueError('Cannot read archive: '+result.stderr.decode('utf-8','replace')[-1500:])
    records = []; current = {}
    for line in result.stdout.decode('utf-8','replace').splitlines()+['']:
        if not line.strip():
            if 'Path' in current: records.append(current)
            current = {}; continue
        if ' = ' in line:
            key,value = line.split(' = ',1); current[key] = value
    members=[]; destinations=set()
    for record in records:
        name = record['Path'].replace('\\','/')
        if not name.lower().endswith('.iso'): continue
        parts = PurePosixPath(name).parts
        if name.startswith('/') or any(p in ('.','..') for p in parts): raise ValueError('Unsafe archive member path')
        for part in parts: valid_name(part)
        if record.get('Encrypted')=='+' or record.get('Symbolic Link') or record.get('Hard Link'):
            raise ValueError('Encrypted or linked archive members are not supported')
        size = int(record.get('Size','0'))
        if size <= 0: raise ValueError('Empty ISO inside archive')
        target = game_relative(name)
        if str(target).lower() in destinations: raise Duplicate('Two ISO images in this archive map to the same game folder. Input retained.')
        destinations.add(str(target).lower()); members.append((record['Path'],size,target))
    if not members: raise ValueError('No ISO files found in this archive')
    return members


def unpack_member(tool,archive,member,size,folder,callback,checkpoint):
    folder.mkdir(parents=True,exist_ok=True)
    args=[str(tool),'e','-y','-aoa','-spd','-bsp1','-bso0','-bse1','-sccUTF-8','-o'+str(folder),'--',str(archive),member]
    proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0))
    tail=bytearray(); progress=[0]
    def read_output():
        pending=b''
        while True:
            data=proc.stdout.read(1)
            if not data: break
            tail.extend(data)
            if len(tail)>8192: del tail[:-8192]
            pending+=data
            if data in (b'\r',b'\n',b'\b') or len(pending)>200:
                for match in re.finditer(rb'(\d{1,3})%',pending): progress[0]=min(100,int(match[1]))
                pending=b''
    reader=threading.Thread(target=read_output,daemon=True); reader.start()
    try:
        while proc.poll() is None:
            checkpoint(); callback('Unpacking',progress[0],100,Path(member).name)
            time.sleep(.2)
        reader.join(timeout=3)
        if proc.returncode: raise ValueError('Archive extraction failed: '+tail.decode('utf-8','replace')[-1800:])
    finally:
        if proc.poll() is None: proc.terminate(); proc.wait(timeout=10)
        reader.join(timeout=3); proc.stdout.close()
    iso=safe_path(folder/Path(member.replace('\\','/')).name,folder)
    if not iso.is_file() or iso.stat().st_size!=size: raise ValueError('Unpacked ISO is missing or incomplete')
    return iso
