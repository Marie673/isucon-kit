"""lab-aws の単体テスト。AWS・ssh・rsync は呼ばない（runner を差し替えて、組み立てたコマンドだけを確かめる）。

    uv run --no-project python -m unittest discover -s tests
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_loader = importlib.machinery.SourceFileLoader("lab_aws", str(HERE.parent / "lab-aws"))
_spec = importlib.util.spec_from_loader("lab_aws", _loader)
lab = importlib.util.module_from_spec(_spec)
sys.modules["lab_aws"] = lab
_loader.exec_module(lab)

NOW = 1_800_000_000.0
BASE_ENV = {
    "LAB_MAX_INSTANCES": "2",
    "LAB_KEY_NAME": "k",
    "LAB_SG_ID": "sg-0123",
    "LAB_SSH_KEY": "/keys/lab.pem",
    "AWS_PROFILE": "p",
    "AWS_REGION": "r-1",
}


def iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def instance(iid, labid="lab-a", idx=1, state="running", expires=NOW + 3600, launch=NOW - 600,
             ip="203.0.113.%d", purpose="isucon-lab", itype="c5.large"):
    tags = [{"Key": "purpose", "Value": purpose}, {"Key": "lab-id", "Value": labid},
            {"Key": "lab-node", "Value": str(idx)}]
    if expires is not None:
        tags.append({"Key": "lab-expires", "Value": str(int(expires))})
    return {"InstanceId": iid, "State": {"Name": state}, "InstanceType": itype, "LaunchTime": iso(launch),
            "PublicIpAddress": ip % idx if "%d" in ip else ip, "PrivateIpAddress": "10.0.0.%d" % idx, "Tags": tags}


def describe_doc(*instances):
    return json.dumps({"Reservations": [{"Instances": list(instances)}]})


class FakeRunner:
    """呼ばれた argv を記録する。aws の describe には describe_doc を返す。"""

    def __init__(self, describes=None, run_ids=None):
        self.calls = []
        self.describes = list(describes or [])
        self.run_ids = list(run_ids or [])

    def __call__(self, argv, capture=True, check=False, stdout_path=None):
        self.calls.append({"argv": list(argv), "stdout_path": stdout_path, "capture": capture})
        if argv[0] != "aws":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        op = argv[[i for i, x in enumerate(argv) if x == "ec2"][0] + 1] if "ec2" in argv else argv[-1]
        if op == "describe-instances":
            doc = self.describes.pop(0) if self.describes else describe_doc()
            return subprocess.CompletedProcess(argv, 0, stdout=doc, stderr="")
        if op == "run-instances":
            return subprocess.CompletedProcess(argv, 0, stdout=self.run_ids.pop(0) + "\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def aws_ops(self):
        out = []
        for c in self.calls:
            a = c["argv"]
            if a[0] == "aws" and "ec2" in a:
                out.append(a[a.index("ec2") + 1])
        return out


def run(argv, env=None, runner=None, tmp=None):
    out, err = [], []
    runner = runner or FakeRunner()
    cwd = Path(tmp or tempfile.mkdtemp())
    e = dict(BASE_ENV if env is None else env)
    e.setdefault("LAB_STATE_DIR", str(cwd / "state"))
    rc = lab.main(argv, env=e, cwd=cwd, runner=runner, out=out.append, err=err.append, now=lambda: NOW)
    return rc, "\n".join(out), "\n".join(err), runner


class ConfigTest(unittest.TestCase):
    def test_dotenv_reads_only_allowed_keys_and_env_wins(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".env").write_text("# c\nLAB_KEY_NAME=fromfile\nLAB_SG_ID='sg-1'\nSECRET=x\nAWS_PROFILE=filep\nLAB_MAX_INSTANCES=\n")
            cfg = lab.Config.load({"LAB_KEY_NAME": "fromenv"}, Path(d))
        self.assertEqual(cfg.key_name, "fromenv")
        self.assertEqual(cfg.sg_id, "sg-1")
        self.assertEqual(cfg.profile, "filep")
        self.assertIsNone(cfg.max_instances)  # 空は未設定のまま

    def test_no_hardcoded_profile_or_region(self):
        cfg = lab.Config.load({}, Path(tempfile.mkdtemp()))
        self.assertEqual((cfg.profile, cfg.region), ("", ""))
        self.assertEqual(lab.aws_argv(lab.Ctx(cfg), ["ec2", "describe-instances"]), ["aws", "ec2", "describe-instances"])

    def test_bad_number(self):
        with self.assertRaises(lab.LabError):
            lab.Config.load({"LAB_MAX_INSTANCES": "abc"}, Path(tempfile.mkdtemp()))

    def test_parse_ttl(self):
        self.assertEqual(lab.parse_ttl("2h"), 120)
        self.assertEqual(lab.parse_ttl("90m"), 90)
        self.assertEqual(lab.parse_ttl("45"), 45)
        with self.assertRaises(lab.LabError):
            lab.parse_ttl("soon")


class GuardTest(unittest.TestCase):
    def test_mutating_ops_are_refused_unless_allowed(self):
        ctx = lab.Ctx(lab.Config(), runner=FakeRunner())
        for op in ("terminate-instances", "run-instances", "create-key-pair", "delete-security-group", "modify-instance-attribute"):
            with self.assertRaises(lab.LabError, msg=op):
                lab.aws(ctx, ["ec2", op])
        self.assertEqual(ctx.runner.calls, [])

    def test_read_ops_pass(self):
        r = FakeRunner()
        ctx = lab.Ctx(lab.Config(), runner=r)
        lab.aws(ctx, ["ec2", "describe-instances"])
        lab.aws(ctx, ["ec2", "wait", "instance-running", "--instance-ids", "i-1"])
        self.assertEqual(len(r.calls), 2)

    def test_allowed_op_only(self):
        ctx = lab.Ctx(lab.Config(), runner=FakeRunner(), allowed_ops=frozenset({"run-instances"}))
        with self.assertRaises(lab.LabError):
            lab.aws(ctx, ["ec2", "terminate-instances", "--instance-ids", "i-1"])

    def test_untagged_instances_are_dropped(self):
        doc = describe_doc(instance("i-aaa"), instance("i-bbb", purpose="isucon-e2e"), instance("i-ccc", purpose="prod"))
        self.assertEqual([n.id for n in lab.parse_nodes(doc)], ["i-aaa"])


class UpTest(unittest.TestCase):
    def test_dry_run_runs_nothing_and_needs_no_config(self):
        rc, out, err, r = run(["up", "--type", "c5.large", "--count", "2", "--ttl", "2h", "--dry-run"], env={})
        self.assertEqual(rc, 0, err)
        self.assertEqual(r.calls, [])
        self.assertEqual(err.count("run-instances"), 2)
        self.assertIn("shutdown -h +120", err)
        self.assertIn("Key=purpose,Value=isucon-lab", err)
        self.assertIn("terminate", err)
        self.assertIn("HttpTokens=required", err)
        self.assertIn("未設定", err)

    def test_requires_yes(self):
        rc, _, err, r = run(["up", "--type", "c5.large"])
        self.assertEqual(rc, 2)
        self.assertIn("--yes", err)
        self.assertNotIn("run-instances", r.aws_ops())

    def test_unapproved_config_blocks(self):
        env = dict(BASE_ENV)
        del env["LAB_MAX_INSTANCES"]
        rc, _, err, r = run(["up", "--type", "c5.large", "--yes"], env=env)
        self.assertEqual(rc, 3)
        self.assertIn("LAB_MAX_INSTANCES", err)
        self.assertEqual(r.calls, [])

    def test_count_over_limit(self):
        rc, _, err, r = run(["up", "--type", "c5.large", "--count", "3", "--yes"])
        self.assertEqual(rc, 3)
        self.assertEqual(r.calls, [])

    def test_running_plus_count_over_limit(self):
        r = FakeRunner(describes=[describe_doc(instance("i-1"), instance("i-2", idx=2))])
        rc, _, err, r = run(["up", "--type", "c5.large", "--count", "1", "--yes"], runner=r)
        self.assertEqual(rc, 3)
        self.assertNotIn("run-instances", r.aws_ops())

    def test_ttl_limits(self):
        for ttl in ("5m", "5h"):
            rc, _, err, r = run(["up", "--type", "c5.large", "--ttl", ttl, "--yes"])
            self.assertEqual(rc, 2, ttl)
            self.assertEqual(r.calls, [])

    def test_type_restrictions(self):
        for t in ("c5.metal", "c5.12xlarge", "p3.2xlarge", "t3.micro", "bogus"):
            rc, _, _, r = run(["up", "--type", t, "--dry-run"])
            self.assertEqual(rc, 2, t)
            self.assertEqual(r.calls, [])

    def test_allowed_families_overridable(self):
        env = dict(BASE_ENV, LAB_ALLOWED_FAMILIES="c5,c4")
        rc, _, _, _ = run(["up", "--type", "c7i.large", "--dry-run"], env=env)
        self.assertEqual(rc, 2)
        rc, _, _, _ = run(["up", "--type", "c4.large", "--dry-run"], env=env)
        self.assertEqual(rc, 0)

    def test_real_up_flow(self):
        r = FakeRunner(describes=[describe_doc(), describe_doc(instance("i-aaa1"), instance("i-bbb2", idx=2))],
                       run_ids=["i-aaa1", "i-bbb2"])
        rc, out, err, r = run(["up", "--type", "c5.large", "--count", "2", "--ttl", "90m", "--yes"], runner=r)
        self.assertEqual(rc, 0, err)
        ops = r.aws_ops()
        self.assertEqual(ops.count("run-instances"), 2)
        self.assertNotIn("terminate-instances", ops)
        self.assertEqual(ops, ["describe-instances", "run-instances", "run-instances", "wait", "describe-instances"])
        runs = [c["argv"] for c in r.calls if "run-instances" in c["argv"]]
        a = runs[0]
        self.assertEqual(a[a.index("--instance-type") + 1], "c5.large")
        self.assertEqual(a[a.index("--key-name") + 1], "k")
        self.assertEqual(a[a.index("--security-group-ids") + 1], "sg-0123")
        self.assertEqual(a[a.index("--instance-initiated-shutdown-behavior") + 1], "terminate")
        self.assertIn("shutdown -h +90", a[a.index("--user-data") + 1])
        self.assertIn("Key=lab-expires,Value=%d" % (NOW + 90 * 60), " ".join(a))
        self.assertNotEqual(runs[0][runs[0].index("--client-token") + 1], runs[1][runs[1].index("--client-token") + 1])
        self.assertIn("実験室", out)

    def test_partial_failure_reports_created(self):
        class Flaky(FakeRunner):
            def __call__(self, argv, **kw):
                if "run-instances" in argv and any(c["argv"] for c in self.calls if "run-instances" in c["argv"]):
                    self.calls.append({"argv": list(argv), "stdout_path": None, "capture": True})
                    return subprocess.CompletedProcess(argv, 254, stdout="", stderr="InsufficientInstanceCapacity")
                return super().__call__(argv, **kw)
        r = Flaky(describes=[describe_doc()], run_ids=["i-aaa1"])
        rc, _, err, _ = run(["up", "--type", "c5.large", "--count", "2", "--yes"], runner=r)
        self.assertEqual(rc, 4)
        self.assertIn("i-aaa1", err)
        self.assertIn("down --lab", err)

    def test_ami_choice(self):
        cfg = lab.Config()
        self.assertIn("/amd64/", lab.ami_arg(cfg, "c5.large"))
        self.assertIn("/arm64/", lab.ami_arg(cfg, "c7g.large"))
        self.assertIn("/amd64/", lab.ami_arg(cfg, "c5n.large"))
        self.assertIn("/amd64/", lab.ami_arg(cfg, "g4dn.xlarge"))
        cfg.ami = "ami-123"
        self.assertEqual(lab.ami_arg(cfg, "c5.large"), "ami-123")

    def test_spot_option(self):
        env = dict(BASE_ENV, LAB_SPOT="1")
        _, _, err, _ = run(["up", "--type", "c5.large", "--dry-run"], env=env)
        self.assertIn("MarketType=spot", err)


class DownSweepTest(unittest.TestCase):
    def test_down_terminates_only_selected_lab(self):
        r = FakeRunner(describes=[describe_doc(instance("i-a1", "lab-a", 1), instance("i-a2", "lab-a", 2))])
        rc, _, err, r = run(["down"], runner=r)
        self.assertEqual(rc, 0, err)
        term = [c["argv"] for c in r.calls if "terminate-instances" in c["argv"]]
        self.assertEqual(len(term), 1)
        self.assertEqual(term[0][term[0].index("--instance-ids") + 1:term[0].index("--query")], ["i-a1", "i-a2"])

    def test_down_needs_lab_when_several(self):
        r = FakeRunner(describes=[describe_doc(instance("i-a1", "lab-a"), instance("i-b1", "lab-b"))])
        rc, _, err, r = run(["down"], runner=r)
        self.assertEqual(rc, 2)
        self.assertIn("--lab", err)
        self.assertNotIn("terminate-instances", r.aws_ops())

    def test_down_lab_and_all(self):
        both = describe_doc(instance("i-a1", "lab-a"), instance("i-b1", "lab-b"))
        r = FakeRunner(describes=[both])
        rc, _, _, r = run(["down", "--lab", "lab-b"], runner=r)
        self.assertEqual(rc, 0)
        term = [c["argv"] for c in r.calls if "terminate-instances" in c["argv"]][0]
        self.assertIn("i-b1", term)
        self.assertNotIn("i-a1", term)
        r = FakeRunner(describes=[both])
        rc, _, _, r = run(["down", "--all"], runner=r)
        term = [c["argv"] for c in r.calls if "terminate-instances" in c["argv"]][0]
        self.assertIn("i-a1", term)
        self.assertIn("i-b1", term)

    def test_down_never_touches_other_purpose(self):
        r = FakeRunner(describes=[describe_doc(instance("i-e2e", purpose="isucon-e2e"))])
        rc, _, err, r = run(["down", "--all"], runner=r)
        self.assertEqual(rc, 0)
        self.assertNotIn("terminate-instances", r.aws_ops())

    def test_down_dry_run(self):
        rc, _, err, r = run(["down", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(r.calls, [])
        self.assertIn("terminate-instances", err)

    def test_sweep_only_expired_with_grace(self):
        doc = describe_doc(
            instance("i-01d", "lab-a", expires=NOW - 3600),
            instance("i-9a1", "lab-a", 2, expires=NOW - 5 * 60),  # 猶予 10 分の中
            instance("i-live", "lab-b", expires=NOW + 3600),
            instance("i-0e0", "lab-c", expires=None, launch=NOW - 10 * 3600),  # 期限タグが無い: 起動 + max ttl
        )
        rc, _, err, r = run(["sweep"], runner=FakeRunner(describes=[doc]))
        self.assertEqual(rc, 0)
        term = [c["argv"] for c in r.calls if "terminate-instances" in c["argv"]][0]
        ids = term[term.index("--instance-ids") + 1:term.index("--query")]
        self.assertEqual(sorted(ids), ["i-01d", "i-0e0"])

    def test_sweep_nothing(self):
        rc, _, err, r = run(["sweep"], runner=FakeRunner(describes=[describe_doc(instance("i-live"))]))
        self.assertEqual(rc, 0)
        self.assertNotIn("terminate-instances", r.aws_ops())


class ListTest(unittest.TestCase):
    def test_list_table(self):
        r = FakeRunner(describes=[describe_doc(instance("i-a1", "lab-a", 1), instance("i-a2", "lab-a", 2, state="pending"))])
        rc, out, _, _ = run(["list"], runner=r)
        self.assertEqual(rc, 0)
        self.assertIn("i-a1", out)
        self.assertIn("pending", out)
        self.assertIn("\t60\t", out)  # 残り 60 分

    def test_list_empty(self):
        rc, out, _, _ = run(["list"])
        self.assertIn("残っている台は無い", out)

    def test_list_json(self):
        r = FakeRunner(describes=[describe_doc(instance("i-a1"))])
        rc, out, _, _ = run(["list", "--json"], runner=r)
        self.assertEqual(json.loads(out)[0]["id"], "i-a1")

    def test_list_filters_by_purpose(self):
        _, _, _, r = run(["list"])
        a = r.calls[0]["argv"]
        self.assertIn("Name=tag:purpose,Values=isucon-lab", a)


class RemoteTest(unittest.TestCase):
    def two(self):
        return FakeRunner(describes=[describe_doc(instance("i-a1", "lab-a", 1), instance("i-a2", "lab-a", 2))])

    def test_run_all_nodes_quotes_command(self):
        rc, _, _, r = run(["run", "--dir", "work/app", "--", "cargo bench && echo 'done'"], runner=self.two())
        self.assertEqual(rc, 0)
        sshs = [c["argv"] for c in r.calls if c["argv"][0] == "ssh"]
        self.assertEqual(len(sshs), 2)
        a = sshs[0]
        self.assertEqual(a[-2], "ubuntu@203.0.113.1")
        self.assertEqual(sshs[1][-2], "ubuntu@203.0.113.2")
        self.assertEqual(a[-1], "bash -lc " + "'cd work/app && cargo bench && echo '\"'\"'done'\"'\"''")
        self.assertIn("/keys/lab.pem", a)
        self.assertIn("StrictHostKeyChecking=accept-new", a)
        self.assertTrue(any("lab-a/known_hosts" in x for x in a))

    def test_run_multiple_args_are_quoted(self):
        _, _, _, r = run(["run", "--node", "2", "--", "ls", "a b"], runner=self.two())
        sshs = [c["argv"] for c in r.calls if c["argv"][0] == "ssh"]
        self.assertEqual(len(sshs), 1)
        self.assertEqual(sshs[0][-2], "ubuntu@203.0.113.2")
        self.assertEqual(sshs[0][-1], "bash -lc 'ls '\"'\"'a b'\"'\"''")

    def test_run_needs_command(self):
        rc, _, err, _ = run(["run"], runner=self.two())
        self.assertEqual(rc, 2)

    def test_ssh_needs_one_node(self):
        rc, _, err, _ = run(["ssh"], runner=self.two())
        self.assertEqual(rc, 2)
        self.assertIn("--node", err)

    def test_ssh_interactive_has_tty(self):
        r = FakeRunner(describes=[describe_doc(instance("i-a1"))])
        rc, _, _, r = run(["ssh"], runner=r)
        a = [c["argv"] for c in r.calls if c["argv"][0] == "ssh"][0]
        self.assertEqual(a[1], "-t")
        self.assertFalse([c for c in r.calls if c["argv"][0] == "ssh"][0]["capture"])

    def test_ssh_command_via_dashdash(self):
        r = FakeRunner(describes=[describe_doc(instance("i-a1"))])
        _, _, _, r = run(["ssh", "--", "uname -a | cat"], runner=r)
        a = [c["argv"] for c in r.calls if c["argv"][0] == "ssh"][0]
        self.assertEqual(a[-1], "uname -a | cat")
        self.assertNotIn("-t", a)

    def test_push_excludes_secrets_and_targets_all(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "webapp"
            src.mkdir()
            rc, _, err, r = run(["push", str(src), "--exclude", "data/"], runner=self.two(), tmp=d)
        self.assertEqual(rc, 0, err)
        rs = [c["argv"] for c in r.calls if c["argv"][0] == "rsync"]
        self.assertEqual(len(rs), 2)
        a = rs[0]
        for pat in (".env", "*.pem", ".git/", ".cache/", "data/"):
            self.assertIn("--exclude=" + pat, a)
        self.assertNotIn("--delete", a)
        self.assertEqual(a[-1], "ubuntu@203.0.113.1:work/webapp/")
        self.assertTrue(a[-2].endswith("webapp/"))
        mk = [c["argv"] for c in r.calls if c["argv"][0] == "ssh"]
        self.assertEqual(mk[0][-1], "mkdir -p work/webapp")

    def test_push_delete_and_dest(self):
        with tempfile.TemporaryDirectory() as d:
            _, _, _, r = run(["push", d, "--dest", "x/y", "--delete", "--node", "1"], runner=self.two(), tmp=d)
        a = [c["argv"] for c in r.calls if c["argv"][0] == "rsync"][0]
        self.assertIn("--delete", a)
        self.assertEqual(a[-1], "ubuntu@203.0.113.1:x/y/")

    def test_push_missing_dir(self):
        rc, _, err, _ = run(["push", "/no/such/dir"], runner=self.two())
        self.assertEqual(rc, 2)

    def test_fetch_per_node_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            _, _, _, r = run(["fetch", "work/app/out", "--to", d], runner=self.two(), tmp=d)
            rs = [c["argv"] for c in r.calls if c["argv"][0] == "rsync"]
            self.assertEqual(rs[0][-2], "ubuntu@203.0.113.1:work/app/out")
            self.assertEqual(rs[0][-1], "%s/node1/" % d)
            self.assertEqual(rs[1][-1], "%s/node2/" % d)
            self.assertTrue((Path(d) / "node2").is_dir())

    def test_cpuinfo_writes_per_node_file(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _, r = run(["cpuinfo", "--to", d], runner=self.two(), tmp=d)
            self.assertEqual(rc, 0)
            sshs = [c for c in r.calls if c["argv"][0] == "ssh"]
            self.assertEqual(sshs[0]["argv"][-1], "cat /proc/cpuinfo")
            self.assertEqual(sshs[0]["stdout_path"], Path(d) / "node1.cpuinfo")
            self.assertIn("cpu-features node1=", out)
            self.assertIn("node2=", out)

    def test_nothing_mutating_in_remote_commands(self):
        for argv in (["run", "--", "true"], ["ssh", "--node", "1", "--", "true"], ["list"], ["cpuinfo"]):
            _, _, _, r = run(argv, runner=self.two())
            self.assertFalse({"run-instances", "terminate-instances"} & set(r.aws_ops()), argv)

    def test_pending_node_is_not_selected(self):
        r = FakeRunner(describes=[describe_doc(instance("i-a1", state="pending"))])
        rc, _, err, _ = run(["run", "--", "true"], runner=r)
        self.assertEqual(rc, 2)

    def test_missing_ssh_key(self):
        env = dict(BASE_ENV)
        del env["LAB_SSH_KEY"]
        rc, _, err, _ = run(["run", "--", "true"], env=env, runner=self.two())
        self.assertEqual(rc, 3)
        self.assertIn("LAB_SSH_KEY", err)

    def test_private_host_field(self):
        env = dict(BASE_ENV, LAB_HOST_FIELD="private")
        _, _, _, r = run(["run", "--node", "1", "--", "true"], env=env, runner=self.two())
        a = [c["argv"] for c in r.calls if c["argv"][0] == "ssh"][0]
        self.assertEqual(a[-2], "ubuntu@10.0.0.1")

    def test_dry_run_remote_commands_execute_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            for argv in (["run", "--dry-run", "--", "true"], ["ssh", "--dry-run"], ["push", d, "--dry-run"],
                         ["fetch", "x", "--dry-run", "--to", d], ["cpuinfo", "--dry-run", "--to", d]):
                rc, _, err, r = run(argv, tmp=d)
                self.assertEqual(rc, 0, (argv, err))
                self.assertEqual(r.calls, [], argv)
                self.assertIn("+ ", err)


class PriceTest(unittest.TestCase):
    DOC = json.dumps({"terms": {"OnDemand": {"X": {"priceDimensions": {"Y": {"pricePerUnit": {"USD": "0.1070000000"}}}}}}})

    def test_parse_price(self):
        self.assertAlmostEqual(lab.parse_price([self.DOC]), 0.107)
        self.assertIsNone(lab.parse_price([]))
        self.assertIsNone(lab.parse_price(["{}", "not json"]))

    def test_price_command_uses_pricing_region_and_prints_estimate(self):
        class R(FakeRunner):
            def __call__(self, argv, **kw):
                self.calls.append({"argv": list(argv), "stdout_path": None, "capture": True})
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"PriceList": [PriceTest.DOC]}), stderr="")
        rc, out, _, r = run(["--region", "ap-northeast-1", "price", "--type", "c5.large", "--count", "2", "--hours", "2"], runner=R())
        self.assertEqual(rc, 0)
        a = r.calls[0]["argv"]
        self.assertEqual(a[a.index("--region") + 1], "us-east-1")
        self.assertIn("Asia Pacific (Tokyo)", " ".join(a))
        self.assertIn("$0.1070", out)
        self.assertIn("$0.43", out)

    def test_price_unknown_region_says_unconfirmed(self):
        rc, out, _, r = run(["price", "--type", "c5.large"], env={})
        self.assertIn("要確認", out)
        self.assertEqual(r.calls, [])


if __name__ == "__main__":
    unittest.main()
