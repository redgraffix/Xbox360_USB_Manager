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
from runtime import find_7zip
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
