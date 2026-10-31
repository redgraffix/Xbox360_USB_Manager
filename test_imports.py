import json, os, struct, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
from test_converter import ConverterTests, make_iso
import content, engine, library, core

def package(path,kind=2,title=0x545408A7,name='Test content',pieces=0,data_size=0):
    h=bytearray(0x1800);h[:4]=b'LIVE'
    struct.pack_into('>I',h,0x344,kind);struct.pack_into('>I',h,0x360,title)
    struct.pack_into('>I',h,0x39D,pieces);struct.pack_into('>Q',h,0x3A1,data_size)
    encoded=name.encode('utf-16-be');h[0x411:0x411+len(encoded)]=encoded
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(h);return path

class ImportTests(ConverterTests):
    def job(self,path):
        j=super().job(path);j['signature']=content.input_signature(path);j['size']=j['signature'][0];return j
    def test_prepared_folder_import_and_source_cleanup(self):
        p=self.base/'Folder Game';p.mkdir();(p/'default.xex').write_bytes(b'XEX2-folder');(p/'data.bin').write_bytes(b'level data')
        j=self.job(p);self.eng.process(j)
        self.assertFalse(p.exists());self.assertEqual((self.drive.root/'Games'/'Folder Game'/'data.bin').read_bytes(),b'level data')
    def test_god_dlc_archive_import_routing_and_history(self):
        p=self.root/'Bundle';descriptor=package(p/'545408A7'/'00007000'/'GAME',0x7000,name='Test game',pieces=1,data_size=4)
        data=descriptor.with_name('GAME.data');data.mkdir();(data/'Data0000').write_bytes(b'GOD!')
        package(p/'545408A7'/'00000002'/'DLC')
        archive=self.base/'Bundle.zip'
        with zipfile.ZipFile(archive,'w') as z:
            for f in p.rglob('*'):
                if f.is_file():z.write(f,f.relative_to(p))
        j=self.job(archive);self.eng.process(j)
        dest=self.drive.root/'Content'/'0000000000000000'/'545408A7'
        self.assertEqual((dest/'00007000'/'GAME.data'/'Data0000').read_bytes(),b'GOD!')
        self.assertTrue((dest/'00000002'/'DLC').is_file());self.assertFalse(archive.exists())
        self.assertTrue(self.eng.verify_job(j));self.assertIn('Games on Demand',j['format'])
        groups=library.scan_library(self.drive.root,'Games',self.eng.jobs)
        self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]['components']),2)
    def test_missing_god_data_retains_original(self):
        p=self.base/'Bad GOD';package(p/'GAME',0x7000,pieces=1,data_size=3)
        with self.assertRaisesRegex(ValueError,'data folder'):self.eng.process(self.job(p))
        self.assertTrue(p.exists())
    def test_mixed_unknown_archive_is_not_partially_deleted(self):
        archive=self.base/'Mixed.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('default.xex',b'XEX2');z.writestr('outside/unknown.exe',b'unknown')
        # A default.xex at the archive root owns its game files, including arbitrary game assets.
        bundle=self.base/'Unknown.zip'
        with zipfile.ZipFile(bundle,'w') as z:z.writestr('Game/default.xex',b'XEX2');z.writestr('unknown.bin',b'unknown')
        j=self.job(bundle);self.eng.process(j)
        self.assertEqual(j['state'],'Needs review');self.assertTrue(bundle.exists());self.assertFalse((self.drive.root/'Games'/'Game').exists())
    def test_separate_dlc_not_duplicate_of_base_game(self):
        iso=self.base/'Base Game.iso';make_iso(iso);self.eng.process(self.job(iso))
        dlc=package(self.base/'DLC');j=self.job(dlc);self.eng.process(j)
        self.assertEqual(j['state'],'Complete')
    def test_unknown_archive_path_rejected_even_without_iso(self):
        archive=self.base/'Bad.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('../outside.txt','no')
        with self.assertRaises(ValueError):content.archive_inventory(self.eng.tool,archive)
    def test_history_clear_retains_duplicate_memory_and_hides_errors(self):
        iso=self.base/'Keep.iso';make_iso(iso);self.eng.delete_sources=False;j=self.job(iso);self.eng.process(j)
        self.eng.clear_finished();self.assertTrue(j['hidden']);self.eng.scan();self.eng.scan();self.assertEqual(len(self.eng.jobs),1)
        j['state']='Error';j['hidden']=False;self.eng.clear_finished();self.assertTrue(j['hidden'])
    def test_settings_new_source_temp_and_destination_folder(self):
        new=self.root/'other-input';new.mkdir();temp=self.root/'scratch'
        self.eng.configure(dict(input=str(new),temp=str(temp),games='My Games',cleanup='keep'))
        iso=new/'Custom.iso';make_iso(iso);j=self.job(iso);self.eng.process(j)
        self.assertTrue(iso.exists());self.assertTrue((self.drive.root/'My Games'/'Custom'/'default.xex').is_file())
        self.assertTrue(self.eng.state_file.is_relative_to(self.base));self.assertFalse(self.eng.enabled)
    def test_keep_temp_and_manual_cleanup(self):
        self.eng.settings['clean_temp']=False
        iso=self.root/'Temp.iso';make_iso(iso);archive=self.base/'Temp.zip'
        with zipfile.ZipFile(archive,'w') as z:z.write(iso,'Temp.iso')
        self.eng.process(self.job(archive));self.assertFalse(list(self.eng.work.iterdir()))
        self.eng.drive_root=self.drive.root;self.assertEqual(self.eng.clean_leftovers(),0)
        self.assertFalse(list(self.eng.work.iterdir()));self.assertTrue((self.drive.root/'Games'/'Temp').exists())
    def test_batch_keep_overrides_delete_default(self):
        self.eng.batch_policy='keep';iso=self.base/'Keep batch.iso';make_iso(iso);self.eng.process(self.job(iso));self.assertTrue(iso.exists())
    def test_ask_cleanup_reverifies_before_deleting(self):
        self.eng.settings['cleanup']='ask';iso=self.base/'Ask.iso';make_iso(iso);j=self.job(iso);self.eng.process(j)
        self.assertTrue(j['cleanup_pending']);self.assertTrue(iso.exists())
        out=self.drive.root/'Games'/'Ask'/'default.xex';out.write_bytes(b'changed')
        with self.assertRaises(ValueError):self.eng.cleanup_originals([j['id']])
        self.assertTrue(iso.exists())
    def test_report_lock_is_nonfatal(self):
        self.eng.save()
        with core.locked_reader(self.eng.state_file):self.assertFalse(self.eng.save())
        self.assertTrue(self.eng.save())
    def test_remove_game_components_keeps_dlc(self):
        p=self.base/'Bundle';package(p/'DLC')
        (p/'Game').mkdir();(p/'Game'/'default.xex').write_bytes(b'XEX2-test');j=self.job(p);self.eng.process(j)
        groups=library.scan_library(self.drive.root)
        game=next(c for g in groups for c in g['components'] if c['kind']=='Extracted game')
        dlc=next(c for g in groups for c in g['components'] if c['kind']=='DLC / install content')
        library.remove_components(self.eng,[game])
        self.assertFalse(Path(game['paths'][0]).exists());self.assertTrue(Path(dlc['paths'][0]).exists())
    def test_removal_cannot_delete_usb_root_or_unrelated_data(self):
        with self.assertRaises(ValueError):library.remove_components(self.eng,[dict(paths=[str(self.drive.root)])])
    def test_cleanup_excludes_unknown_work_folder(self):
        unknown=self.eng.work/'important';unknown.mkdir();(unknown/'keep.txt').write_text('keep')
        self.eng.clean_leftovers();self.assertTrue(unknown.exists())

    def test_matching_game_metadata_detects_other_format(self):
        root=self.drive.root;path=root/'Games'/'Existing'/'default.xex';path.parent.mkdir(parents=True)
        h=bytearray(0x100);h[:4]=b'XEX2';struct.pack_into('>I',h,0x14,1);struct.pack_into('>II',h,0x18,0x40006,0x40)
        struct.pack_into('>IIII',h,0x40,0x12345678,0,0,0x545408A7);h[0x52]=2;h[0x53]=2;path.write_bytes(h)
        metadata=dict(title_id='545408A7',media_id='12345678',version='00000000',disc=2)
        self.assertEqual(self.eng._metadata_match(root,metadata,[]),path.parent)
        metadata['disc']=1;self.assertIsNone(self.eng._metadata_match(root,metadata,[]))
    def test_reimport_kept_source_preserves_previous_history(self):
        iso=self.base/'Again.iso';make_iso(iso);self.eng.delete_sources=False;j=self.job(iso);self.eng.process(j)
        self.eng.clear_finished();self.eng.reimport([j['id']])
        self.assertEqual(len(self.eng.jobs),2);self.assertEqual(j['state'],'Complete');self.assertEqual(self.eng.jobs[-1]['state'],'Waiting')
    def test_title_update_routes_to_content_and_requires_activation(self):
        update=package(self.base/'tu00000001_00000000',kind=0xB0000,name='Game update')
        j=self.job(update);self.eng.process(j)
        self.assertTrue((self.drive.root/'Content'/'0000000000000000'/'545408A7'/'000B0000'/update.name).exists())
        self.assertIn('enable title updates',j['setup_note'])
    def test_source_not_deleted_if_verification_record_cannot_save(self):
        iso=self.base/'Record failure.iso';make_iso(iso);j=self.job(iso)
        real=__import__('pipeline').atomic_json
        def fail_manifest(path,data,required=False):
            if Path(path).parent.name=='manifests':raise PermissionError('locked verification record')
            return real(path,data,required)
        with patch('pipeline.atomic_json',side_effect=fail_manifest):
            with self.assertRaises(PermissionError):self.eng.process(j)
        self.assertTrue(iso.exists())

    def test_legacy_title_update_retained_without_publishing_bundle(self):
        bundle=self.base/'Legacy update bundle';package(bundle/'TU_00000001',kind=0xB0000,name='Old update')
        (bundle/'Game').mkdir();(bundle/'Game'/'default.xex').write_bytes(b'XEX2-test')
        j=self.job(bundle);self.eng.process(j)
        self.assertEqual(j['state'],'Needs review');self.assertTrue(bundle.exists())
        self.assertFalse((self.drive.root/'Games'/'Legacy update bundle').exists())
        self.assertFalse((self.drive.root/'Content').exists())

    def test_clear_results_hides_errors_but_keeps_waiting_jobs_and_files(self):
        source=self.base/'Waiting.iso';make_iso(source);pending=self.job(source)
        pending['state']='Queued'
        failed=dict(pending,id='f'*32,state='Error');self.eng.jobs.append(failed)
        self.assertEqual(self.eng.clear_finished(),1)
        self.assertTrue(failed['hidden']);self.assertFalse(pending.get('hidden',False));self.assertTrue(source.exists())
        self.assertEqual(len(self.eng.jobs),2)
    def test_library_inventory_matches_removal_signature(self):
        p=self.drive.root/'Games'/'Sample';(p/'Data').mkdir(parents=True)
        (p/'default.xex').write_bytes(b'XEX2-test');(p/'Data'/'MixedCase.bin').write_bytes(b'asset')
        amount,sig=library.inventory(p,lambda:None)
        self.assertEqual(sig,content.input_signature(p));self.assertEqual(amount,14)
    def test_library_scan_can_be_cancelled(self):
        import threading
        p=self.drive.root/'Games'/'Sample';p.mkdir(parents=True);(p/'default.xex').write_bytes(b'XEX2')
        cancel=threading.Event()
        with self.assertRaises(core.Cancelled):library.scan_library(self.drive.root,cancel=cancel,progress=lambda _:cancel.set())
    def test_exclusive_operation_release_and_cancelled_verification(self):
        iso=self.base/'Check.iso';make_iso(iso);self.eng.delete_sources=False;j=self.job(iso);self.eng.process(j)
        self.eng.begin_maintenance();self.eng.enable();self.assertFalse(self.eng.enabled)
        self.eng.management_cancel.set()
        with self.assertRaises(core.Cancelled):self.eng.verify_job(j)
        self.eng.end_maintenance();self.eng.enable();self.assertTrue(self.eng.enabled)
    def test_active_import_rejects_exclusive_operation(self):
        self.eng.active_id='test'
        with self.assertRaises(ValueError):self.eng.begin_maintenance()
        self.assertFalse(self.eng.maintenance)

    def test_archived_iso_keeps_disc_system_updates_despite_content_collision(self):
        sysdata=package(self.root/'system-update',kind=0xB0000,title=0xFFFE07D1).read_bytes()
        existing=package(self.drive.root/'Content'/'0000000000000000'/'FFFE07D1'/'000B0000'/'su20076000_00000000',kind=0xB0000,title=0xFFFE07D1,name='Other system update')
        original_existing=existing.read_bytes()
        iso=self.root/'New Game.iso';make_iso(iso,{'default.xex':b'XEX2-new-game','$SystemUpdate/su20076000_00000000':sysdata,'data/asset.bin':b'new game'})
        archive=self.base/'New Game.zip'
        with zipfile.ZipFile(archive,'w') as z:z.write(iso,iso.name)
        j=self.job(archive);self.eng.process(j)
        self.assertEqual(j['state'],'Complete');self.assertFalse(archive.exists())
        self.assertEqual((self.drive.root/'Games'/'New Game'/'$SystemUpdate'/'su20076000_00000000').read_bytes(),sysdata)
        self.assertEqual(existing.read_bytes(),original_existing)
    def test_matching_executable_and_sizes_do_not_prove_duplicate(self):
        first=self.base/'First.iso';make_iso(first,{'default.xex':b'XEX2-same','data.bin':b'AAAA'});self.eng.process(self.job(first))
        second=self.base/'Different game.iso';make_iso(second,{'default.xex':b'XEX2-same','data.bin':b'BBBB'});j=self.job(second);self.eng.process(j)
        self.assertEqual(j['state'],'Complete');self.assertEqual((self.drive.root/'Games'/'Different game'/'data.bin').read_bytes(),b'BBBB')
    def test_name_conflict_is_review_not_duplicate(self):
        iso=self.base/'Conflicting.iso';make_iso(iso);j=self.job(iso)
        dest=self.drive.root/'Games'/'Conflicting';dest.mkdir(parents=True);(dest/'keep.txt').write_text('keep')
        self.eng.drive_root=self.drive.root;self.eng.enabled=True
        self.eng._work_once()
        self.assertEqual(j['state'],'Needs review');self.assertTrue(iso.exists());self.assertTrue((dest/'keep.txt').exists())

del ConverterTests
if __name__=='__main__':unittest.main(verbosity=2)
