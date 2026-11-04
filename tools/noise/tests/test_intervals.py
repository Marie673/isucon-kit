import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
loader=importlib.machinery.SourceFileLoader('noise_intervals',str(Path(__file__).resolve().parents[1]/'noise'))
spec=importlib.util.spec_from_loader(loader.name,loader);nz=importlib.util.module_from_spec(spec);loader.exec_module(nz)
class IntervalTests(unittest.TestCase):
    def test_eight_runs_detect_twelve_point_shift(self):
        a=[90,95,97,99,101,103,105,110];b=[v+12 for v in a]
        lo,hi,df=nz.mean_interval(a,b)
        self.assertGreater(lo,0);self.assertLess(hi,24)
        small=nz.mean_interval(a[:2],b[:2])
        self.assertGreater(small[1]-small[0],hi-lo)
    def test_paired_difference_and_mismatched_counts(self):
        a=[90,100,110,95];b=[v+12 for v in a]
        self.assertEqual(nz.mean_interval(a,b,True)[:2],(12,12))
        with self.assertRaises(ValueError):nz.mean_interval(a,b[:2],True)
    def test_scoreboard_result_alias(self):
        rows=nz.rows_from_dicts([{'score':10,'result':'fail','label':'base'},{'score':20,'result':'pass','label':'base'}])
        selected,failed,_=nz.select(rows,'base')
        self.assertEqual(len(selected),1);self.assertEqual(failed,1)
    def test_t_table(self):
        self.assertEqual(nz.t95(1),12.706)
        self.assertAlmostEqual(nz.t95(11),(2.228+2.179)/2)
