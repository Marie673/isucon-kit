import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess as sp
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class BinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.server = self.base / 'server'; self.server.mkdir()
        self.fake = self.base / 'fake'; self.fake.mkdir()
        driver = self.fake / 'driver'
        driver.write_text((ROOT / 'tests/fake_remote.py').read_text()); driver.chmod(0o755)
        for name in ['ssh','scp','sudo','systemctl','mysql','psql','sqlite3','mysqldump','pg_dump','alp','pt-query-digest','nginx','vmstat','pidstat','perf','tcpdump','curl','journalctl','timeout','codex','cat','docker','uname','sed','chown','mktemp']:
            (self.fake / name).symlink_to(driver)
        self.env = {**os.environ, 'PATH': str(self.fake) + os.pathsep + os.environ['PATH'], 'FAKE_SERVER': str(self.server),
                    'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME':'test','GIT_AUTHOR_EMAIL':'test@example.test',
                    'GIT_COMMITTER_NAME':'test','GIT_COMMITTER_EMAIL':'test@example.test', 'UV_NO_PROGRESS':'1'}
        for name in ['ISU_LOCK_TOKEN','ISU_ENV_FILE','ISU_WHO']:
            self.env.pop(name, None)
        self.command([ROOT / 'bin/kit-new', self.repo], cwd=ROOT)
        self.config(ISU_HOSTS='host', ISU_IP_host='127.0.0.1', ISU_NGINX_HOST='host', ISU_DB_HOST='host', ISU_APP_HOSTS='host',
                    ISU_LOCK_HOST='host', ISU_SERVICES_host='app.service', ISU_LANG='none', ISU_DB_KIND='mysql', ISU_DB_NAME='test',
                    ISU_REMOTE_HOME=str(self.server / 'host/home'), ISU_TARGET_HOST='app.test', ISU_TARGET_SCHEME='http', ISU_TARGET_PORT='8080',
                    ISU_PORTAL_URL='https://portal.invalid', ISU_END_AT='2099-01-01T18:00:00+09:00', ISU_FREEZE_AT='2099-01-01T17:00:00+09:00',
                    ISU_ACCESS_LOG=str(self.server / 'access.log'), ISU_SLOW_LOG=str(self.server / 'slow.log'), ISU_APP_PORT='8000')
        self.home = self.server / 'host/home'; self.home.mkdir(parents=True, exist_ok=True)
        self.log = self.server / 'access.log'; self.log.touch()
        for path in ['etc/nginx/conf.d','etc/mysql/conf.d','etc/systemd/system','tmp','var/log']:
            (self.server/'host'/path).mkdir(parents=True,exist_ok=True)
        (self.repo / 'hosts/common/etc/mysql').mkdir(parents=True)
        (self.repo / 'hosts/common/etc/mysql/test.cnf').write_text('[mysqld]\nmax_connections=100\n')
        self.git('add', '.'); self.git('commit','-qm','initial')

    def command(self, args, *, cwd=None, code=0, env=None, data=None):
        p = sp.run(list(map(str,args)), cwd=cwd or self.repo, env={**self.env, **(env or {})}, input=data, capture_output=True, text=True)
        if code is not None:
            self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p

    def git(self, *args):
        return self.command(['git', *args]).stdout.strip()

    def tool(self, name, *args, **kw):
        return self.command([self.repo / 'bin' / ('isu-' + name), *args], **kw)

    def config(self, **values):
        with (self.repo / 'isu.env').open('a') as f:
            for key,value in values.items():
                f.write(key+'='+shlex.quote(value)+'\n')

    def commands(self):
        return (self.server / 'commands.jsonl').read_text()

    def start(self, *args):
        p = self.tool('bench','start','--note','test','--label','baseline','--allow-dirty','--skip-golden', *args)
        return p.stdout.strip().splitlines()[-1]

    def test_portal_bench_roundtrip_noise_and_code_sha(self):
        self.tool('doctor')
        id = self.start()
        current = self.repo / '.git/isu/bench-current'
        self.assertTrue(current.exists())
        self.log.write_text(''.join(json.dumps({'msec':t,'method':'GET','uri':'/'})+'\n' for t in [100]*6+[102]*6))
        portal = self.base / 'portal.txt'; portal.write_text('job 123 score 100 pass')
        before = self.git('rev-parse','HEAD')
        self.tool('bench','finish',id,'--score','100','--pass','--job','123','--portal-file',portal)
        self.assertFalse(current.exists())
        self.assertEqual(self.git('rev-list','--count',before+'..HEAD'),'1')
        result = self.repo / 'results' / id
        self.assertEqual((result / 'portal.txt').read_text(), portal.read_text())
        meta = (result / 'meta.env').read_text()
        self.assertIn('START=100',meta); self.assertIn('END=102',meta)
        self.assertIn('RESULT=pass',meta); self.assertIn('LABEL=baseline',meta)
        self.assertIn('| result | label |', (self.repo/'results/SCOREBOARD.md').read_text())
        p = self.command([self.repo/'tools/noise/noise','-f','results/SCOREBOARD.md','band','--label','baseline'])
        self.assertIn('| 1 | 100 |',p.stdout)
        sha = self.tool('ssh','--write','host','cat '+str(self.home/'.isu-deployed')).stdout.strip()
        id2 = self.start('--no-deploy','--no-measure')
        self.assertIn('SHA='+shlex.quote(sha),(self.repo/'results'/id2/'meta.env').read_text())
        self.tool('bench','finish',id2,'--score','0','--fail')
        p = self.command([self.repo/'tools/noise/noise','-f','results/SCOREBOARD.md','band','--label','baseline'])
        self.assertIn('失格',p.stdout)

    def test_start_failure_releases_without_stopping_unstarted_measurement(self):
        p=self.tool('bench','start','--note','fail','--no-build',code=1,env={'FAKE_RESTART_FAIL':'1'})
        self.assertIn('journal:',p.stderr)
        self.assertFalse((self.repo/'.git/isu/bench-current').exists())
        self.assertFalse((self.home/'.isu-lock').exists())
        self.assertNotIn('slow_query_log',self.commands())
        self.assertTrue(list((self.repo/'results').glob('*/SUMMARY.md')))

    def test_token_prevents_same_owner_nested_deploy(self):
        token = self.tool('lock','acquire').stdout.strip().split('=',1)[1]
        self.tool('deploy','--no-build',code=1)
        self.tool('deploy','--no-build',env={'ISU_LOCK_TOKEN':token})
        self.tool('lock','release',env={'ISU_LOCK_TOKEN':token})

    def test_main_checkout_only_and_probe_exception(self):
        other = self.base/'other'
        self.git('worktree','add','-b','work',str(other))
        self.command([other/'bin/isu-deploy','--no-build'],cwd=other,code=1)
        self.command([other/'bin/isu-lock','acquire'],cwd=other,code=1)
        self.command([other/'bin/isu-setup'],cwd=other,code=1)
        self.command([other/'bin/isu-reboot-test','--yes'],cwd=other,code=1)
        self.command([other/'bin/isu-bench','start','--note','x'],cwd=other,code=1)
        self.git('worktree','add','-b','probe/test',str(self.base/'probe'))
        probe=self.base/'probe'
        self.command([probe/'bin/isu-bench','start','--probe','--note','probe','--no-build','--skip-golden','--no-measure'],cwd=probe)
        self.command([probe/'bin/isu-bench','abort','--probe','--reason','test'],cwd=probe)

    def test_manifest_removes_only_previous_files_and_common_restart(self):
        self.config(ISU_DEPLOY_PATHS='data')
        data=self.repo/'data'; data.mkdir(); (data/'deleted').write_text('old'); (data/'kept').write_text('keep')
        self.tool('deploy','--no-build','--allow-dirty')
        (self.home/'data/unmanaged').write_text('user')
        (data/'deleted').unlink()
        p=self.repo/'hosts/common/etc/mysql/test.cnf'; p.write_text('max_connections=200')
        before=len(self.commands())
        self.tool('deploy','--no-build','--allow-dirty')
        self.assertFalse((self.home/'data/deleted').exists())
        self.assertTrue(list((self.home/'.isu-removed').glob('**/deleted')))
        self.assertTrue((self.home/'data/unmanaged').exists())
        self.assertIn('restart',self.commands()[before:]); self.assertIn('mysql',self.commands()[before:])

    def test_restart_failure_fails_deploy_and_shows_journal(self):
        p=self.tool('deploy','--no-build',code=1,env={'FAKE_RESTART_FAIL':'1'})
        self.assertIn('journal:',p.stderr)
        self.assertFalse((self.home/'.isu-deployed').exists())

    def test_health_http_and_5xx_failure_4xx_warning(self):
        self.tool('deploy','--no-build')
        self.assertIn('app.test:8080:127.0.0.1',self.commands())
        self.assertIn('http://app.test:8080/',self.commands())
        self.config(ISU_HEALTH_RETRY_SECS='0')
        self.tool('deploy','--no-build','--allow-dirty',code=1,env={'FAKE_HTTP':'503'})
        p=self.tool('deploy','--no-build','--allow-dirty',env={'FAKE_HTTP':'404'})
        self.assertIn('HTTP 404',p.stderr)
        self.tool('deploy','--no-build','--allow-dirty',code=1,env={'FAKE_CURL_FAIL':'1'})

    def test_hold_and_write_ssh_guards(self):
        token=self.tool('lock','acquire').stdout.strip().split('=',1)[1]
        self.tool('ssh','host','uptime',code=1)
        self.tool('ssh','--during-hold','host','echo ok')
        self.tool('lock','release',env={'ISU_LOCK_TOKEN':token})
        for command in ['systemctl restart app','rm file','sed -i s/a/b/ file','mysql -e "UPDATE t SET x=1"','echo x > file','sh -c true']:
            self.tool('ssh','host',command,code=1)
        self.tool('ssh','--write','host','systemctl restart app')

    def test_deadline_and_freeze(self):
        self.config(ISU_END_AT='2000-01-01T18:00:00+09:00')
        self.tool('ssh','host','uptime',code=1)
        self.tool('deploy','--no-build','--allow-dirty',code=1)
        self.config(ISU_END_AT='2099-01-01T18:00:00+09:00',ISU_FREEZE_AT='2000-01-01T17:00:00+09:00')
        self.tool('deploy','--no-build','--allow-dirty',code=1)
        self.tool('deploy','--no-build','--allow-dirty','--after-freeze')

    def test_comment_only_groups_and_alp_group_argument(self):
        id=self.start()
        self.tool('bench','finish',id,'--score','1','--pass')
        (self.repo/'conf/alp-groups.txt').write_text('# comment\n^/items/[0-9]+\n')
        id=self.start()
        self.tool('bench','finish',id,'--score','1','--pass')
        lines=[json.loads(l) for l in (self.server/'commands.jsonl').read_text().splitlines()]
        calls=[args for name,args in lines if name=='alp' and '-m' in args]
        self.assertEqual(calls[-1][calls[-1].index('-m')+1],'^/items/[0-9]+')

    def test_postgresql_measurement_and_docker_prefix(self):
        self.config(ISU_DB_KIND='postgresql',ISU_DB_CMD='sudo docker exec db')
        self.tool('doctor')
        id=self.start()
        self.tool('bench','finish',id,'--score','1','--pass')
        self.assertTrue((self.repo/'results'/id/'host/pg-stat-statements.txt').exists())
        self.assertIn('pg_stat_statements_reset()',self.commands())
        self.assertIn('total_exec_time',self.commands())
        self.assertIn('docker',self.commands())
        self.tool('inventory')
        self.assertIn('postgres',self.commands().lower())

    def test_no_measure_skips_profilers_and_abort(self):
        self.tool('deploy','--no-build')
        start=len(self.commands())
        id=self.start('--no-deploy','--no-measure')
        self.tool('bench','abort',id,'--reason','拡張が応答しない')
        text=self.commands()[start:]
        for word in ['slow_query_log','pg_stat_statements_reset','vmstat','pidstat','perf record']:
            self.assertNotIn(word,text)
        self.assertIn('STATE=aborted',(self.repo/'results'/id/'meta.env').read_text())
        self.assertFalse((self.home/'.isu-lock').exists())

    def test_backup_rejects_empty_or_failed_dump(self):
        self.tool('backup')
        for option in ['FAKE_DUMP_FAIL','FAKE_DUMP_EMPTY']:
            self.tool('backup','bad',code=1,env={option:'1'})
            self.assertFalse((self.home/'backup/bad-db.sql.gz').exists())

    def test_export_seed_limit_and_failure_no_empty_file(self):
        self.tool('export-seed',self.repo/'seed')
        self.assertEqual((self.repo/'seed/items.jsonl').read_text(),'\x7b"id":1}\n')
        self.assertIn('group_concat_max_len=1048576',self.commands())
        self.tool('export-seed',self.repo/'bad-seed',code=1,env={'FAKE_MYSQL_FAIL':'1'})
        self.assertEqual(list((self.repo/'bad-seed').glob('*.jsonl')),[])

    def test_review_stdin_and_visible_failure(self):
        self.git('branch','base')
        (self.repo/'change').write_text('changed')
        self.git('add','change');self.git('commit','-qm','change')
        p=self.tool('review','diff','base','--with','codex',code=7,env={'FAKE_CODEX_EXIT':'7'})
        self.assertIn('review stderr',p.stderr)
        self.assertIn('diff --git',(self.server/'review-input').read_text())
        self.assertNotIn('exec review',self.commands())

    def test_golden_uses_shared_http_port(self):
        (self.repo/'conf/golden.txt').write_text('root GET /\n')
        self.tool('golden','record','--no-init')
        self.assertTrue((self.repo/'golden/root.txt').exists())
        self.assertIn('http://app.test:8080/',self.commands())
        self.assertIn('app.test:8080:127.0.0.1',self.commands())

    def test_mysql_container_setup_keeps_stdin_open(self):
        self.config(ISU_DB_CMD='sudo docker exec db')
        self.tool('setup')
        self.assertIn('exec -i db bash -s',self.commands())
        self.assertIn('long_query_time=0',(self.server/'host/etc/mysql/conf.d/zz-isu.cnf').read_text())

    def test_postgresql_setup_preserves_preloads_and_seed(self):
        self.config(ISU_DB_KIND='postgresql',ISU_DB_CMD='sudo docker exec db')
        self.tool('setup')
        self.assertIn('auto_explain,pg_stat_statements',self.commands())
        self.assertIn('restart',self.commands())
        self.tool('export-seed',self.repo/'pg-seed')
        self.assertEqual((self.repo/'pg-seed/items.jsonl').read_text(),'{"id":1}\n')

    def test_run_requires_explicit_mode_and_preserves_current(self):
        self.tool('deploy','--no-build')
        id=self.start('--no-deploy','--no-measure')
        # 秒ごとの判定は RuntimeTests でも検査する。wait が結果を書かないことを見る。
        before=(self.repo/'results'/id/'meta.env').read_text()
        self.tool('bench','run','--note','x',code=1)
        self.assertEqual((self.repo/'results'/id/'meta.env').read_text(),before)
        self.tool('bench','abort',id,'--reason','test')

    def test_setup_json_log_preserves_symlink(self):
        etc=self.server/'host/etc/nginx'
        (etc/'sites-enabled').mkdir()
        target=etc/'site.conf';target.write_text('access_log /var/log/nginx/access.log;\n')
        link=etc/'sites-enabled/default';link.symlink_to(target)
        (etc/'nginx.conf').write_text('access_log /var/log/nginx/access.log;\n')
        self.tool('setup')
        self.assertTrue(link.is_symlink())
        self.assertIn('# isu-setup:',target.read_text())
        conf=(etc/'conf.d/isu-log.conf').read_text()
        for key in ['msec','connection','connection_requests','request_id','server_protocol','ssl_session_reused','upstream_addr','http_authorization','http_cookie']:
            self.assertIn('$'+key,conf)
        self.assertIn('--follow-symlinks',self.commands())

    def test_capture_is_bounded_and_not_committed(self):
        id=self.start('--capture')
        self.assertIn('tcpdump',self.commands())
        # stop の tar 回収も偽のサーバーで行う。
        (self.home/'.isu-measure/capture.pcap').write_bytes(b'pcap fixture')
        self.tool('bench','finish',id,'--score','1','--pass')
        result=self.repo/'results'/id/'host'
        self.assertTrue((result/'capture.pcap').exists())
        self.assertNotIn('capture.pcap',self.git('ls-files','results'))
        self.assertNotIn('access.log',self.git('ls-files','results'))
        self.assertIn('16',self.commands())
        self.assertIn('300',self.commands())

    def test_no_log_window_is_unrecorded(self):
        id=self.start()
        p=self.tool('bench','finish',id,'--score','1','--pass')
        self.assertIn('START/END は未記録',p.stderr)
        self.assertNotIn('START=',(self.repo/'results'/id/'meta.env').read_text())

    def test_commit_failure_warns_and_preserves_pending(self):
        self.tool('deploy','--no-build')
        id=self.start('--no-deploy','--no-measure')
        hook=self.repo/'.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
        p=self.tool('bench','finish',id,'--score','1','--pass')
        self.assertIn('コミットが失敗',p.stderr)
        self.assertTrue((self.repo/'.git/isu/commit-pending.json').exists())
        self.assertFalse((self.home/'.isu-lock').exists())
        hook.unlink()
        self.tool('score',id,'1','--pass')
        self.assertFalse((self.repo/'.git/isu/commit-pending.json').exists())

    def test_merge_pending_is_retryable(self):
        self.tool('deploy','--no-build')
        id=self.start('--no-deploy','--no-measure')
        merge=self.repo/'.git/MERGE_HEAD';merge.write_text(self.git('rev-parse','HEAD')+'\n')
        p=self.tool('bench','finish',id,'--score','1','--pass')
        self.assertIn('マージ中',p.stderr)
        self.assertTrue((self.repo/'.git/isu/commit-pending.json').exists())
        merge.unlink()
        self.tool('score',id,'1','--pass')

    def test_final_doctor_checks_deployment_and_logs(self):
        self.tool('deploy','--no-build')
        self.tool('doctor','--final',code=1)
        self.tool('doctor','--final',env={'FAKE_LOGS_OFF':'1'})
        (self.home/'.isu-deployed').write_text('wrong')
        self.tool('doctor','--final',code=1,env={'FAKE_LOGS_OFF':'1'})

    def test_missing_env_stops_doctor_and_logs_off_warns(self):
        p=self.tool('doctor',env={'FAKE_LOGS_OFF':'1'})
        self.assertIn('--no-measure',p.stderr)
        self.config(ISU_TARGET_HOST='')
        p=self.tool('doctor',code=1)
        self.assertIn('ISU_TARGET_HOST',p.stderr)

    def test_no_checkpoint_option_and_default_preserve_settings(self):
        dst=self.base/'without-checkpoint'
        self.command([ROOT/'bin/kit-new',dst,'--no-checkpoint'])
        self.assertFalse((dst/'tools/checkpoint/checkpoint').exists())
        self.assertFalse((dst/'.agents/skills/audit/SKILL.md').exists())
        settings=self.repo/'.claude/settings.json'
        settings.write_text('{"permissions":{"allow":[]}}\n')
        self.command([ROOT/'bin/kit-new',self.repo])
        self.assertEqual(settings.read_text(),'{"permissions":{"allow":[]}}\n')

    def test_kit_new_preserves_records_update_refreshes_managed(self):
        board=self.repo/'docs/README.md';board.write_text('my board')
        agents=self.repo/'AGENTS.md';agents.write_text('my agents')
        tool=self.repo/'bin/isu-score';tool.write_text('old')
        self.command([ROOT/'bin/kit-new',self.repo])
        self.assertEqual(agents.read_text(),'my agents')
        self.assertEqual(board.read_text(),'my board')
        self.assertEqual(tool.read_text(),'old')
        (self.repo/'bin/isu-task').write_text('legacy')
        self.command([ROOT/'bin/kit-new',self.repo,'--update'])
        self.assertEqual(board.read_text(),'my board')
        self.assertNotEqual(agents.read_text(),'my agents')
        self.assertIn('isu_runtime',tool.read_text())
        self.assertTrue((self.repo/'tools/checkpoint/checkpoint').exists())
        self.assertFalse((self.repo/'bin/isu-task').exists())

    def test_installed_document_links_and_tool_paths_exist(self):
        for p in [self.repo/'AGENTS.md',*self.repo.glob('.agents/skills/**/SKILL.md'),*self.repo.glob('docs/tuning/*.md')]:
            text=p.read_text()
            for link in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)',text):
                if '://' not in link:
                    self.assertTrue((p.parent/link).exists(),str(p)+': '+link)
            if 'tuning' in p.parts:
                continue
            for rel in re.findall(r'(?<![\w/])(?:bin/isu-[\w-]+|tools/[\w-]+/[\w.-]+|docs/[\w/-]+\.md|\.agents/skills/[\w-]+/SKILL\.md)',text):
                self.assertTrue((self.repo/rel).exists(),str(p)+': '+rel)

    def test_intake_rejects_outside_static_and_durability(self):
        self.git('checkout','-qb','change')
        (self.repo/'app').mkdir();(self.repo/'app/main.go').write_text('package main')
        self.git('add','app'); self.git('commit','-qm','app')
        self.git('checkout','main')
        self.tool('intake','change','--paths','other',code=1)
        self.tool('intake','change','--paths','app')
        self.git('checkout','change')
        (self.repo/'app/main.go').write_text('SET synchronous_commit = off;')
        self.git('add','app');self.git('commit','-qm','bad')
        self.git('checkout','main')
        self.tool('intake','change','--paths','app',code=1)
        self.git('checkout','change')
        (self.repo/'app/main.go').write_text('package main')
        (self.repo/'app/static').mkdir();(self.repo/'app/static/a.css').write_text('body{}')
        self.git('add','app');self.git('commit','-qm','static')
        self.git('checkout','main')
        self.tool('intake','change','--paths','app',code=1)

    def test_reboot_all_hosts_skips_empty_last_ip(self):
        self.config(ISU_HOSTS='host empty',ISU_IP_empty='')
        self.tool('reboot-test','--yes')
        self.assertIn('reboot',self.commands())

    def test_result_commit_does_not_change_code_sha(self):
        # docs と results だけのコミットを SHA の正にしない。
        self.tool('deploy','--no-build')
        sha=(self.home/'.isu-deployed').read_text().strip()
        (self.repo/'docs/test.md').write_text('record')
        self.git('add','docs/test.md');self.git('commit','-qm','record')
        self.tool('deploy','--no-build')
        self.assertEqual((self.home/'.isu-deployed').read_text().strip(),sha)

if __name__=='__main__': unittest.main()
