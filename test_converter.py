import io, json, os, struct, tempfile, time, unittest, uuid, zipfile
from pathlib import Path
from unittest.mock import patch
import core, engine

from runtime import find_7zip
TOOL=find_7zip()


def make_iso(path,files=None):
    files=files or {'default.xex':b'XEX2-test-executable','data/level.bin':bytes(range(256))*700}
    image=bytearray(1024*1024); image[0x10000:0x10000+20]=core.MAGIC
    next_dir=[40]; next_file=[100]
    def directory(tree):
        sector=next_dir[0]; next_dir[0]+=1; records=[]
        for name,value in sorted(tree.items()):
            if isinstance(value,dict): start,size=directory(value); attr=16
            else:
                start=next_file[0]; size=len(value); next_file[0]+=(size+2047)//2048+1; attr=0
                image[start*2048:start*2048+size]=value
            encoded=name.encode('ascii'); record=bytearray(struct.pack('<HHIIBB',0,0,start,size,attr,len(encoded))+encoded)
            record.extend(b'\0'*((-len(record))%4)); records.append(record)
        cursor=0
        for i,record in enumerate(records):
            if i+1<len(records): struct.pack_into('<H',record,2,(cursor+len(record))//4)
            image[sector*2048+cursor:sector*2048+cursor+len(record)]=record; cursor+=len(record)
        return sector,cursor
    tree={}
    for name,data in files.items():
        parent=tree
        parts=name.split('/')
        for part in parts[:-1]: parent=parent.setdefault(part,{})
        parent[parts[-1]]=data
    sector,length=directory(tree); struct.pack_into('<II',image,0x10000+20,sector,length)
    path.write_bytes(image)
    return files

class TestDrive:
    def __init__(self,root): self.root=root; root.mkdir()
    def locate(self): return self.root
    def check(self,root):
        if Path(root)!=self.root or not self.root.exists(): raise core.DriveMissing('Disconnected')

class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='xbox-test-'); self.root=Path(self.tmp.name)
        self.base=self.root/'input'; self.drive=TestDrive(self.root/'usb')
        self.eng=engine.Engine(self.base,TOOL,self.drive,settle_seconds=0)
        # Deletion tests explicitly opt in; fresh installations keep their sources.
        self.eng.options(delete=True)
    def tearDown(self):
        for h in list(self.eng.logger.handlers): h.close(); self.eng.logger.removeHandler(h)
        self.tmp.cleanup()
    def job(self,path):
        j={'id':uuid.uuid4().hex,'source':str(path),'name':path.name,'signature':core.signature(path),'size':path.stat().st_size,
           'state':'Queued','detail':'','percent':0,'added':time.time(),'outputs':[]}
        self.eng.jobs.append(j); return j
    def test_nested_iso_exact_output_and_source_deletion(self):
        iso=self.base/'Test Game (USA).iso'; expected=make_iso(iso); job=self.job(iso)
        self.eng.process(job)
        self.assertFalse(iso.exists()); self.assertEqual(job['state'],'Complete')
        for name,data in expected.items(): self.assertEqual((self.drive.root/'Games'/'Test Game'/name).read_bytes(),data)
    def test_source_kept_when_cleanup_disabled(self):
        iso=self.base/'Keep.iso'; make_iso(iso); self.eng.delete_sources=False; self.eng.process(self.job(iso)); self.assertTrue(iso.exists())
    def test_corrupt_output_fails_verification(self):
        iso=self.base/'Verify.iso'; make_iso(iso); stage=self.root/'verify'
        with iso.open('rb') as f:
            entries=core.manifest(f); core.extract_and_verify(f,entries,stage,lambda *a:None,lambda:None)
            data=stage/'data'/'level.bin'; data.write_bytes(b'bad'+data.read_bytes()[3:])
            with self.assertRaisesRegex(ValueError,'mismatch'): core.verify(f,entries,stage,lambda *a:None,lambda:None)
        self.assertTrue(iso.exists())
    def test_verification_failure_never_removes_source(self):
        iso=self.base/'Bad.iso'; make_iso(iso); job=self.job(iso)
        with patch('core.verify',side_effect=ValueError('bad verification')):
            with self.assertRaises(ValueError): self.eng.process(job)
        self.assertTrue(iso.exists()); self.assertFalse((self.drive.root/'Games'/'Bad').exists())
    def test_duplicate_existing_folder_no_overwrite(self):
        iso=self.base/'Duplicate.iso'; make_iso(iso); job=self.job(iso)
        dest=self.drive.root/'Games'/'Duplicate'; dest.mkdir(parents=True); (dest/'keep.txt').write_text('original')
        with self.assertRaises(engine.ContentConflict): self.eng.process(job)
        self.assertEqual((dest/'keep.txt').read_text(),'original'); self.assertTrue(iso.exists())
    def test_duplicate_content_with_different_filename(self):
        first=self.base/'Original.iso'; make_iso(first); self.eng.process(self.job(first))
        second=self.base/'Different Name.iso'; make_iso(second)
        with self.assertRaisesRegex(core.Duplicate,'content'): self.eng.process(self.job(second))
        self.assertTrue(second.exists()); self.assertFalse((self.drive.root/'Games'/'Different Name').exists())
    def test_duplicate_queued_archive_and_iso(self):
        make_iso(self.base/'Same Game (USA).iso'); (self.base/'Same Game (1).7z').write_bytes(b'test')
        self.eng.scan(); self.eng.scan()
        # Content detection, not matching outer archive names, decides duplicates.
        self.assertEqual(sorted(j['state'] for j in self.eng.jobs),['Queued','Queued'])
    def test_disc_destinations_stay_separate(self):
        self.assertNotEqual(core.game_relative('Game (Disc 1).iso'),core.game_relative('Game (Disc 2).iso'))
    def test_zip_unpack_verify_cleanup(self):
        iso=self.root/'Zip Game (USA).iso'; expected=make_iso(iso)
        archive=self.base/'Zip Game.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z: z.write(iso,'nested/'+iso.name); z.writestr('readme.txt','test')
        job=self.job(archive); self.eng.process(job)
        self.assertEqual(job['state'],'Complete'); self.assertFalse(archive.exists()); self.assertFalse(list(self.eng.work.iterdir()))
        self.assertEqual((self.drive.root/'Games'/'Zip Game'/'default.xex').read_bytes(),expected['default.xex'])
    def test_7z_unpack_verify_cleanup(self):
        import subprocess
        iso=self.root/'Seven Zip Game (USA).iso'; make_iso(iso)
        archive=self.base/'Seven Zip Game.7z'
        result=subprocess.run([str(TOOL),'a','-t7z','-mx=1',str(archive),str(iso)],stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0)
        job=self.job(archive); self.eng.process(job)
        self.assertEqual(job['state'],'Complete'); self.assertFalse(archive.exists())
        self.assertTrue((self.drive.root/'Games'/'Seven Zip Game'/'default.xex').exists())
    def test_drive_disconnect_keeps_input(self):
        iso=self.base/'Disconnected.iso'; make_iso(iso)
        with patch.object(self.drive,'check',side_effect=core.DriveMissing('Drive removed')):
            with self.assertRaises(core.DriveMissing): self.eng.process(self.job(iso))
        self.assertTrue(iso.exists())
    def test_broken_archive_retains_source(self):
        archive=self.base/'Broken.zip'; archive.write_bytes(b'not a zip')
        with self.assertRaises(ValueError): self.eng.process(self.job(archive))
        self.assertTrue(archive.exists())
    def test_archive_path_traversal_rejected(self):
        archive=self.base/'Unsafe.zip'
        with zipfile.ZipFile(archive,'w') as z: z.writestr('../escape.iso',b'123')
        with self.assertRaises(ValueError): core.archive_members(TOOL,archive)
    def test_iso_unsafe_name_rejected(self):
        iso=self.base/'Unsafe.iso'; make_iso(iso,{'default.xex':b'XEX2','../escape.bin':b'bad'})
        with iso.open('rb') as f:
            with self.assertRaises(ValueError): core.manifest(f)
    def test_iso_bounds_rejected(self):
        iso=self.base/'Truncated.iso'; make_iso(iso); iso.write_bytes(iso.read_bytes()[:100000])
        with iso.open('rb') as f:
            with self.assertRaises(ValueError): core.manifest(f)
    def test_cancel_retains_source_and_cleans_partial(self):
        iso=self.base/'Cancel.iso'; make_iso(iso); self.eng.cancel.set()
        with self.assertRaises(core.Cancelled): self.eng.process(self.job(iso))
        self.assertTrue(iso.exists()); self.assertFalse((self.drive.root/'Games'/'Cancel').exists())
    def test_previous_batch_blocks_new_work(self):
        iso=self.base/'Waiting.iso'; make_iso(iso); self.job(iso); self.eng.enabled=True; self.eng.drive_root=self.drive.root
        with patch('engine.batch_running',return_value=True),patch.object(self.eng,'process') as work:
            self.eng._work_once(); work.assert_not_called()
        self.assertIn('previous',self.eng.blocked)
    def test_multiple_discs_retry_reverifies_published_disc(self):
        a=self.root/'Game (Disc 1).iso'; b=self.root/'Game (Disc 2).iso'
        make_iso(a); make_iso(b,{'default.xex':b'XEX2-disc2','second.bin':b'content'})
        archive=self.base/'Game.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z: z.write(a,a.name); z.write(b,b.name)
        job=self.job(archive); real=engine.extract_and_verify; count=[0]
        def fail_second(*args):
            count[0]+=1
            if count[0]==2: raise ValueError('simulated failure')
            return real(*args)
        with patch('engine.extract_and_verify',side_effect=fail_second):
            with self.assertRaises(ValueError): self.eng.process(job)
        # All disc images prepare successfully before any game is published.
        self.assertTrue(archive.exists()); self.assertFalse((self.drive.root/'Games'/'Game'/'Disc 1'/'default.xex').exists())
        self.eng.process(job)
        self.assertFalse(archive.exists()); self.assertEqual(job['state'],'Complete')
    def test_restarted_app_is_paused_and_interrupted_job_retained(self):
        iso=self.base/'Restart.iso'; make_iso(iso); job=self.job(iso); job['state']='Converting'; self.eng.save()
        new=engine.Engine(self.base,TOOL,self.drive)
        try:
            self.assertFalse(new.enabled); self.assertEqual(new.jobs[0]['state'],'Interrupted'); self.assertTrue(iso.exists())
        finally:
            for h in new.logger.handlers: h.close()
    def test_safe_removal_cannot_escape_root(self):
        outside=self.root/'keep'; outside.mkdir(); (outside/'important.txt').write_text('keep')
        with self.assertRaises(ValueError): core.safe_remove_tree(outside,self.eng.work)
        self.assertTrue((outside/'important.txt').exists())
    def test_source_write_lock_prevents_processing_incomplete_copy(self):
        if os.name!='nt': self.skipTest('Windows sharing check')
        iso=self.base/'Copying.iso'; make_iso(iso)
        with core.locked_reader(iso):
            with self.assertRaises(OSError): iso.open('wb')
    def test_named_lock_is_exclusive(self):
        with core.NamedLock('Local\\Xbox360Test-'+str(os.getpid())):
            with self.assertRaises(BlockingIOError): core.NamedLock('Local\\Xbox360Test-'+str(os.getpid()))

if __name__=='__main__': unittest.main(verbosity=2)
