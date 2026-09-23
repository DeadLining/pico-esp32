import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SERVER = Path(__file__).parents[2] / 'docker' / 'firmware-builder' / 'server.py'


def load(root, out):
    os.environ['FIRMWARE_SOURCE_DIR'] = str(root)
    os.environ['FIRMWARE_OUTPUT_DIR'] = str(out)
    os.environ['FIRMWARE_SOURCE_REVISION'] = 'testrev'
    spec = importlib.util.spec_from_file_location('fb_server_%d' % id(out), SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.JOBS.clear()
    return mod


def make_manifest(server, job_id, files, artifacts=None):
    manifest = {'schema': 2, 'product': 'Pico', 'version': 'pico',
                'cache_key': server.JOBS[job_id]['cache_key'], 'board': 'x',
                'chip': 'ESP32-C3', 'flashSize': 8388608, 'files': files,
                'artifacts': artifacts if artifacts is not None else files}
    (server.OUT / job_id / 'manifest.json').write_text(json.dumps(manifest))
    server.JOBS[job_id]['manifest'] = manifest
    return manifest


class FirmwareBuilderHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'src'
        self.out = Path(self.tmp.name) / 'out'
        self.root.mkdir()
        self.out.mkdir()
        self.server = load(self.root, self.out)

    def _new_job(self, body):
        key = self.server.cache_key(body)
        jid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.server.JOBS[jid] = {'id': jid, 'status': 'queued', 'progress': 0,
                                 'log': '', 'cache_key': key,
                                 'created_at': self.server.now_iso(),
                                 'request': dict(body)}
        return jid

    def test_history_lists_newest_first_with_file_availability(self):
        jid = self._new_job({'board': 'folotoy/ai-passport', 'name': 'ai-passport',
                             'target': 'esp32c3', 'language': 'zh-CN',
                             'wake_word': 'wn9s_hiesp', 'build_options': {}})
        d = self.out / jid; d.mkdir(parents=True)
        (d / 'merged-binary.bin').write_bytes(b'fw')
        make_manifest(self.server, jid, [], [
            {'name': 'merged-binary.bin', 'size': 2, 'sha256': 'a' * 64}])
        h = self.server.history()
        self.assertEqual(len(h['builds']), 1)
        row = h['builds'][0]
        self.assertEqual(row['id'], jid)
        self.assertEqual(row['request']['board'], 'folotoy/ai-passport')
        self.assertEqual(row['chip'], 'ESP32-C3')
        self.assertEqual(row['files'][0]['name'], 'merged-binary.bin')
        self.assertTrue(row['files'][0]['available'])

    def test_history_marks_missing_artifact_unavailable(self):
        jid = self._new_job({'board': 'b', 'name': 'n', 'target': 'esp32c3',
                             'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}})
        (self.out / jid).mkdir(parents=True)
        make_manifest(self.server, jid, [], [
            {'name': 'pico.bin', 'size': 2, 'sha256': 'b' * 64}])
        self.assertFalse(self.server.history()['builds'][0]['files'][0]['available'])

    def test_cache_hit_reuses_success_artifact(self):
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {'x': 1}}
        jid = self._new_job(body)
        d = self.out / jid; d.mkdir(parents=True)
        (d / 'merged-binary.bin').write_bytes(b'fw')
        (d / 'pico.bin').write_bytes(b'ota')
        make_manifest(self.server, jid, [
            {'name': 'pico.bin', 'size': 3, 'sha256': 'd' * 64, 'address': 0x20000},
            {'name': 'merged-binary.bin', 'size': 2, 'sha256': 'c' * 64, 'address': 0}])
        self.server.JOBS[jid]['status'] = 'success'
        hit = self.server.find_cached(self.server.cache_key(body))
        self.assertIsNotNone(hit)
        self.assertTrue(hit['cached'])
        # different build_options must not collide
        other = dict(body, build_options={'x': 2})
        self.assertIsNone(self.server.find_cached(self.server.cache_key(other)))

    def test_legacy_overlapping_manifest_is_never_a_cache_source(self):
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        jid = self._new_job(body)
        d = self.out / jid; d.mkdir(parents=True)
        for n in ('pico.bin','merged-binary.bin'): (d/n).write_bytes(b'x')
        # pre-fix layout: merged image listed as a flash segment -> overlapping regions
        (d/'manifest.json').write_text(json.dumps({'schema':2,'product':'Pico','version':'pico',
            'cache_key': self.server.JOBS[jid]['cache_key'],'board':'x','chip':'ESP32-C3',
            'flashSize':8388608,'files':[
              {'name':'pico.bin','size':1,'sha256':'a'*64,'address':0x20000},
              {'name':'merged-binary.bin','size':1,'sha256':'b'*64,'address':0}]}))
        self.server.JOBS[jid]['manifest']=json.loads((d/'manifest.json').read_text())
        self.server.JOBS[jid]['status']='success'
        self.assertIsNone(self.server.find_cached(self.server.cache_key(body)))

    def test_cache_miss_when_artifact_deleted(self):
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        jid = self._new_job(body)
        (self.out / jid).mkdir(parents=True)
        make_manifest(self.server, jid, [
            {'name': 'merged-binary.bin', 'size': 2, 'sha256': 'd' * 64, 'address': 0}])
        self.server.JOBS[jid]['status'] = 'success'
        self.assertIsNone(self.server.find_cached(self.server.cache_key(body)))

    def test_load_jobs_restores_persisted_success_and_backfills_created_at(self):
        jid = '12345678-1234-1234-1234-123456789abc'
        d = self.out / jid; d.mkdir(parents=True)
        d.joinpath('job.json').write_text(json.dumps(
            {'id': jid, 'status': 'success', 'progress': 100, 'cache_key': 'k'}))
        d.joinpath('manifest.json').write_text(json.dumps(
            {'schema': 2, 'product': 'Pico', 'files': []}))
        self.server.JOBS.clear()
        self.server.load_jobs()
        restored = self.server.JOBS[jid]
        self.assertEqual(restored['status'], 'success')
        self.assertIn('manifest', restored)
        self.assertIn('created_at', restored)

    def test_manifest_only_legacy_build_is_recovered(self):
        jid = 'aaaaaaaa-0000-0000-0000-000000000002'
        d = self.out / jid; d.mkdir(parents=True)
        d.joinpath('manifest.json').write_text(json.dumps(
            {'schema': 2, 'product': 'Pico', 'version': 'pico', 'board': 'ai-passport',
             'chip': 'ESP32-C3', 'flashSize': 8388608,
             'files': [{'name': 'merged-binary.bin', 'size': 2, 'sha256': 'e' * 64, 'address': 0}]}))
        self.server.JOBS.clear()
        self.server.load_jobs()
        self.assertIn(jid, self.server.JOBS)
        row = self.server.history()['builds'][0]
        self.assertEqual(row['status'], 'success')
        self.assertEqual(row['chip'], 'ESP32-C3')
        self.assertEqual(row['request']['target'], 'esp32c3')

    def test_non_pico_manifest_is_not_recovered(self):
        jid = 'aaaaaaaa-0000-0000-0000-000000000003'
        d = self.out / jid; d.mkdir(parents=True)
        d.joinpath('manifest.json').write_text(json.dumps(
            {'schema': 1, 'product': 'xiaozhi', 'files': []}))
        self.server.JOBS.clear()
        self.server.load_jobs()
        self.assertNotIn(jid, self.server.JOBS)

    def test_legacy_record_without_id_uses_directory_name(self):
        jid = 'aaaaaaaa-0000-0000-0000-000000000001'
        d = self.out / jid; d.mkdir(parents=True)
        d.joinpath('job.json').write_text(json.dumps(
            {'status': 'failed', 'progress': 100, 'cache_key': 'k'}))
        self.server.JOBS.clear()
        self.server.load_jobs()
        self.assertIn(jid, self.server.JOBS)

    def test_flash_segments_reads_real_idf_layout(self):
        b=self.root/'build'; (b/'bootloader').mkdir(parents=True); (b/'partition_table').mkdir()
        for rel in ('bootloader/bootloader.bin','partition_table/partition-table.bin','ota_data_initial.bin','generated_assets.bin','pico.bin'):
            (b/rel).write_bytes(b'x')
        (b/'flasher_args.json').write_text(json.dumps({'flash_files':{
            '0x0':'bootloader/bootloader.bin','0x8000':'partition_table/partition-table.bin',
            '0xd000':'ota_data_initial.bin','0x600000':'generated_assets.bin','0x20000':'pico.bin'}}))
        segs=self.server.flash_segments(self.root)
        self.assertEqual([a for a,_,_ in segs],[0,0x8000,0xd000,0x20000,0x600000])
        self.assertEqual([n for _,n,_ in segs][3],'pico.bin')

    def test_flash_segments_reject_missing_file(self):
        b=self.root/'build'; b.mkdir(parents=True)
        (b/'flasher_args.json').write_text(json.dumps({'flash_files':{'0x0':'bootloader/bootloader.bin'}}))
        with self.assertRaises(RuntimeError): self.server.flash_segments(self.root)

    def test_merged_image_never_appears_as_a_flash_segment(self):
        """The merged image starts at 0x0 and would overlap every real segment."""
        b=self.root/'build'; b.mkdir(parents=True)
        (b/'pico.bin').write_bytes(b'x')
        (b/'flasher_args.json').write_text(json.dumps({'flash_files':{'0x20000':'pico.bin'}}))
        names=[n for _,n,_ in self.server.flash_segments(self.root)]
        self.assertNotIn('merged-binary.bin',names)

    def test_history_reports_ota_and_full_as_separate_available_artifacts(self):
        jid = self._new_job({'board': 'folotoy/ai-passport', 'name': 'ai-passport',
                             'target': 'esp32c3', 'language': 'zh-CN',
                             'wake_word': 'wn9s_hiesp', 'build_options': {}})
        d = self.out / jid; d.mkdir(parents=True)
        (d / 'pico.bin').write_bytes(b'ota')
        (d / 'merged-binary.bin').write_bytes(b'full')
        make_manifest(self.server, jid, [], [
            {'name': 'pico.bin', 'size': 3, 'sha256': 'f' * 64},
            {'name': 'merged-binary.bin', 'size': 4, 'sha256': 'a' * 64}])
        names = [f['name'] for f in self.server.history()['builds'][0]['files']]
        self.assertIn('pico.bin', names)
        self.assertIn('merged-binary.bin', names)

    def test_legacy_manifest_is_not_marked_complete_or_flashable(self):
        """Old records keep the merged image in `files`; flashing them must be refused."""
        jid='aaaaaaaa-0000-0000-0000-00000000000f'
        d=self.out/jid; d.mkdir(parents=True)
        (d/'pico.bin').write_bytes(b'ota'); (d/'merged-binary.bin').write_bytes(b'full')
        legacy={'schema':2,'product':'Pico','version':'pico','cache_key':'k','board':'ai-passport',
                'chip':'ESP32-C3','flashSize':8388608,'files':[
                    {'name':'pico.bin','address':0x20000,'size':3,'sha256':'d'*64},
                    {'name':'merged-binary.bin','address':0,'size':4,'sha256':'a'*64}]}
        (d/'manifest.json').write_text(json.dumps(legacy)); (d/'job.json').write_text(json.dumps(
            {'id':jid,'status':'success','progress':100,'log':'','cache_key':'k','manifest':legacy}))
        self.server.JOBS.clear(); self.server.load_jobs()
        row=self.server.history()['builds'][0]
        self.assertFalse(row['complete'],'legacy record must not offer USB flashing')

    def test_cached_hit_does_not_serve_a_build_missing_ota(self):
        """A build without pico.bin must not be reused as a cache source."""
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        jid = self._new_job(body)
        d = self.out / jid; d.mkdir(parents=True)
        (d / 'merged-binary.bin').write_bytes(b'fw')
        # only the full image exists -> incomplete artifact set
        make_manifest(self.server, jid, [
            {'name': 'merged-binary.bin', 'size': 2, 'sha256': 'c' * 64, 'address': 0}])
        self.server.JOBS[jid]['status'] = 'success'
        hit = self.server.find_cached(self.server.cache_key(body))
        if hit is not None:
            names = [f['name'] for f in (hit.get('manifest') or {}).get('files', [])]
            self.assertIn('pico.bin', names)

    def test_delete_removes_record_directory_and_retires_cache(self):
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        jid = self._new_job(body)
        d = self.out / jid; d.mkdir(parents=True)
        (d / 'merged-binary.bin').write_bytes(b'x' * 100)
        (d / 'pico.bin').write_bytes(b'y' * 50)
        (d / 'bootloader.bin').write_bytes(b'z' * 20)
        make_manifest(self.server, jid, [
            {'name': 'bootloader.bin', 'size': 20, 'sha256': 'c' * 64, 'address': 0}],
            [
            {'name': 'merged-binary.bin', 'size': 100, 'sha256': 'a' * 64},
            {'name': 'pico.bin', 'size': 50, 'sha256': 'b' * 64}])
        self.server.JOBS[jid]['status'] = 'success'
        key = self.server.cache_key(body)
        self.assertIsNotNone(self.server.find_cached(key))

        result = self.server.delete_build(jid)

        self.assertEqual(result['deleted'], jid)
        self.assertGreaterEqual(result['freed_bytes'], 170)
        self.assertFalse(d.exists())
        self.assertNotIn(jid, self.server.JOBS)
        self.assertEqual(self.server.history()['builds'], [])
        # The cache key is retired too: same config must compile again.
        self.assertIsNone(self.server.find_cached(key))

    def test_restart_reaps_interrupted_build_so_it_is_not_stuck_forever(self):
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        jid = self._new_job(body)
        d = self.out / jid; d.mkdir(parents=True)
        self.server.JOBS[jid]['status'] = 'running'
        self.server.persist(self.server.JOBS[jid])
        self.server.JOBS.clear()

        self.server.load_jobs()

        row = self.server.JOBS[jid]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('中断', row['error'])
        # Persisted too, so a later restart does not resurrect the stuck状态.
        on_disk = json.loads((d / 'job.json').read_text())
        self.assertEqual(on_disk['status'], 'failed')

    def test_interrupted_build_can_be_deleted_after_restart(self):
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        jid = self._new_job(body)
        d = self.out / jid; d.mkdir(parents=True)
        self.server.JOBS[jid]['status'] = 'running'
        self.server.JOBS.clear()
        self.server.load_jobs()
        # No live compile process exists, so this must not raise "build in progress".
        result = self.server.delete_build(jid)
        self.assertEqual(result['deleted'], jid)
        self.assertFalse(d.exists())

    def test_delete_rejects_a_build_with_a_live_compile(self):
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        jid = self._new_job(body)
        (self.out / jid).mkdir(parents=True)
        self.server.JOBS[jid]['status'] = 'running'
        # Only a build with a live compile subprocess is protected from deletion.
        self.server.ACTIVE.add(jid)
        with self.assertRaises(RuntimeError):
            self.server.delete_build(jid)
        self.assertTrue((self.out / jid).is_dir())

    def test_delete_rejects_path_traversal_and_unknown_id(self):
        with self.assertRaises(ValueError):
            self.server.delete_build('../evil')
        with self.assertRaises(FileNotFoundError):
            self.server.delete_build('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')

    def test_source_revision_participates_in_cache_key(self):
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        k1 = self.server.cache_key(body)
        os.environ['FIRMWARE_SOURCE_REVISION'] = 'other'
        self.assertNotEqual(k1, self.server.cache_key(body))

    def test_editing_a_source_file_changes_the_cache_key(self):
        # The image is built from the working tree, so an uncommitted edit must
        # invalidate the cache even when FIRMWARE_SOURCE_REVISION is unchanged.
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        board = self.root / 'main' / 'boards' / 'demo'
        board.mkdir(parents=True)
        source = board / 'board.cc'
        source.write_text('int version = 1;\n')

        k1 = self.server.cache_key(body)
        self.server._FINGERPRINT['at'] = 0.0  # bypass the short-lived memo
        self.assertEqual(k1, self.server.cache_key(body),
                         'an unchanged tree must keep the same key')

        source.write_text('int version = 2;\n')
        self.server._FINGERPRINT['at'] = 0.0
        self.assertNotEqual(k1, self.server.cache_key(body),
                            'a changed source file must invalidate the cache')

    def test_fingerprint_ignores_build_and_vendor_directories(self):
        # Build outputs and fetched components are machine-local, so churning
        # them must not invalidate a cache entry.
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        (self.root / 'main').mkdir(parents=True)
        (self.root / 'main' / 'app.cc').write_text('int main(){}\n')
        k1 = self.server.cache_key(body)

        for ignored in ('build', 'managed_components', '.git'):
            d = self.root / ignored
            d.mkdir(parents=True, exist_ok=True)
            (d / 'artifact.bin').write_bytes(b'noise')
        self.server._FINGERPRINT['at'] = 0.0
        self.assertEqual(k1, self.server.cache_key(body))

    def test_restoring_identical_content_keeps_the_cache_key(self):
        # Content hashing, not mtime: restoring a file byte-for-byte changes its
        # mtime but must not throw away an otherwise valid cache entry.
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        src = self.root / 'main' / 'app.cc'
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text('int value = 7;\n')
        k1 = self.server.cache_key(body)

        os.utime(src, (1, 1))  # same bytes, different timestamps
        self.server._FINGERPRINT['at'] = 0.0
        self.assertEqual(k1, self.server.cache_key(body))

    def test_fingerprint_ignores_build_generated_source_files(self):
        # `idf.py reconfigure` rewrites sdkconfig/sdkconfig.old and the component
        # manager rewrites dependencies.lock; build.py regenerates
        # lang_config.h. They sit inside the source tree, so without an explicit
        # skip list every build would invalidate its own cache entry.
        body = {'board': 'b', 'name': 'n', 'target': 'esp32c3',
                'language': 'zh-CN', 'wake_word': 'w', 'build_options': {}}
        (self.root / 'main' / 'assets').mkdir(parents=True, exist_ok=True)
        (self.root / 'main' / 'app.cc').write_text('int main(){}\n')
        k1 = self.server.cache_key(body)

        (self.root / 'sdkconfig').write_text('# regenerated\n')
        (self.root / 'sdkconfig.old').write_text('# regenerated\n')
        (self.root / 'dependencies.lock').write_text('{}')
        (self.root / 'main' / 'assets' / 'lang_config.h').write_text('#pragma once\n')
        self.server._FINGERPRINT['at'] = 0.0
        self.assertEqual(k1, self.server.cache_key(body))

    def test_fingerprint_is_stable_across_repeated_calls(self):
        first = self.server.source_fingerprint()
        self.server._FINGERPRINT['at'] = 0.0
        self.assertEqual(first, self.server.source_fingerprint())
        self.assertNotEqual(first, 'unknown')


if __name__ == '__main__':
    unittest.main()
