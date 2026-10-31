"""Recovery and destination checks using synthetic data and a loopback FTP server."""
import contextlib
import io
import json
import logging
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from core import Duplicate
from engine import Engine
from destinations import FileSession, FTPSession, connect, identity, location_label, normalize, relative, DestinationConflict
from library_cache import load, scan, remove, thumbnail, package_art
from test_converter import ConverterTests, make_iso, TOOL
from test_imports import package


@contextlib.contextmanager
def ftp_server(root):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
    from pyftpdlib.ioloop import IOLoop
    from pyftpdlib.log import config_logging
    config_logging(level=logging.CRITICAL)
    logging.getLogger('pyftpdlib').setLevel(logging.CRITICAL)
    authorizer=DummyAuthorizer();authorizer.add_user('fixture','fixture-pass',str(root),perm='elradfmwMT')
    class Handler(FTPHandler): pass
    Handler.authorizer=authorizer
    server=FTPServer(('127.0.0.1',0),Handler,ioloop=IOLoop())
    thread=threading.Thread(target=server.serve_forever,kwargs=dict(timeout=.05,blocking=True,handle_exit=False),daemon=True);thread.start()
    config=dict(kind='ftp',host='127.0.0.1',port=server.socket.getsockname()[1],user='fixture',root='/Hdd1')
    try:yield config
    finally:server.close_all();thread.join(3)


class DestinationTests(ConverterTests):
    def test_missing_local_output_does_not_break_startup_or_recreate_it(self):
        output=self.root/'offline-output';output.mkdir()
        self.eng.settings['destination']=dict(kind='folder',path=str(output));self.eng.options(delete=False);output.rmdir()
        other=Engine(self.base,TOOL,self.drive)
        try:
            self.assertFalse(other.enabled);self.assertFalse(output.exists())
        finally:
            for h in other.logger.handlers:h.close()

    def test_removal_handles_preexisting_empty_subfolders(self):
        src=self.base/'Empty folders.iso';make_iso(src);self.eng.process(self.job(src))
        root=self.drive.root/'Games'/'Empty folders';(root/'empty'/'nested').mkdir(parents=True)
        rows=scan(self.eng,threading.Event());remove(self.eng,rows[0]['components'])
        self.assertFalse(root.exists())

    def test_local_output_stays_bound_across_settings_change(self):
        src=self.base/'Bound.iso';make_iso(src);j=self.job(src)
        with patch.object(FileSession,'put',side_effect=OSError('disconnected')):
            with self.assertRaises(OSError):self.eng.process(j)
        self.assertTrue((self.eng.work/j['id']/'prepared.json').exists())
        self.eng.settings['destination']=dict(kind='folder',path=str(self.root/'different'))
        with self.assertRaises(DestinationConflict):self.eng.process(j)
        self.assertTrue(src.exists())

    def test_dropped_transfer_holds_three_game_queue_and_reuses_verified_file(self):
        for n in range(3):make_iso(self.base/f'Queue {n}.iso',{'default.xex':f'XEX2-{n}'.encode(),'a.bin':bytes([n])*4000,'b.bin':b'data'})
        self.eng.scan();self.eng.scan();self.eng.enabled=True
        first=self.eng.jobs[0];real=FileSession.put;calls=[]
        def drop(session,path,*args):
            calls.append(path)
            if len(calls)==2:raise OSError('Disconnected fixture')
            return real(session,path,*args)
        with patch.object(FileSession,'put',drop):self.eng._work_once()
        self.assertEqual(first['state'],'Waiting for destination')
        self.assertEqual([j['state'] for j in self.eng.jobs[1:]],['Queued','Queued'])
        self.eng._work_once();self.assertEqual([j['state'] for j in self.eng.jobs[1:]],['Queued','Queued'])
        calls.clear()
        def track(session,path,*args):calls.append(path);return real(session,path,*args)
        self.eng.enable()
        with patch.object(FileSession,'put',track):self.eng._work_once()
        self.assertEqual(first['state'],'Complete');self.assertEqual(len(calls),2)
        self.assertFalse(Path(first['source']).exists());self.assertFalse((self.eng.work/first['id']).exists())
        self.eng._work_once();self.eng._work_once()
        self.assertTrue(all(j['state']=='Complete' for j in self.eng.jobs))

    def test_retrying_wholly_duplicate_after_disconnect_keeps_original(self):
        src=self.base/'Duplicate.iso';files=make_iso(src);j=self.job(src)
        out=self.drive.root/'Games'/'Duplicate'
        for name,data in files.items():p=out/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        real=FileSession.digest;count=[0]
        def drop(session,*args,**kw):
            count[0]+=1
            if count[0]==2:raise OSError('disconnect during duplicate check')
            return real(session,*args,**kw)
        with patch.object(FileSession,'digest',drop):
            with self.assertRaises(OSError):self.eng.process(j)
        self.assertTrue(j['transferred']);self.assertFalse(j.get('owned_files'))
        with self.assertRaises(Duplicate):self.eng.process(j)
        self.assertTrue(src.exists())

    def test_restart_is_paused_and_retains_preparation(self):
        src=self.base/'Restart.iso';make_iso(src);j=self.job(src)
        with patch.object(FileSession,'put',side_effect=OSError('power loss')):
            with self.assertRaises(OSError):self.eng.process(j)
        j['state']='Copying';self.eng.save(True)
        other=Engine(self.base,TOOL,self.drive,settle_seconds=0)
        try:
            self.assertFalse(other.enabled);self.assertEqual(other.jobs[0]['state'],'Interrupted')
            self.assertTrue((other.work/j['id']/'prepared.json').exists())
            other.retry([j['id']]);other.scan();other.scan();other.enable();other._work_once()
            self.assertEqual(other.jobs[0]['state'],'Complete')
        finally:
            for h in other.logger.handlers:h.close()

    def test_cache_is_offline_and_never_proves_duplicate_by_itself(self):
        src=self.base/'First.iso';make_iso(src);self.eng.process(self.job(src))
        with patch('library_cache.connect',side_effect=AssertionError('cache opened destination')):
            cached=load(self.eng)
        self.assertEqual(len(cached['items']),1);self.assertEqual(cached['items'][0]['location'],'Local folder')
        (self.drive.root/'Games'/'First'/'default.xex').write_bytes(b'altered')
        src=self.base/'Second.iso';make_iso(src);j=self.job(src);self.eng.process(j)
        self.assertEqual(j['state'],'Complete')
        self.assertEqual((self.drive.root/'Games'/'First'/'default.xex').read_bytes(),b'altered')

    def test_remove_rejects_new_files_and_leaves_everything(self):
        src=self.base/'Remove.iso';make_iso(src);self.eng.process(self.job(src))
        items=scan(self.eng,threading.Event());extra=self.drive.root/'Games'/'Remove'/'new.txt';extra.write_text('keep')
        with self.assertRaisesRegex(ValueError,'changed'):remove(self.eng,items[0]['components'])
        self.assertTrue(extra.exists());self.assertTrue((extra.parent/'default.xex').exists())

    def test_real_ftp_drop_resume_scan_verify_and_cleanup(self):
        hdd=self.drive.root/'Hdd1';hdd.mkdir()
        src=self.base/'FTP game.iso';files=make_iso(src);j=self.job(src)
        with ftp_server(self.drive.root) as config, patch('credentials.read',return_value='fixture-pass'):
            self.eng._supplied_drive=None;self.eng.settings['destination']=config;self.eng.enabled=True
            real=FTPSession.put;count=[0]
            def dropped(session,*args):
                count[0]+=1
                if count[0]==2:session.ftp.close();raise ConnectionResetError('fixture drop')
                return real(session,*args)
            with patch.object(FTPSession,'put',dropped):self.eng._work_once()
            self.assertEqual(j['state'],'Waiting for destination');self.assertTrue(src.exists())
            self.eng.enable();self.eng._work_once();self.assertEqual(j['state'],'Complete');self.assertFalse(src.exists())
            for name,data in files.items():self.assertEqual((hdd/'Games'/'FTP game'/name).read_bytes(),data)
            self.assertTrue(self.eng.verify_job(j))
            rows=scan(self.eng,threading.Event());self.assertEqual(rows[0]['location'],'Xbox HDD')
            self.assertEqual(rows[0]['name'],'FTP game')
            remove(self.eng,rows[0]['components']);self.assertFalse((hdd/'Games'/'FTP game').exists())
            self.assertEqual(load(self.eng)['items'],[])

    def test_god_descriptor_is_published_after_its_data(self):
        source=self.base/'God';source.mkdir();descriptor=package(source/'fixture',kind=0x7000,pieces=1,data_size=15)
        (source/'fixture.data').mkdir();(source/'fixture.data'/'Data0000').write_bytes(b'fixture content')
        self.eng.scan();self.eng.scan();j=self.eng.jobs[0];real=FileSession.rename;names=[]
        def record(session,old,new):names.append(new);return real(session,old,new)
        with patch.object(FileSession,'rename',record):self.eng.process(j)
        self.assertTrue(names[0].endswith('fixture.data/Data0000'));self.assertTrue(names[-1].endswith('/fixture'))

    def test_artwork_and_locations(self):
        image=Image.new('RGB',(200,100),'green');stream=io.BytesIO();image.save(stream,'PNG');raw=stream.getvalue()
        name=thumbnail(self.eng,raw)
        with Image.open(self.eng.state_dir/'thumbnails'/name) as small:self.assertEqual(small.size,(40,20))
        self.assertIsNone(thumbnail(self.eng,b'not an image'))
        for root,label in [('/Hdd1','Xbox HDD'),('/Usb0','Xbox USB')]:
            self.assertEqual(location_label(dict(kind='ftp',host='example.invalid',user='test',root=root)),label)
        self.assertEqual(location_label(dict(kind='usb',volume=dict(serial=123,label='Fixture'))),'PC USB')

    def test_traversal_and_secret_fields_are_rejected_or_removed(self):
        for path in ('../escape','a/../b','/absolute','a\r\nDELE b'):
            with self.assertRaises(ValueError):relative(path)
        config=normalize(dict(kind='ftp',host='example.invalid',root='/Hdd1',password='never-save'))
        self.assertNotIn('password',config)
        self.assertNotEqual(identity(config),identity({**config,'root':'/Usb0'}))
        with self.assertRaises(ValueError):normalize({**config,'host':'host\r\nUSER other'})


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(DestinationTests(name) for name in DestinationTests.__dict__ if name.startswith('test_'))

if __name__=='__main__':unittest.main()
