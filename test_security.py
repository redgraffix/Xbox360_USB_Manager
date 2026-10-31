"""Privacy defaults and unsafe-input regressions using synthetic fixtures only."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import content
import core
from engine import Engine
from runtime import find_7zip, default_base
from test_converter import TestDrive, make_iso


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xbox-security-')
        self.root = Path(self.temp.name).resolve()
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.close()
            for handler in list(engine.logger.handlers):
                handler.close()
                engine.logger.removeHandler(handler)
        self.temp.cleanup()

    def engine(self, base=None, drive=None):
        engine = Engine(base or self.root / 'input', find_7zip(), drive, settle_seconds=0)
        self.engines.append(engine)
        return engine

    def test_first_run_keeps_sources_and_requires_drive_selection(self):
        engine = self.engine()
        self.assertEqual(engine.settings['cleanup'], 'keep')
        self.assertFalse(engine.delete_sources)
        self.assertFalse(engine.enabled)
        self.assertIsNone(engine.settings['drive'])
        with self.assertRaises(core.DriveMissing):
            engine.drive.locate()

    def test_windows_redirected_work_folder_can_save_settings_and_import(self):
        logical=self.root/'local-data'/'Work';physical=self.root/'package-cache'/'Work'
        physical.mkdir(parents=True);logical.mkdir(parents=True)
        original=Path.resolve
        def redirected(path,*args,**kwargs):
            if path==logical or path.is_relative_to(logical):return physical/path.relative_to(logical)
            return original(path,*args,**kwargs)
        drive=TestDrive(self.root/'usb');engine=self.engine(drive=drive)
        source=engine.base/'Redirected.iso';files=make_iso(source)
        with patch.object(Path,'resolve',redirected):
            engine.configure(dict(temp=str(logical),destination=dict(kind='ftp',host='example.invalid',user='fixture',root='/Usb0')))
            engine.scan();engine.scan();engine.enable();engine._work_once()
        self.assertEqual(engine.jobs[0]['state'],'Complete')
        self.assertTrue(source.exists())
        self.assertEqual((drive.root/'Games'/'Redirected'/'default.xex').read_bytes(),files['default.xex'])

    def test_resolved_child_must_stay_within_resolved_root(self):
        root=self.root/'selected';root.mkdir();child=root/'file';outside=self.root/'outside'
        original=Path.resolve
        def escaped(path,*args,**kwargs):
            if path==child:return outside
            return original(path,*args,**kwargs)
        with patch.object(Path,'resolve',escaped):
            with self.assertRaisesRegex(ValueError,'outside'):core.safe_path(child,root)

    def test_redirected_target_reparse_points_are_still_rejected(self):
        import stat
        from types import SimpleNamespace
        logical=self.root/'logical';actual=self.root/'actual';logical.mkdir();actual.mkdir()
        resolve=Path.resolve;lstat=Path.lstat
        def redirected(path,*args,**kwargs):
            return actual if path==logical else resolve(path,*args,**kwargs)
        def metadata(path,*args,**kwargs):
            return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400) if path==actual else lstat(path,*args,**kwargs)
        with patch.object(Path,'resolve',redirected),patch.object(Path,'lstat',metadata):
            with self.assertRaisesRegex(ValueError,'Linked'):core.safe_path(logical,logical)

    def test_portable_app_uses_user_profile_not_executable_parent(self):
        import sys
        with patch('runtime.Path.home', return_value=self.root / 'profile'), \
             patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', str(self.root / 'portable' / 'Converter.exe')):
            self.assertEqual(default_base(), self.root / 'profile' / 'XBOX360-Convert')

    def test_missing_archive_helper_shows_error_before_creating_profile(self):
        import app
        with patch.object(app.sys, 'argv', ['app.py']), \
             patch('app.find_7zip', side_effect=FileNotFoundError('Install 7-Zip')), \
             patch('app.ctypes.windll.user32.MessageBoxW') as dialog, \
             patch('app.Engine') as engine:
            self.assertEqual(app.main(), 1)
        dialog.assert_called_once()
        engine.assert_not_called()

    def test_keep_originals_dismisses_pending_cleanup_persistently(self):
        engine = self.engine()
        job = dict(id='a'*32, state='Complete', source=str(engine.base / 'example.iso'), cleanup_pending=True)
        engine.jobs.append(job)
        engine.keep_originals([job['id']])
        self.assertFalse(self.engine().jobs[0]['cleanup_pending'])

    def test_first_run_import_does_not_delete_original(self):
        import time, uuid
        drive = TestDrive(self.root / 'usb')
        engine = self.engine(drive=drive)
        source = engine.base / 'Synthetic Game.iso'
        make_iso(source)
        job = dict(id=uuid.uuid4().hex, source=str(source), source_root=str(engine.base),
                   name=source.name, signature=content.input_signature(source),
                   state='Queued', size=source.stat().st_size, outputs=[], added=time.time())
        engine.jobs.append(job)
        engine.process(job)
        self.assertEqual(job['state'], 'Complete')
        self.assertTrue(source.exists())
        self.assertTrue((drive.root / 'Games' / 'Synthetic Game' / 'default.xex').exists())

    def test_old_history_does_not_invent_drive_identity_or_enable_deletion(self):
        state = self.root / 'input' / '_state'
        state.mkdir(parents=True)
        (state / 'queue.json').write_text(json.dumps({'jobs': []}), encoding='utf-8')
        engine = self.engine()
        self.assertIsNone(engine.settings['drive'])
        self.assertFalse(engine.delete_sources)

    def test_explicit_cleanup_preference_survives_restart(self):
        first = self.engine()
        first.options(delete=True)
        second = self.engine()
        self.assertEqual(second.settings['cleanup'], 'delete')
        self.assertTrue(second.delete_sources)
        self.assertFalse(second.enabled)

    def test_archive_helper_does_not_search_current_directory_or_path(self):
        fake = self.root / '7z.exe'
        fake.write_bytes(b'not executable')
        with patch.dict(os.environ, {'PATH': str(self.root)}, clear=True):
            with self.assertRaises(FileNotFoundError):
                find_7zip()
            with self.assertRaises(ValueError):
                find_7zip('7z.exe')
            self.assertEqual(find_7zip(str(fake)), fake.resolve())

    def test_archive_helper_standard_install_and_override(self):
        standard = self.root / '7-Zip' / '7z.exe'
        standard.parent.mkdir()
        standard.write_bytes(b'fixture')
        with patch.dict(os.environ, {'ProgramFiles': str(self.root)}, clear=True):
            self.assertEqual(find_7zip(), standard.resolve())
        with patch.dict(os.environ, {'XBOX360_7ZIP': str(standard)}, clear=True):
            self.assertEqual(find_7zip(), standard.resolve())

    def test_malformed_archive_entries_are_rejected_before_extraction(self):
        listings = [
            'Path = ../escape.txt\nSize = 1',
            'Path = CON.txt\nSize = 1',
            'Path = file:stream\nSize = 1',
            'Path = link\nSymbolic Link = target\nSize = 0',
            'Path = file\nEncrypted = +\nSize = 1',
            'Path = file\nSize = -1',
            'Path = A.txt\nSize = 1\n\nPath = a.txt\nSize = 1',
        ]
        for listing in listings:
            with self.subTest(listing=listing):
                result = subprocess.CompletedProcess([], 0, stdout=listing.encode(), stderr=b'')
                with patch('content.subprocess.run', return_value=result):
                    with self.assertRaises(ValueError):
                        content.archive_inventory('unused', self.root / 'test.zip')

    def test_games_destination_cannot_escape_usb(self):
        engine = self.engine()
        for destination in ('../outside', 'Content', 'Xbox360-Conversion-Staging'):
            with self.subTest(destination=destination):
                with self.assertRaises(ValueError):
                    engine.configure({'games': destination})


if __name__ == '__main__':
    unittest.main(verbosity=2)
