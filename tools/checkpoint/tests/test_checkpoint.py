import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
loader=importlib.machinery.SourceFileLoader('checkpoint_module',str(Path(__file__).resolve().parents[1]/'checkpoint'))
spec=importlib.util.spec_from_loader(loader.name,loader);cp=importlib.util.module_from_spec(spec);loader.exec_module(cp)
class CheckpointTests(unittest.TestCase):
    def test_end_at_drives_schedule(self):
        times={id:t for t,id,_ in cp.schedule(100,20000)}
        self.assertEqual(times['reboot'],12800)
        self.assertEqual(times['final'],16400)
        self.assertEqual(times['review'],9200)
    def test_without_end_has_only_initial_checkpoints(self):
        self.assertEqual(len(cp.schedule(100)),4)
