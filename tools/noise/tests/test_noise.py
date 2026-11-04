import importlib.machinery
import importlib.util
import math
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "noise"
FIX = HERE / "fixtures"

loader = importlib.machinery.SourceFileLoader("noise_mod", str(SCRIPT))
spec = importlib.util.spec_from_loader("noise_mod", loader)
nz = importlib.util.module_from_spec(spec)
loader.exec_module(nz)


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=FIX)


class StatsTests(unittest.TestCase):
    def test_stats(self):
        s = nz.stats_of([10, 12, 14])
        self.assertEqual((s.n, s.mean, s.lo, s.hi, s.span), (3, 12, 10, 14, 4))
        self.assertAlmostEqual(s.sd, 2.0)
        self.assertAlmostEqual(s.cv, 2 / 12)

    def test_single_has_no_sd(self):
        s = nz.stats_of([5.0])
        self.assertIsNone(s.sd)
        self.assertIsNone(s.cv)
        self.assertIsNone(nz.stats_of([]))

    def test_parse_score_and_pass(self):
        self.assertEqual(nz.parse_score("10,120"), 10120)
        self.assertIsNone(nz.parse_score("?"))
        self.assertIsNone(nz.parse_score("nan"))
        self.assertFalse(nz.parse_pass("FAIL"))
        self.assertFalse(nz.parse_pass(False))
        self.assertTrue(nz.parse_pass(""))
        self.assertTrue(nz.parse_pass("pass"))


class LoadTests(unittest.TestCase):
    def test_csv_jsonl(self):
        csv_rows = nz.load_rows(str(FIX / "scores.csv"))
        self.assertEqual(len(csv_rows), 17)
        self.assertFalse(csv_rows[5].passed)
        jl = nz.load_rows(str(FIX / "scores.jsonl"))
        self.assertEqual([r.passed for r in jl], [True, True, True, True, False])
        self.assertEqual(jl[0].label, "baseline")

    def test_scoreboard_markdown(self):
        rows = nz.load_rows(str(FIX / "SCOREBOARD.md"))
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0].id, "20261031-100500")  # [id](id/SUMMARY.md) のリンク文字列
        self.assertEqual(rows[0].score, 10120)
        self.assertEqual(rows[0].commit, "a1b2c3d")
        self.assertIsNone(rows[-1].score)  # `?`
        self.assertTrue(all(r.passed for r in rows))

    def test_label_from_note(self):
        rows = nz.apply_label_from_note(nz.load_rows(str(FIX / "SCOREBOARD.md")), r"^(\S+)")
        self.assertEqual([r.label for r in rows[:4]], ["baseline", "baseline", "baseline", "nginx-keepalive"])
        whole = nz.apply_label_from_note(nz.load_rows(str(FIX / "SCOREBOARD.md")), r"run\d")
        self.assertEqual(whole[0].label, "run1")

    def test_ids_selection(self):
        rows = nz.load_rows(str(FIX / "SCOREBOARD.md"))
        chosen, _, _ = nz.select(rows, "ids=20261031-100500..20261031-102000")
        self.assertEqual([r.score for r in chosen], [10120, 9340, 10480])
        chosen, _, _ = nz.select(rows, "ids=20261031-100500+20261031-103000")
        self.assertEqual(len(chosen), 2)
        with self.assertRaises(ValueError):
            nz.select(rows, "ids=nope")

    def test_select_failed_and_unrecorded(self):
        rows = nz.load_rows(str(FIX / "scores.csv"))
        chosen, failed, unrec = nz.select(rows, "baseline")
        self.assertEqual((len(chosen), failed, unrec), (5, 1, 0))
        chosen, failed, _ = nz.select(rows, "baseline", include_failed=True)
        self.assertEqual(len(chosen), 6)
        chosen, _, _ = nz.select(rows, "9c8d")  # label に無ければ commit の前方一致
        self.assertEqual(len(chosen), 3)
        chosen, _, _ = nz.select(rows, "label=baseline,commit=a1b2")
        self.assertEqual(len(chosen), 5)


class CliTests(unittest.TestCase):
    def test_band(self):
        r = run("-f", "scores.csv", "band", "--label", "baseline")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| 5 | 10,088 | 537.09 | 9,340 | 10,690 | 1,350 | 5.3% |", r.stdout)

    def test_band_all_labels(self):
        out = run("-f", "scores.csv", "band").stdout
        for lab in ("baseline", "tune-a", "tune-b", "single"):
            self.assertIn(f"| {lab} |", out)

    def test_compare_verdicts_and_exit_codes(self):
        cases = [("baseline", "tune-a", 0, "差なし"), ("baseline", "tune-b", 10, "良い"),
                 ("baseline", "tune-c", 11, "悪い"), ("baseline", "single", 12, "本数不足"),
                 ("baseline", "nothing", 12, "本数不足")]
        for a, b, code, word in cases:
            r = run("-f", "scores.csv", "compare", a, b)
            self.assertEqual(r.returncode, code, (a, b, r.stdout, r.stderr))
            self.assertIn(f"**判定: {word}**", r.stdout)

    def test_lower_is_better_flips(self):
        r = run("-f", "scores.csv", "compare", "baseline", "tune-b", "--lower-is-better")
        self.assertEqual(r.returncode, 11)

    def test_compare_identical_groups_is_same(self):
        r = run("-f", "scores.csv", "compare", "baseline", "baseline")
        self.assertEqual(r.returncode, 0)

    def test_options_after_subcommand(self):
        r = run("band", "-f", "scores.jsonl", "--label", "tune-b", "--include-failed")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| 2 | 12,750 |", r.stdout)

    def test_scoreboard_with_label_from_note(self):
        r = run("-f", "SCOREBOARD.md", "--label-from-note", r"^(\S+)", "compare", "baseline", "index-feeds")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("**判定: 良い**", r.stdout)
        # 点が未記録の行（`?`）は除かれ、失格にはならない
        band = run("-f", "SCOREBOARD.md", "--label-from-note", r"^(\S+)", "band").stdout
        self.assertRegex(band, r"\| index-feeds \| 3 \|.*\| 0 \| 1 \|")

    def test_scoreboard_with_ids(self):
        r = run("-f", "SCOREBOARD.md", "compare", "ids=20261031-100500..20261031-102000", "ids=20261031-105400..20261031-111000")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)

    def test_errors(self):
        self.assertEqual(run("-f", "nope.csv", "band").returncode, 1)
        self.assertEqual(run("-f", "scores.csv", "compare", "ids=zzz", "baseline").returncode, 1)
        self.assertEqual(run("-f", "scores.csv", "band", "--label", "ない").returncode, 1)


if __name__ == "__main__":
    unittest.main()
