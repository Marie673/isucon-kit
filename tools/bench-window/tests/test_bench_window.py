import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "bench-window"
FIX = HERE / "fixtures"

loader = importlib.machinery.SourceFileLoader("bench_window", str(SCRIPT))
spec = importlib.util.spec_from_loader("bench_window", loader)
bw = importlib.util.module_from_spec(spec)
loader.exec_module(bw)

JST = timezone(timedelta(hours=9))


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=FIX)


class ParseTests(unittest.TestCase):
    def test_ltsv_line_with_aliases(self):
        d = bw.parse_line("time:2026-10-31T10:03:20+09:00\thost:1.2.3.4\tmethod:get\turi:/a?x=1\tstatus:200\treqtime:0.123\n")
        r = bw.to_rec(d, "h1", JST, None)
        self.assertEqual((r.host, r.method, r.path, r.status), ("h1", "GET", "/a", 200))
        self.assertAlmostEqual(r.rt, 0.123)
        self.assertAlmostEqual(r.t, datetime(2026, 10, 31, 10, 3, 20, tzinfo=JST).timestamp())

    def test_json_line_with_other_names(self):
        d = bw.parse_line('{"time_iso8601":"2026-10-31T10:03:20+09:00","request_method":"POST","request_uri":"/p/1?a=b","status":503,"request_time":1.5}')
        r = bw.to_rec(d, "h", JST, None)
        self.assertEqual((r.method, r.path, r.status, r.rt), ("POST", "/p/1", 503, 1.5))

    def test_request_field_and_time_local(self):
        d = {"time_local": "31/Oct/2026:10:03:20 +0900", "request": "GET /x?y=1 HTTP/1.1", "status": "404", "request_time": "0.010"}
        r = bw.to_rec(d, "h", JST, None)
        self.assertEqual((r.method, r.path), ("GET", "/x"))
        self.assertAlmostEqual(r.t, datetime(2026, 10, 31, 10, 3, 20, tzinfo=JST).timestamp())

    def test_msec_wins_and_naive_time_uses_tz(self):
        d = {"msec": "1730336600.250", "time_iso8601": "2000-01-01T00:00:00+00:00", "uri": "/", "method": "GET"}
        self.assertAlmostEqual(bw.to_rec(d, "h", JST, None).t, 1730336600.25)
        naive = bw.parse_time("2026-10-31 10:03:20", JST)
        self.assertAlmostEqual(naive, datetime(2026, 10, 31, 10, 3, 20, tzinfo=JST).timestamp())

    def test_garbage_line_is_none(self):
        self.assertIsNone(bw.parse_line("not a log line"))
        self.assertIsNone(bw.to_rec({"foo": "bar"}, "h", JST, None))

    def test_rel_and_percentile(self):
        self.assertEqual(bw.parse_rel("-5s"), -5)
        self.assertEqual(bw.parse_rel("+1m"), 60)
        self.assertEqual(bw.parse_rel("500ms"), 0.5)
        self.assertFalse(bw.is_rel("10:03:20"))
        self.assertEqual(bw.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 99), 10)
        self.assertEqual(bw.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50), 5)
        self.assertIsNone(bw.pct([], 50))

    def test_grouper_first_match_and_comment(self):
        g = bw.load_groups(str(FIX / "groups.txt"))
        self.assertEqual(g.key("/api/items/12"), "^/api/items/[0-9]+$")
        self.assertEqual(g.key("/api/items/12/notes"), "^/api/items/[0-9]+/notes$")
        self.assertEqual(g.key("/other"), "/other")


class CpuTests(unittest.TestCase):
    def setUp(self):
        self.day = datetime(2026, 10, 31).date()

    def test_mpstat_12h_and_banner(self):
        s = bw.load_cpu(str(FIX / "host1.mpstat.txt"), JST, self.day, None)
        self.assertTrue(all(h == "host1" for _, h, _ in s))
        t0 = datetime(2026, 10, 31, 10, 3, 20, tzinfo=JST).timestamp()
        v = [c for t, _, c in s if t == t0]
        self.assertEqual(len(v), 1)
        self.assertTrue(0 < v[0] < 40)

    def test_csv_with_header(self):
        s = bw.load_cpu(str(FIX / "host2.cpu.csv"), JST, self.day, None)
        self.assertEqual({h for _, h, _ in s}, {"host2"})
        self.assertEqual(len(s), 60)

    def test_pidstat_sums_and_normalizes(self):
        import tempfile
        txt = (
            "Linux 6.8.0 (web1) \t2026-10-31 \t_x86_64_\t(4 CPU)\n\n"
            "10:03:20      UID       PID    %usr %system  %guest   %wait    %CPU   CPU  Command\n"
            "10:03:21        0       100   120.00   20.00    0.00    0.00  140.00     1  app\n"
            "10:03:21        0       200    40.00    20.00    0.00    0.00   60.00     2  nginx\n"
            "10:03:22        0       100    10.00    10.00    0.00    0.00   20.00     1  app\n"
            "Average:        0       100    10.00    10.00    0.00    0.00   20.00     -  app\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pid.txt"
            p.write_text(txt)
            s = bw.load_cpu(str(p), JST, self.day, None)
        vals = sorted(c for _, _, c in s)
        self.assertEqual(vals, [5.0, 50.0])  # 200/4 と 20/4
        self.assertEqual({h for _, h, _ in s}, {"web1"})

    def test_pidstat_h_hash_header(self):
        # pidstat -u -h 1（isu-measure の形）。見出しが `#` で始まり、毎秒くり返す
        s = bw.load_cpu(str(FIX / "host1.pidstat-h.txt"), JST, self.day, None)
        self.assertEqual(len(s), 6)
        first = min(s)
        self.assertAlmostEqual(first[2], (45.0 + 12.0) / 2)  # 2 プロセスの合計 / 2 CPU
        self.assertEqual(datetime.fromtimestamp(first[0], JST).strftime("%H:%M:%S"), "10:03:20")
        self.assertEqual({h for _, h, _ in s}, {"host1"})

    def test_pidstat_h_epoch_time(self):
        s = bw.load_cpu(str(FIX / "host1.pidstat-epoch.txt"), JST, self.day, None)
        self.assertEqual([t for t, _, _ in s], [1761872600.0, 1761872601.0, 1761872602.0])
        self.assertEqual([c for _, _, c in s], [30.0, 40.0, 50.0])


class CliTests(unittest.TestCase):
    ARGS = ["host1.ltsv.log", "host2.json.log", "--anchor", "10:03:20", "--from", "+30s", "--to", "+60s", "--group", "groups.txt"]

    def test_window_counts_and_sections(self):
        r = run(*self.ARGS, "--cpu", "host1.mpstat.txt", "--cpu", "host2.cpu.csv")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("窓: 10:03:50.0 〜 10:04:20.0（30 秒", out)
        self.assertIn("窓の中 624 件、窓の外 272 件", out)
        for h in ("## 秒単位のタイムライン", "## エンドポイント別", "## 窓の中と外の比較", "ピーク"):
            self.assertIn(h, out)
        self.assertIn("cpu:", out)
        self.assertIn("`^/api/lists/[0-9]+$`", out)

    def test_peak_is_slower_than_outside(self):
        out = run(*self.ARGS).stdout
        line = next(ln for ln in out.splitlines() if "lists" in ln and "x**" in ln)
        ratio = float(line.split("**")[1].rstrip("x"))
        self.assertGreater(ratio, 2.0)

    def test_last_equals_anchor_window(self):
        a = run("host1.ltsv.log", "host2.json.log", "--last", "30s", "--timeline", "none", "--group", "groups.txt").stdout
        b = run(*self.ARGS, "--timeline", "none").stdout
        self.assertEqual(a.split("## エンドポイント別")[1], b.split("## エンドポイント別")[1])

    def test_long_format_and_bucket(self):
        out = run(*self.ARGS, "--timeline", "long", "--bucket", "10").stdout
        self.assertIn("| 時刻 | 相対 | 台 | 件数 |", out)
        self.assertIn("（10 秒ごと）", out)

    def test_labels(self):
        out = run("a=host1.ltsv.log", "e=host2.json.log", "--last", "5s").stdout
        self.assertIn("| 時刻 | 相対 | a | e | 合計 |", out)
        self.assertIn("a 526 行", out)
        out = run("host1.ltsv.log", "host2.json.log", "--host-label", "x", "--host-label", "y", "--last", "5s").stdout
        self.assertIn("| 時刻 | 相対 | x | y | 合計 |", out)

    def test_errors(self):
        self.assertEqual(run("host1.ltsv.log").returncode, 2)  # 窓の指定なし
        self.assertEqual(run("host1.ltsv.log", "--from", "-5s", "--to", "+5s").returncode, 2)  # 相対なのに anchor なし
        self.assertEqual(run("host1.ltsv.log", "--anchor", "11:00:00").returncode, 2)  # ログの外

    def test_window_outside_log_reports_empty(self):
        r = run("host1.ltsv.log", "--anchor", "10:03:20", "--from", "-300s", "--to", "-200s")
        self.assertEqual(r.returncode, 0)
        self.assertIn("窓の中にリクエストが無い", r.stdout)


if __name__ == "__main__":
    unittest.main()
