"""Performance regressions with small synthetic data, never a real game drive."""
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import core
import engine
from test_converter import ConverterTests, make_iso


class PerformanceTests(ConverterTests):
    # Reuse setup/helpers without collecting the inherited conversion suite twice.
    def test_iso_publishes_verified_stage_without_second_copy(self):
        source = self.base / 'Single write.iso'
        expected = make_iso(source)
        job = self.job(source)
        with patch('engine.verify_files', side_effect=AssertionError('redundant USB copy')):
            self.eng.process(job)
        self.assertEqual(job['state'], 'Complete')
        records = json.loads(Path(job['verification_manifest']).read_text())
        for name, data in expected.items():
            self.assertEqual(records[0]['files'][name]['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual((self.drive.root / 'Games' / 'Single write' / name).read_bytes(), data)

    def stage(self, job):
        return self.drive.root / 'Xbox360-Conversion-Staging' / 'Converter' / job['id'] / 'converted' / '0'

    def test_completed_interrupted_stage_is_reverified_without_rewriting(self):
        source = self.base / 'Resume.iso'; make_iso(source); job = self.job(source)
        with source.open('rb') as image:
            core.extract_and_verify(image, core.manifest(image), self.stage(job), lambda *a: None, lambda: None)
        with patch('core.StageWriter', side_effect=AssertionError('rewrote completed extraction')):
            self.eng.process(job)
        self.assertEqual(job['state'], 'Complete')
        self.assertFalse(source.exists())

    def test_corrupt_interrupted_stage_retains_original(self):
        source = self.base / 'Corrupt resume.iso'; make_iso(source); job = self.job(source)
        with source.open('rb') as image:
            core.extract_and_verify(image, core.manifest(image), self.stage(job), lambda *a: None, lambda: None)
        asset = self.stage(job) / 'data' / 'level.bin'
        asset.write_bytes(b'BAD!' + asset.read_bytes()[4:])
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.eng.process(job)
        self.assertTrue(source.exists())
        self.assertFalse((self.drive.root / 'Games' / 'Corrupt resume').exists())

    def test_incomplete_interrupted_stage_is_rebuilt(self):
        source = self.base / 'Partial.iso'; expected = make_iso(source); job = self.job(source)
        stage = self.stage(job); stage.mkdir(parents=True)
        (stage / 'default.xex').write_bytes(b'partial')
        self.eng.process(job)
        self.assertEqual((self.drive.root / 'Games' / 'Partial' / 'default.xex').read_bytes(), expected['default.xex'])

    def test_inventory_validates_directories_not_every_file_path(self):
        folder = self.root / 'many'; folder.mkdir()
        for n in range(300): (folder / f'{n}.txt').write_text('fixture')
        with patch('core.safe_path', wraps=core.safe_path) as check:
            self.assertEqual(len(core.actual_files(folder)), 300)
        self.assertLessEqual(check.call_count, 3)

    def test_inventory_cancel_stops_between_files(self):
        folder = self.root / 'many'; folder.mkdir()
        for n in range(100): (folder / f'{n}.txt').write_text('fixture')
        calls = []
        def checkpoint():
            calls.append(1)
            if len(calls) == 8: raise core.Cancelled()
        with self.assertRaises(core.Cancelled): core.actual_files(folder, checkpoint)
        self.assertEqual(len(calls), 8)

    def test_stop_during_inventory_keeps_stage_for_checked_retry(self):
        source = self.base / 'Stop scan.iso'; make_iso(source); job = self.job(source)
        update = self.eng.update
        def stop(job, **values):
            if values.get('detail') == 'Checking USB file list': self.eng.cancel.set()
            return update(job, **values)
        with patch.object(self.eng, 'update', side_effect=stop):
            with self.assertRaises(core.Cancelled): self.eng.process(job)
        self.assertTrue(source.exists()); self.assertTrue(self.stage(job).exists())
        self.eng.cancel.clear(); self.eng.process(job)
        self.assertEqual(job['state'], 'Complete')

    def test_unrelated_executable_does_not_trigger_whole_game_inventory(self):
        existing = self.drive.root / 'Games' / 'Unrelated'; existing.mkdir(parents=True)
        (existing / 'default.xex').write_bytes(b'not-the-same-executable')
        source = self.base / 'New game.iso'; make_iso(source)
        with patch('engine.folder_identity', side_effect=AssertionError('scanned unrelated game')):
            self.eng.process(self.job(source))

    def test_changed_verified_stage_is_not_published(self):
        source = self.base / 'Changed.iso'; make_iso(source); job = self.job(source)
        def change(*args):
            (self.stage(job) / 'default.xex').write_bytes(b'changed')
        with patch.object(self.eng, '_metadata_match', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'stage changed'):
                self.eng.process(job)
        self.assertTrue(source.exists())
        self.assertFalse((self.drive.root / 'Games' / 'Changed').exists())

    def test_inventory_rejects_links_and_keeps_external_files(self):
        folder = self.root / 'links'; folder.mkdir()
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'keep.txt').write_text('keep')
        try: os.symlink(outside, folder / 'linked', target_is_directory=True)
        except OSError: self.skipTest('Symlink creation unavailable')
        with self.assertRaises(ValueError): core.actual_files(folder)
        self.assertEqual((outside / 'keep.txt').read_text(), 'keep')

    def test_stage_writer_rejects_escape_and_existing_files(self):
        stage = self.root / 'write'; writer = core.StageWriter(stage)
        with self.assertRaises(ValueError): writer.target(Path('..') / 'escape')
        target = writer.target(Path('asset.bin')); target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError): writer.target(Path('asset.bin')).open('xb')
        self.assertEqual(target.read_bytes(), b'keep')

    def test_windows_reparse_attribute_is_rejected_from_cached_metadata(self):
        import stat
        entry = MagicMock(); entry.path = 'synthetic-link'
        entry.stat.return_value.st_mode = stat.S_IFDIR
        entry.stat.return_value.st_file_attributes = 0x400
        scan = MagicMock(); scan.__enter__.return_value = iter([entry])
        with patch('core.os.scandir', return_value=scan):
            with self.assertRaisesRegex(ValueError, 'Linked'):
                list(core.checked_entries(self.root))

    def test_mutation_after_byte_verification_cannot_become_new_baseline(self):
        source = self.base / 'Late change.iso'; make_iso(source); job = self.job(source)
        real = engine.game_unit
        def mutate(folder, label, files):
            (folder / 'default.xex').write_bytes(b'changed after verification')
            return real(folder, label, files)
        with patch('engine.game_unit', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'stage changed'):
                self.eng.process(job)
        self.assertTrue(source.exists())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(PerformanceTests(name) for name in PerformanceTests.__dict__ if name.startswith('test_'))


if __name__ == '__main__': unittest.main()
