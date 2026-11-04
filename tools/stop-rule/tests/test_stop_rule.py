import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import unittest
path=Path(__file__).resolve().parents[1]/'stop-rule'
loader=importlib.machinery.SourceFileLoader('stop_module',str(path));spec=importlib.util.spec_from_loader(loader.name,loader);st=importlib.util.module_from_spec(spec);loader.exec_module(st)
class StopRuleTests(unittest.TestCase):
    def test_expected_thresholds_include_failure(self):
        v=st.thresholds([0,100,200],3)
        self.assertEqual(v[1],100)
        self.assertAlmostEqual(v[2],400/3)
        self.assertGreater(v[3],v[2])
    def test_stop_and_continue(self):
        for score,code in [('200',0),('0',10)]:
            p=subprocess.run([str(path),'--scores','0,100,200','--remaining','2','--last',score],capture_output=True,text=True)
            self.assertEqual(p.returncode,code,p.stderr)
