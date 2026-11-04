"""cpu-features の単体テスト。ネットワーク・サーバーには触らない。

    uv run --no-project python -m unittest discover -s tests
"""
import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
_loader = importlib.machinery.SourceFileLoader("cpu_features", str(HERE.parent / "cpu-features"))
_spec = importlib.util.spec_from_loader("cpu_features", _loader)
cf = importlib.util.module_from_spec(_spec)
sys.modules["cpu_features"] = cf
_loader.exec_module(cf)


def load(name, label=None):
    return cf.parse_text((FIX / name).read_text(), label or name)


class ParseTest(unittest.TestCase):
    def test_xeon_8124m_has_no_sha_ni(self):
        h = load("xeon-8124m.cpuinfo", "host1")
        self.assertEqual(h.arch, cf.X86)
        self.assertEqual(h.logical, 2)
        self.assertEqual(h.cores, 1)
        self.assertIn("8124M", h.model)
        self.assertIs(h.feature("sha_ni"), False)
        self.assertIs(h.feature("aes"), True)
        self.assertIs(h.feature("avx2"), True)
        self.assertIs(h.feature("avx512bw"), True)
        self.assertIs(h.feature("vaes"), False)
        self.assertIs(h.feature("neon"), None)  # x86 には無い機能は n/a

    def test_sapphire_rapids(self):
        h = load("sapphire-rapids.cpuinfo")
        for f in ("sha_ni", "vaes", "vpclmulqdq", "gfni", "avx512vbmi", "avx512bw", "bmi2"):
            self.assertIs(h.feature(f), True, f)

    def test_lscpu_matches_cpuinfo(self):
        a = load("xeon-8124m.cpuinfo")
        b = load("xeon-8124m.lscpu")
        self.assertEqual(cf.detect_format((FIX / "xeon-8124m.lscpu").read_text()), "lscpu")
        self.assertEqual(a.flags, b.flags)
        self.assertEqual((a.logical, a.cores), (b.logical, b.cores))
        self.assertEqual(b.mhz, 3000)  # モデル名の @ 3.00GHz から

    def test_graviton_lscpu(self):
        h = load("graviton3.lscpu")
        self.assertEqual(h.arch, cf.ARM)
        self.assertEqual(h.model, "Neoverse-V1")
        self.assertIs(h.feature("sha_ni"), True)  # sha2
        self.assertIs(h.feature("pclmulqdq"), True)  # pmull
        self.assertIs(h.feature("neon"), True)  # asimd
        self.assertIs(h.feature("avx2"), None)

    def test_apple_sysctl(self):
        h = load("apple-m.sysctl")
        self.assertEqual(cf.detect_format((FIX / "apple-m.sysctl").read_text()), "sysctl")
        self.assertEqual(h.arch, cf.ARM)
        self.assertEqual(h.model, "Apple M2")
        self.assertEqual(h.logical, 8)
        for f in ("sha_ni", "aes", "pclmulqdq", "crc32", "neon"):
            self.assertIs(h.feature(f), True, f)
        self.assertIsNone(h.feature("avx512f"))

    def test_intel_mac_sysctl(self):
        text = (
            "machdep.cpu.brand_string: Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz\n"
            "machdep.cpu.features: FPU VME SSE4.2 AES PCLMULQDQ AVX1.0\n"
            "machdep.cpu.leaf7_features: SMEP BMI1 AVX2 BMI2 SHA\n"
            "hw.ncpu: 16\nhw.physicalcpu: 8\n"
        )
        h = cf.parse_text(text, "intelmac")
        self.assertEqual(h.arch, cf.X86)
        for f in ("sse4_2", "aes", "pclmulqdq", "avx2", "bmi2", "sha_ni"):
            self.assertIs(h.feature(f), True, f)
        self.assertEqual(h.mhz, 2400)

    def test_unknown_format(self):
        with self.assertRaises(cf.ParseError):
            cf.parse_text("hello\nworld\n", "x")

    def test_cpuinfo_without_flags(self):
        with self.assertRaises(cf.ParseError):
            cf.parse_text("processor\t: 0\nmodel name\t: foo\n", "x")

    def test_arm_cpuinfo(self):
        text = (
            "processor\t: 0\nBogoMIPS\t: 2100.00\n"
            "Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32\n"
            "CPU implementer\t: 0x41\nCPU part\t: 0xd0c\n"
        )
        h = cf.parse_text(text, "arm")
        self.assertEqual(h.arch, cf.ARM)
        self.assertEqual(h.model, "Neoverse-N1")
        self.assertIs(h.feature("sha_ni"), True)


class AnalyzeTest(unittest.TestCase):
    def hosts(self):
        return [
            load("xeon-8124m.cpuinfo", "host1"),
            load("sapphire-rapids.cpuinfo", "host2"),
        ]

    def test_warns_when_servers_differ(self):
        res = cf.analyze(self.hosts())
        self.assertEqual(len(res["warnings"]), 1)
        w = res["warnings"][0]
        self.assertIn("SHA", w)
        self.assertIn("host1", w)
        self.assertIn("sha_ni", w)
        self.assertIn("avx2=あり", w)  # マルチバッファの手がかり
        self.assertEqual(res["common_missing"], [])

    def test_no_warning_when_servers_same(self):
        hs = [load("sapphire-rapids.cpuinfo", "a"), load("sapphire-rapids.cpuinfo", "b")]
        res = cf.analyze(hs)
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["common_missing"], [])
        self.assertEqual(res["local_warnings"], [])

    def test_common_missing(self):
        hs = [load("xeon-8124m.cpuinfo", "a"), load("xeon-8124m.lscpu", "b")]
        res = cf.analyze(hs)
        self.assertEqual(res["warnings"], [])
        self.assertEqual(len(res["common_missing"]), 1)

    def test_local_arm_vs_x86_servers(self):
        local = load("apple-m.sysctl", "local")
        local.is_local = True
        res = cf.analyze(self.hosts() + [local])
        joined = "\n".join(res["local_warnings"])
        self.assertIn("arm64", joined)
        self.assertIn("NEON", joined)
        # SHA の効き目が逆: 手元は速い、host1 は遅い
        self.assertIn("本番と逆", joined)
        self.assertIn("host1", joined)

    def test_local_not_counted_among_servers(self):
        local = load("apple-m.sysctl", "local")
        local.is_local = True
        res = cf.analyze([load("sapphire-rapids.cpuinfo", "a"), local])
        self.assertEqual(res["warnings"], [])  # 台どうしは同じ


class RenderTest(unittest.TestCase):
    def test_markdown_sections_and_cells(self):
        hs = [load("xeon-8124m.cpuinfo", "host1"), load("sapphire-rapids.cpuinfo", "host2"), load("apple-m.sysctl", "mac")]
        md = cf.render_markdown(hs)
        self.assertTrue(md.startswith("# CPU 機能の比較"))
        self.assertLess(md.index("## 警告: 台によって速さが変わる処理"), md.index("## 台"))
        self.assertIn("| 機能 | host1 | host2 | mac | 意味 |", md)
        row = [l for l in md.splitlines() if l.startswith("| sha_ni |")][0]
        self.assertIn("| **no** | yes | yes |", row)
        avx = [l for l in md.splitlines() if l.startswith("| avx2 |")][0]
        self.assertIn("| yes | yes | n/a |", avx)
        self.assertIn("**遅い**（sha_ni なし）", md)

    def test_markdown_escapes_pipes(self):
        h = load("apple-m.sysctl", "x|y")
        self.assertIn("x\\|y", cf.render_markdown([h]))

    def test_json(self):
        hs = [load("xeon-8124m.cpuinfo", "host1"), load("sapphire-rapids.cpuinfo", "host2")]
        data = json.loads(cf.render_json(hs))
        self.assertEqual(data["hosts"][0]["features"]["sha_ni"], False)
        self.assertEqual(data["hosts"][1]["features"]["sha_ni"], True)
        self.assertTrue(data["warnings"])


class LocalTest(unittest.TestCase):
    def test_darwin_uses_sysctl_even_if_nonzero_exit(self):
        out = (FIX / "apple-m.sysctl").read_text()
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, stdout=out, stderr="unknown oid")

        h = cf.read_local(system="Darwin", run=fake_run)
        self.assertEqual(calls[0][0], "sysctl")
        self.assertTrue(h.is_local)
        self.assertEqual(h.label, "local")
        self.assertEqual(h.arch, cf.ARM)

    def test_linux_reads_proc_cpuinfo(self):
        seen = []

        def fake_read(path):
            seen.append(path)
            return (FIX / "xeon-8124m.cpuinfo").read_text()

        h = cf.read_local(system="Linux", read=fake_read)
        self.assertEqual(seen, ["/proc/cpuinfo"])
        self.assertTrue(h.is_local)

    def test_other_os(self):
        with self.assertRaises(cf.ParseError):
            cf.read_local(system="Windows")


class CliTest(unittest.TestCase):
    def run_main(self, argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old = cf.sys.stdin
        if stdin is not None:
            cf.sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    rc = cf.main(argv)
                except SystemExit as e:
                    rc = e.code
        finally:
            cf.sys.stdin = old
        return rc, out.getvalue(), err.getvalue()

    def test_label_and_file(self):
        rc, out, _ = self.run_main(["host1=%s" % (FIX / "xeon-8124m.cpuinfo"), "host2=%s" % (FIX / "sapphire-rapids.cpuinfo")])
        self.assertEqual(rc, 0)
        self.assertIn("host1", out)
        self.assertIn("host2", out)

    def test_default_label_is_file_stem(self):
        rc, out, _ = self.run_main([str(FIX / "graviton3.lscpu")])
        self.assertEqual(rc, 0)
        self.assertIn("| graviton3 |", out)

    def test_stdin(self):
        rc, out, _ = self.run_main(["host1=-"], stdin=(FIX / "xeon-8124m.cpuinfo").read_text())
        self.assertEqual(rc, 0)
        self.assertIn("host1", out)

    def test_missing_file(self):
        rc, _, err = self.run_main(["/no/such/file"])
        self.assertEqual(rc, 2)
        self.assertIn("ファイルが無い", err)

    def test_garbage_input(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
            f.write("nothing here\n")
            f.flush()
            rc, _, err = self.run_main([f.name])
        self.assertEqual(rc, 2)
        self.assertIn("形式を判別できない", err)

    def test_duplicate_labels(self):
        p = str(FIX / "xeon-8124m.cpuinfo")
        rc, _, err = self.run_main(["a=%s" % p, "a=%s" % p])
        self.assertEqual(rc, 2)
        self.assertIn("重複", err)

    def test_no_args(self):
        rc, _, _ = self.run_main([])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
