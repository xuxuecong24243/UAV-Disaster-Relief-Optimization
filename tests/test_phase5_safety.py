"""Release protection checks; fixtures are bytes, never synthetic experiment results."""
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
os.environ.setdefault('UAV_RUN_DIR', str(ROOT/'results/runs'/('safety_'+uuid.uuid4().hex[:10])))
os.environ.setdefault('MPLCONFIGDIR', str(Path(os.environ['UAV_RUN_DIR'])/'.matplotlib'))
os.environ.setdefault('MPLBACKEND', 'Agg')
import project_paths as paths
paths.bootstrap()


class Phase5SafetyTests(unittest.TestCase):
    def setUp(self):
        self.folder = paths.run_root()/('safety_'+uuid.uuid4().hex[:10])
        self.folder.mkdir(parents=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(ROOT/'run.py'), *map(str,args)],
                              cwd=self.folder, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=90)

    def test_existing_run_rejected_without_overwrite(self):
        target = self.folder/'existing'
        target.mkdir()
        marker = target/'run_manifest.json'
        marker.write_text('{"fixture": "previous run"}', encoding='utf-8')
        before = paths.sha256(marker)
        result = self.cli('read_dem', '--run-dir', target)
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('already exists', result.stderr)
        self.assertEqual(paths.sha256(marker), before)

    def test_missing_raw_cli_records_failure(self):
        target = self.folder/'missing_input_run'
        result = self.cli('read_dem', '--raw-dir', self.folder/'absent_raw', '--run-dir', target)
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        self.assertIn('Missing input:', result.stderr)
        self.assertIn('--raw-dir', result.stderr)
        manifest = json.loads((target/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest['status'], manifest['exit_code']), ('failed', 1))
        self.assertEqual(manifest['outputs'], [])

    def test_import_launchers_has_no_output_side_effect(self):
        target = self.folder/'never_created'
        env = os.environ.copy()
        env['UAV_RUN_DIR'] = str(target)
        code = "import sys;sys.path.insert(0,sys.argv[1]);import project_paths,run;assert not project_paths.run_root().exists();print('NO_OUTPUT')"
        result = subprocess.run([sys.executable,'-B','-c',code,str(ROOT)],cwd=self.folder,
                                env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertFalse(target.exists())
        self.assertIn('NO_OUTPUT', result.stdout)

    def test_mutation_guard_with_disposable_reference_fixture(self):
        protected = self.folder/'fixture_reference'
        protected.mkdir()
        source = protected/'sentinel.txt'
        source.write_bytes(b'guard fixture only')
        incoming = self.folder/'incoming.txt'
        incoming.write_bytes(b'other guard fixture')
        # Exercise removal/rename/overwrite protection on disposable copies only.
        with patch.object(paths, 'REFERENCE', protected):
            for operation in (lambda: source.unlink(),
                              lambda: source.rename(self.folder/'moved.txt'),
                              lambda: os.replace(incoming,source)):
                with self.assertRaises(PermissionError):
                    operation()
        self.assertEqual(source.read_bytes(), b'guard fixture only')
        self.assertTrue(incoming.exists())

    def test_external_raw_override_is_readonly(self):
        protected = self.folder/'raw_fixture'
        protected.mkdir()
        source = protected/'input.txt'
        source.write_bytes(b'raw fixture only')
        with patch.dict(os.environ, {'UAV_RAW_DIR':str(protected)}):
            with self.assertRaises(PermissionError):
                source.write_bytes(b'forbidden')
        self.assertEqual(source.read_bytes(), b'raw fixture only')

    def test_unknown_cache_rejected_before_deserialization(self):
        import problem3_segment_communication_cache as cache
        unknown = self.folder/'untrusted.pkl.gz'
        unknown.write_bytes(b'not a cache; no pickle payload')
        with patch.dict(os.environ, {'UAV_CACHE_SHA256':''}), patch.object(cache.pickle, 'load') as deserialize:
            with self.assertRaisesRegex(ValueError, 'Untrusted pickle'):
                cache.load_cache(unknown)
            deserialize.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
