import importlib.util
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('isu_runtime',Path(__file__).resolve().parents[1]/'isu_runtime.py')
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)

class RuntimeTests(unittest.TestCase):
    def kit(self, **env):
        k=runtime.Kit.__new__(runtime.Kit);k.env=env;k.config=Path('/unused/config')
        return k
    def test_deadline_last_minute_and_scp(self):
        k=self.kit(ISU_END_AT=str(time.time()+30))
        with self.assertRaisesRegex(RuntimeError,'60秒前'):k.guard_time()
        with patch.object(runtime,'run') as call:
            with self.assertRaises(RuntimeError):k.scp('a','host:a')
            call.assert_not_called()
    def test_remote_ssh_has_n_without_script_stdin(self):
        k=self.kit(ISU_IP_host='127.0.0.1')
        with patch.object(runtime.sp,'run',return_value=subprocess.CompletedProcess([],0,stdout='')) as call:
            k.ssh('host','echo 1')
            self.assertIn('-n',call.call_args.args[0])
            k.script('host','echo 1')
            self.assertNotIn('-n',call.call_args.args[0])
            self.assertEqual(call.call_args.kwargs['input'],'echo 1')
    def test_metadata_cannot_execute_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'meta.env'
            values={'NOTE':"$(touch SHOULD_NOT_EXIST) ' quote\nnext",'SCORE':'0'}
            runtime.write_meta(p,values)
            self.assertEqual(runtime.read_meta(p)['NOTE'],values['NOTE'].replace('\n',' '))
            self.assertFalse((Path.cwd()/'SHOULD_NOT_EXIST').exists())
    def test_iso_offsets_and_http_defaults(self):
        self.assertEqual(runtime.epoch('2026-01-01T09:00:00+09:00'),runtime.epoch('2026-01-01T00:00:00Z'))
        k=self.kit(ISU_TARGET_HOST='app.test',ISU_TARGET_SCHEME='http')
        self.assertIn('app.test:80:127.0.0.1',k.health_command())
    def test_wait_padded_wc_does_not_write_result(self):
        k=self.kit(ISU_NGINX_HOST='host',ISU_ACCESS_LOG='/access.log',ISU_IDLE_RPS='5',ISU_IDLE_SECS='1')
        with tempfile.TemporaryDirectory() as tmp:
            k.results=Path(tmp)
            responses=[subprocess.CompletedProcess([],0,stdout='       '+n+'\n') for n in ['0','8','8']]
            with patch.object(k,'ssh',side_effect=responses),patch.object(runtime.time,'sleep'):
                k.measure('wait','run')
            self.assertFalse((k.results/'run/meta.env').exists())
