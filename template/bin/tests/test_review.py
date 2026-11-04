import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess as sp
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import isu_runtime as rt
from isu_summary import cpu_table, processes, journal_counts

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.k = rt.Kit.__new__(rt.Kit)
        self.k.root = self.k.main = self.root
        self.k.state = self.root / '.state'; self.k.state.mkdir()
        self.k.results = self.root / 'results'; self.k.results.mkdir()
        self.k.env = {'ISU_HOSTS': 'host', 'ISU_IP_host': '127.0.0.1', 'ISU_LOCK_HOST': 'host', 'ISU_DB_KIND':'none'}
        self.k.branch = 'main'; self.k.token = 'token'
        self.k.home = '/home/isucon'; self.k.lockdir = self.k.home + '/.isu-lock'; self.k.config = self.k.state / 'ssh_config'
        self.r = self.k.results / 'run'; self.r.mkdir()
        self.m = {'ID':'run','START':'1767225600','END':'1767225601','SHA':'abc'}
        rt.write_meta(self.r / 'meta.env', self.m)

    def current(self, measure=True):
        (self.k.state/'bench-current').write_text(json.dumps({'id':'run','root':str(self.root),'token':'token','measure':measure}))

    def test_summary_filters_window_in_server_tz_and_keeps_sections(self):
        host = self.r/'host'; host.mkdir()
        # Mac の TZ と異なる UTC のサーバー。窓は UTC の日付またぎ。
        self.m.update(START=str(dt.datetime.fromisoformat('2026-01-01T23:59:59+00:00').timestamp()),END=str(dt.datetime.fromisoformat('2026-01-02T00:00:00+00:00').timestamp()),TZ_OFFSET='+0000')
        rt.write_meta(self.r/'meta.env',self.m)
        (host/'tz-offset').write_text('+0000')
        header='r b swpd free buff cache si so bi bo in cs us sy id wa st time\n'
        outside='99 0 0 0 0 0 0 0 0 0 0 0 99 0 1 0 0 2026-01-01 23:59:58\n'
        inside='2 0 0 0 0 0 0 0 0 0 0 0 20 10 65 5 0 2026-01-01 23:59:59\n3 0 0 0 0 0 0 0 0 0 0 0 40 20 35 5 0 2026-01-02 00:00:00\n'
        (host/'vmstat.log').write_text(header+outside*45+inside)
        (host/'pidstat.log').write_text('# Time UID PID %usr %system %guest %wait %CPU CPU Command\n23:59:58 0 1 0 0 0 0 999 0 before-only\n23:59:59 0 1 0 0 0 0 20 0 app\n00:00:00 0 1 0 0 0 0 40 0 app\n')
        (host/'slow.txt').write_text('outside header\n# Profile\n# Rank Query\n# 1 SELECT\n\nDETAILED QUERY SECRET\n')
        (host/'alp.txt').write_text('alp aggregate')
        (host/'alp-errors.txt').write_text('404 errors')
        (host/'pg-stat-statements.txt').write_text('postgres top')
        (host/'perf.txt').write_text('swapper hidden\napp useful symbol')
        (host/'journal.txt').write_text('20 warn repeated')
        (self.r/'bench.log').write_text('\n'.join('bench-'+str(i) for i in range(100)))
        with patch.dict(os.environ,{'TZ':'Asia/Tokyo'}):
            self.k.summarize('run')
        text=(self.r/'SUMMARY.md').read_text()
        self.assertIn('| host | 30 | 15 | 5 | 50 | 3 | 2 |',text)
        self.assertIn('app                        30.0',text)
        self.assertNotIn('before-only',text)
        self.assertNotIn('DETAILED QUERY SECRET',text)
        self.assertNotIn('swapper hidden',text)
        for value in ['4xx / 5xx','404 errors','# Profile','postgres top','app useful symbol','20 warn repeated','bench-99','生データ']:
            self.assertIn(value,text)
        self.assertNotIn('bench-0\n',text)

    def test_window_offset_is_from_access_log_not_mac(self):
        host=self.r/'host';host.mkdir()
        (host/'access.log').write_text(''.join(json.dumps({'time':'2026-01-01T00:00:00-05:00','msec':'1767243600'})+'\n' for _ in range(6)))
        self.k.window('run')
        meta=rt.read_meta(self.r/'meta.env')
        self.assertEqual(meta['START_HMS'],'00:00:00');self.assertEqual(meta['TZ_OFFSET'],'-0500')

    def test_summary_window_option_top_15(self):
        host=self.r/'host';host.mkdir();(host/'access.log').write_text('{}')
        tool=self.root/'tools/bench-window/bench-window';tool.parent.mkdir(parents=True);tool.touch()
        self.k.env['ISU_WINDOW_LAST']='12s'
        with patch.object(rt.sp,'run',return_value=sp.CompletedProcess([],0,stdout='window')) as command:
            self.k.summarize('run')
            args=command.call_args.args[0]
            self.assertEqual(args[args.index('--last')+1],'12s');self.assertEqual(args[args.index('--top')+1],'15')

    def test_journal_grouping_is_counted_and_sorted(self):
        lines=journal_counts('2026-01-01T00:00:00+00:00 host app[12]: warn abc\n2026-01-01T00:00:01+00:00 host app[13]: warn abc\n2026-01-01T00:00:01+00:00 host app[14]: error other\n')
        self.assertTrue(lines[0].lstrip().startswith('2 '));self.assertIn('warn abc',lines[0])

    def test_ssh_config_atomic_unchanged_and_has_identity_options(self):
        self.k.gen_ssh_config();before=self.k.config.stat().st_mtime_ns
        self.k.gen_ssh_config();self.assertEqual(self.k.config.stat().st_mtime_ns,before)
        original=rt.os.replace
        with patch.object(rt.os,'replace',side_effect=lambda src,dst: (self.assertTrue(Path(src).read_text()),self.assertTrue(Path(dst).read_text()),original(src,dst))[-1]) as replace:
            self.k.env['ISU_IP_host']='127.0.0.2';self.k.gen_ssh_config();replace.assert_called_once()
        self.assertIn('IdentitiesOnly yes',self.k.config.read_text());self.assertIn('LogLevel ERROR',self.k.config.read_text())

    def test_abort_without_lock_attempts_every_host_and_records_failure(self):
        self.current();self.k.env.update(ISU_HOSTS='host other',ISU_IP_other='127.0.0.2')
        with patch.object(self.k,'owner',return_value='other-token|main'),patch.object(self.k,'script',side_effect=RuntimeError('offline')) as stop,patch.object(self.k,'commit'):
            self.k.bench(rt.parser('isu-bench').parse_args(['abort','run','--reason','lost']))
            self.assertEqual([c.args[0] for c in stop.call_args_list],['host','other'])
        self.assertFalse((self.k.state/'bench-current').exists())
        self.assertIn('other: offline',(self.r/'measure-warnings.txt').read_text())
        self.assertEqual(rt.read_meta(self.r/'meta.env')['STATE'],'aborted')

    def test_finish_saves_score_before_collection_and_retries(self):
        self.current()
        a=rt.parser('isu-bench').parse_args(['finish','run','--score','42','--pass','--job','job-7'])
        with patch.object(self.k,'owner',return_value='token|main'),patch.object(self.k,'measure',side_effect=RuntimeError('collect')):
            with self.assertRaises(RuntimeError):self.k.bench(a)
        meta=rt.read_meta(self.r/'meta.env')
        self.assertEqual((meta['SCORE'],meta['RESULT'],meta['JOB']),('42.0','pass','job-7'))
        with patch.object(self.k,'owner',return_value='token|main'),patch.object(self.k,'measure'),patch.object(self.k,'release'),patch.object(self.k,'commit'):
            self.k.bench(a)
        self.assertFalse((self.k.state/'bench-current').exists())

    def test_force_release_cleans_current_and_shows_cleanup(self):
        self.current()
        with patch.object(self.k,'ssh'),contextlib.redirect_stderr(io.StringIO()) as err:
            self.k.force_release()
        self.assertFalse((self.k.state/'bench-current').exists())
        self.assertIn('isu-measure stop run',err.getvalue())

    def test_bootstrap_needs_no_services_or_ssh(self):
        key=self.root/'key';key.touch()
        self.k.env.update(ISU_PORTAL_URL='http://portal',ISU_END_AT='2099-01-01T18:00:00+09:00',ISU_FREEZE_AT='2099-01-01T17:00:00+09:00',ISU_BOOT_USER='root',ISU_BOOT_KEY=str(key))
        with patch.object(self.k,'ssh') as ssh:
            self.k.doctor(rt.parser('isu-doctor').parse_args(['--bootstrap']));ssh.assert_not_called()

    def test_pending_paths_are_committed_before_removal(self):
        sp.run(['git','init','-q','-b','main',self.root],check=True)
        self.k.state=self.root/'.git/isu';self.k.state.mkdir()
        (self.root/'old').write_text('pending');(self.root/'new').write_text('current')
        pending=self.k.state/'commit-pending.json';pending.write_text(json.dumps({'paths':['old'],'message':'old'}))
        env={'GIT_AUTHOR_NAME':'test','GIT_AUTHOR_EMAIL':'test@example.invalid','GIT_COMMITTER_NAME':'test','GIT_COMMITTER_EMAIL':'test@example.invalid'}
        with patch.dict(os.environ,env):self.k.commit('new',['new'])
        self.assertFalse(pending.exists())
        tracked=sp.check_output(['git','-C',self.root,'ls-files'],text=True)
        self.assertIn('old',tracked);self.assertIn('new',tracked)

    def test_reboot_closes_mux_and_ssh_replaces_invalid_bytes(self):
        with patch.object(self.k,'ssh'),patch.object(self.k,'close_mux') as close:
            self.k.reboot_host('host');close.assert_called_once_with('host')
        with patch.object(rt.sp,'run',return_value=sp.CompletedProcess([],0,stdout='')) as run:
            self.k.ssh('host','true');self.assertEqual(run.call_args.kwargs['errors'],'replace')

    def test_measurement_locale_line_buffering_and_sixty_second_stop(self):
        self.k.env.update(ISU_NGINX_HOST='host',ISU_APP_HOSTS='host',ISU_ACCESS_LOG='/access.log')
        with patch.object(self.k,'script',return_value=sp.CompletedProcess([],0,stdout='')) as script:
            self.k.measure('start','run')
            start=script.call_args.args[1]
            self.assertIn('export LC_ALL=C S_TIME_FORMAT=ISO TZ=UTC',start)
            self.assertIn('stdbuf -oL vmstat',start)
            self.assertIn('stdbuf -oL pidstat -H',start)
        with patch.object(self.k,'script',side_effect=RuntimeError('stop')) as script:
            with self.assertRaises(RuntimeError):self.k.measure('stop','run')
            self.assertIn('seq 600',script.call_args.args[1])
