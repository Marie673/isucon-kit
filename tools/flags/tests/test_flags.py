import importlib.machinery
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "flags"
FIX = HERE / "fixtures"
NOW = "2026-10-31T18:00:00+09:00"

loader = importlib.machinery.SourceFileLoader("flags_mod", str(SCRIPT))
spec = importlib.util.spec_from_loader("flags_mod", loader)
fl = importlib.util.module_from_spec(spec)
loader.exec_module(fl)


def run(*args, cwd=FIX):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=cwd)


def lint(*extra):
    return run("lint", "--ledger", "flags.md", "--code", "code", "--env", "env", "--now", NOW, *extra)


class LedgerTests(unittest.TestCase):
    def test_parse_ledger(self):
        led = fl.parse_ledger((FIX / "flags.md").read_text(encoding="utf-8"))
        self.assertEqual(len(led.entries), 9)
        e = led.entries[0]
        self.assertEqual((e["name"], e["default"], e["prod"], e["status"]), ("ISU_FAST_FEED", "0", "1", "有効"))
        self.assertEqual(e["line"], 5)
        self.assertEqual(led.entries[1]["remove_when"], "")

    def test_english_headers_and_escaped_pipe(self):
        text = "| name | default | prod | purpose | evidence | added | remove_when | status |\n|---|---|---|---|---|---|---|---|\n| `A_FLAG` | 0 | 1 | a \\| b | PR | 12:00 | x | trial |\n"
        led = fl.parse_ledger(text)
        self.assertEqual(led.entries[0]["purpose"], "a | b")
        self.assertEqual(led.entries[0]["status"], "trial")

    def test_parse_added(self):
        now = datetime(2026, 10, 31, 18, 0, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(fl.parse_added("2026-10-31 14:05", now).hour, 14)
        self.assertEqual(fl.parse_added("17:30", now).minute, 30)
        self.assertEqual(fl.parse_added("19:00", now).day, 30)  # 未来の時刻は前日
        self.assertIsNone(fl.parse_added("さっき", now))

    def test_norm_value(self):
        self.assertEqual(fl.norm_value('"1"'), "1")
        self.assertEqual(fl.norm_value("`on`"), "on")
        self.assertEqual(fl.norm_value("-"), "")
        self.assertEqual(fl.norm_value("未設定"), "")


class ScanTests(unittest.TestCase):
    def test_code_refs_all_languages(self):
        refs = fl.scan_code([str(FIX / "code")], fl.DEFAULT_PATTERN, set(), set())
        for name in ("ISU_FAST_FEED", "ISU_SIG_BATCH", "ISU_UNLISTED_FLAG", "ISU_NODE_FLAG", "DATABASE_URL",
                     "ISU_PY_FLAG", "ISU_PY_OLD", "ISU_GO_FLAG", "ISU_SH_WORKERS", "HOME"):
            self.assertIn(name, refs)
        self.assertEqual(refs["ISU_FAST_FEED"][0][1], 4)

    def test_env_refs(self):
        refs = fl.scan_env([str(FIX / "env")], fl.DEFAULT_PATTERN, set())
        self.assertEqual({n: [v for _, _, v in r] for n, r in refs.items()},
                         {"ISU_FAST_FEED": ["1"], "ISU_SIG_BATCH": ["1"], "ISU_ORPHAN_ENV": ["1"],
                          "ISU_NODE_FLAG": ["on"], "ISU_PY_FLAG": ["2"]})

    def test_custom_pattern(self):
        refs = fl.scan_code([str(FIX / "code")], r"ISU_PY_[A-Z]+", set(), set())
        self.assertEqual(set(refs), {"ISU_PY_FLAG", "ISU_PY_OLD"})


class LintCliTests(unittest.TestCase):
    def test_findings(self):
        r = lint()
        self.assertEqual(r.returncode, 1, r.stderr)
        out = r.stdout
        sections = {
            "## 台帳に無いフラグ（3）": ["ISU_UNLISTED_FLAG", "ISU_ORPHAN_ENV", "DATABASE_URL"],
            "## コードに無いのに台帳にある（1）": ["ISU_DEAD_FLAG"],
            "## 削除済みなのに残っている（1）": ["ISU_PY_OLD"],
            "## 消す条件が空（1）": ["ISU_SIG_BATCH"],
            "## 試験中のまま 3 時間を超えた（2）": ["ISU_SIG_BATCH", "ISU_NODE_FLAG"],
            "## 台帳の本番の値と設定ファイルが合わない（2）": ["ISU_NODE_FLAG", "ISU_DEAD_FLAG"],
        }
        for head, names in sections.items():
            self.assertIn(head, out)
            body = out.split(head)[1].split("\n## ")[0]
            for n in names:
                self.assertIn(f"`{n}`", body, (head, n))
        # 問題のあるフラグだけが出て、ISU_PY_FLAG（台帳・コード・設定が合っている）は出ない
        self.assertNotIn("`ISU_PY_FLAG`", out)
        # HOME は既定の無視リストで出ない
        self.assertNotIn("`HOME`", out)

    def test_ignore_and_trial_hours(self):
        out = lint("--ignore", "^DATABASE_", "--trial-hours", "4.5").stdout
        self.assertNotIn("DATABASE_URL", out)
        self.assertIn("## 試験中のまま 4.5 時間を超えた（1）", out)
        self.assertIn("`ISU_SIG_BATCH` — 試験中のまま 4.7", out)

    def test_code_only_skips_env_checks(self):
        r = run("lint", "--ledger", "flags.md", "--code", "code", "--now", NOW)
        self.assertNotIn("設定ファイルが合わない", r.stdout)
        self.assertNotIn("ISU_ORPHAN_ENV", r.stdout)

    def test_clean_exit_zero(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "code").mkdir()
            (d / "code" / "a.py").write_text('import os\nX = os.environ.get("ISU_ONLY_FLAG")\n')
            (d / "env").mkdir()
            (d / "env" / "a.env").write_text("ISU_ONLY_FLAG=1\n")
            (d / "flags.md").write_text(
                "| 名前 | 既定値 | 本番の値 | 目的 | 根拠 | 入れた時刻 | 消す条件 | 状態 |\n|---|---|---|---|---|---|---|---|\n"
                "| `ISU_ONLY_FLAG` | 0 | 1 | 速くする | PR #1 | 2026-10-31 17:00 | 恒久化したら | 試験中 |\n")
            r = run("lint", "--ledger", "flags.md", "--code", "code", "--env", "env", "--now", NOW, cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("問題なし", r.stdout)

    def test_needs_code_or_env(self):
        self.assertEqual(run("lint", "--ledger", "flags.md").returncode, 2)
        self.assertEqual(run("lint", "--ledger", "nope.md", "--code", "code").returncode, 1)

    def test_list(self):
        r = run("list", "--ledger", "flags.md", "--code", "code", "--env", "env")
        self.assertEqual(r.returncode, 0, r.stderr)
        line = next(ln for ln in r.stdout.splitlines() if "`ISU_PY_FLAG`" in ln)
        self.assertIn("| 有効 | 1 | 2 | 2 | 1 |", line)


class NewTests(unittest.TestCase):
    def test_new_creates_from_template_and_appends(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            r = run("new", "ISU_NEW_FLAG", "--default", "0", "--purpose", "a | b", "--added", "2026-10-31 12:00", "--ledger", "flags.md", cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("消す条件・根拠が空", r.stderr)
            led = fl.parse_ledger((d / "flags.md").read_text(encoding="utf-8"))
            self.assertEqual([e["name"] for e in led.entries], ["ISU_NEW_FLAG"])  # 雛形の列の説明の表は台帳として読まれない
            self.assertEqual(led.entries[0]["purpose"], "a | b")
            self.assertEqual(led.entries[0]["status"], "試験中")
            r = run("new", "ISU_SECOND", "--remove-when", "済んだら", "--ledger", "flags.md", cwd=d)
            led = fl.parse_ledger((d / "flags.md").read_text(encoding="utf-8"))
            self.assertEqual([e["name"] for e in led.entries], ["ISU_NEW_FLAG", "ISU_SECOND"])
            # 重複・名前の形
            self.assertEqual(run("new", "ISU_SECOND", "--ledger", "flags.md", cwd=d).returncode, 1)
            self.assertEqual(run("new", "bad", "--ledger", "flags.md", cwd=d).returncode, 2)

    def test_new_keeps_other_text(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shutil.copy(FIX / "flags.md", d / "flags.md")
            (d / "flags.md").write_text((d / "flags.md").read_text() + "\n末尾のメモ\n")
            self.assertEqual(run("new", "ISU_ADDED", "--ledger", "flags.md", cwd=d).returncode, 0)
            text = (d / "flags.md").read_text()
            self.assertTrue(text.rstrip().endswith("末尾のメモ"))
            self.assertLess(text.index("`ISU_ADDED`"), text.index("末尾のメモ"))
            self.assertEqual(len(fl.parse_ledger(text).entries), 10)

    def test_template_has_no_rows(self):
        led = fl.parse_ledger((HERE.parent / "flags.template.md").read_text(encoding="utf-8"))
        self.assertEqual(led.entries, [])
        self.assertIsNotNone(led.span)


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "hosts/isu1/etc/mysql").mkdir(parents=True)
        (self.tmp / "docs").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, rel, text):
        (self.tmp / rel).write_text(text, encoding="utf-8")

    def durability(self):
        return run("durability", "--conf", "hosts", "--conf", "webapp", "--decisions", "docs/DECISIONS.md", cwd=self.tmp)

    def test_detects_weakening_settings(self):
        cases = {
            "innodb_flush_log_at_trx_commit = 2": "innodb_flush_log_at_trx_commit",
            "  command: mysqld --innodb-flush-log-at-trx-commit=0": "innodb_flush_log_at_trx_commit",
            "sync_binlog=0": "sync_binlog",
            "skip-innodb-doublewrite": "innodb_doublewrite",
            "innodb_doublewrite = OFF": "innodb_doublewrite",
            "fsync = off": "fsync",
            "ALTER SYSTEM SET synchronous_commit TO 'off';": "synchronous_commit",
            "full_page_writes = off": "full_page_writes",
            "appendfsync no": "appendfsync",
            "    tmpfs: /var/lib/mysql": "tmpfs",
        }
        for line, name in cases.items():
            self.write("hosts/isu1/etc/mysql/x.cnf", line + "\n")
            hits = fl.scan_durability([str(self.tmp / "hosts")], set())
            self.assertEqual([h[0] for h in hits], [name], line)

    def test_ignores_safe_values_and_comments(self):
        self.write("hosts/isu1/etc/mysql/x.cnf", "\n".join([
            "innodb_flush_log_at_trx_commit = 1",
            "sync_binlog = 1",
            "# innodb_flush_log_at_trx_commit = 2",
            "synchronous_commit = remote_apply",
            "-- SET fsync = off",
            "disable-log-bin",
        ]) + "\n")
        self.assertEqual(fl.scan_durability([str(self.tmp / "hosts")], set()), [])

    def test_blocks_until_recorded(self):
        self.write("hosts/isu1/etc/mysql/x.cnf", "innodb_flush_log_at_trx_commit = 2\n")
        r = self.durability()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("決まりの記録が無い", r.stdout)
        self.assertIn("x.cnf:1", r.stdout)
        self.write("docs/DECISIONS.md", "- 14:05 innodb_flush_log_at_trx_commit=2 を採る（人間が承認。再起動試験 OK）\n")
        r = self.durability()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("記録あり", r.stdout)

    def test_clean_tree(self):
        self.write("hosts/isu1/etc/mysql/x.cnf", "max_connections = 1000\n")
        r = self.durability()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("問題なし", r.stdout)


if __name__ == "__main__":
    unittest.main()
