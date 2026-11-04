#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""検証専用のローカル Ubuntu コンテナで競技の道具を一巡する。"""
from pathlib import Path
import datetime as dt
import json
import os
import shlex
import subprocess as sp
import time

root = Path(__file__).resolve().parents[2]
work = root / 'tests/linux-work'
work.mkdir(exist_ok=True)
repo = work / 'repo'
container = 'isu-kit-review-test'
env = {**os.environ, 'GIT_AUTHOR_NAME': 'kit test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid', 'GIT_COMMITTER_NAME': 'kit test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}

def run(args, **kw):
    print('+', shlex.join(map(str, args)), flush=True)
    return sp.run(list(map(str, args)), check=True, env=env, **kw)

def local(args):
    return run(args, stdout=sp.PIPE, text=True).stdout.strip()

def remote(script):
    return run(['podman', 'exec', '-i', container, 'bash', '-se'], input=script, text=True)

def tool(name, *args, **kw):
    return run([repo / 'bin' / ('isu-' + name), *args], cwd=repo, **kw)

key = work / 'id_ed25519'
if not key.exists():
    run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key])
remote('mkdir -p /root/.ssh; chmod 700 /root/.ssh; systemctl start ssh nginx mysql\n')
run(['podman', 'cp', key.with_suffix('.pub'), container + ':/root/.ssh/authorized_keys'])
remote('chmod 600 /root/.ssh/authorized_keys; mkdir -p /home/isucon/webapp; echo fixture > /home/isucon/webapp/test.txt; chown -R isucon:isucon /home/isucon/webapp; mysql -e "CREATE DATABASE IF NOT EXISTS kit_test; CREATE TABLE IF NOT EXISTS kit_test.items(id INT PRIMARY KEY); INSERT IGNORE INTO kit_test.items VALUES(1);"\n')
port = local(['podman', 'port', container, '22']).rsplit(':', 1)[1]
run([root / 'bin/kit-new', repo, '--update'])
values = dict(ISU_HOSTS='host', ISU_IP_host='127.0.0.1', ISU_SSH_PORT_host=port, ISU_SSH_KEY=str(key), ISU_BOOT_KEY=str(key), ISU_BOOT_USER='root', ISU_TEAM_GITHUB='', ISU_NGINX_HOST='host', ISU_LOCK_HOST='host', ISU_APP_HOSTS='host', ISU_SERVICES_host='nginx', ISU_DB_HOST='host', ISU_DB_KIND='mysql', ISU_DB_NAME='kit_test', ISU_DB_CMD='sudo', ISU_TARGET_HOST='localhost', ISU_TARGET_SCHEME='http', ISU_TARGET_PORT='80', ISU_APP_PORT='80', ISU_PORTAL_URL='http://portal.invalid', ISU_LANG='none', ISU_IMPORT_PATHS='webapp', ISU_DEPLOY_PATHS='webapp', ISU_IDLE_RPS='5', ISU_CAPTURE_MB='1', ISU_CAPTURE_SECS='30', ISU_FREEZE_AT=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)).isoformat(), ISU_END_AT=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=2)).isoformat())
with (repo / 'isu.env').open('a') as f:
    for k,v in values.items():
        f.write(k + '=' + shlex.quote(v) + '\n')
tool('doctor', '--bootstrap')
tool('keys')
tool('setup')
tool('backup', 'initial')
tool('inventory')
tool('import')
(repo/'conf/alp-groups.txt').write_text('^/items/[0-9]+$\n')
run(['git', 'add', '--', 'isu.env', 'hosts', 'webapp', 'docs', 'bin', 'tools', '.agents', '.claude', '.codex', 'golden', '.gitignore', '.kit-managed.json', 'AGENTS.md', 'CLAUDE.md', 'conf', 'results', 'templates'], cwd=repo)
if sp.run(['git', 'diff', '--cached', '--quiet'], cwd=repo).returncode:
    run(['git', 'commit', '-qm', 'fixture'], cwd=repo)
tool('doctor')
owner=repo/'hosts/host/etc/nginx/conf.d/kit-owner.conf'
owner.write_text('# local ownership fixture\n')
run(['git','add','--',str(owner.relative_to(repo))],cwd=repo)
if sp.run(['git','diff','--cached','--quiet'],cwd=repo).returncode:
    run(['git','commit','-qm','ownership fixture'],cwd=repo)
p = tool('bench', 'start', '--note', 'local Linux', '--label', 'linux', '--no-build', '--skip-golden', '--capture', stdout=sp.PIPE, text=True)
id = p.stdout.strip()
# SSH からローカル nginx へ負荷をかける。404 も別の表へ残す。
remote('for i in $(seq 100); do curl -s -o /dev/null http://127.0.0.1/; curl -s -o /dev/null http://127.0.0.1/items/$i; sleep 0.03; done\n')
remote('dd if=/dev/zero of=/var/www/html/large bs=1M count=2 status=none; curl -s -o /dev/null http://127.0.0.1/large\n')
time.sleep(2)
tool('bench', 'finish', id, '--score', '100', '--pass', '--job', 'local-test')
tool('deploy', '--no-build')
if local(['podman','exec',container,'stat','-c','%U','/etc/nginx/conf.d/kit-owner.conf']) != 'root':
    raise RuntimeError('新規 /etc の所有者が root ではありません')
summary = (repo / 'results' / id / 'SUMMARY.md').read_text()
for expected in ['| host | us | sy | wa | idle | max run queue | samples |', '| host | ', 'プロセス別 CPU', 'mysqld', 'cpu:', '^/items/[0-9]+$', '4xx / 5xx', 'Profile', '生データ']:
    if expected not in summary:
        raise RuntimeError('SUMMARY に必要な集計がありません: ' + expected)
capture=repo / 'results' / id / 'host' / 'capture.pcap'
if not 0 < capture.stat().st_size <= 1024 * 1024:
    raise RuntimeError('capture が空か、サイズ上限を超えています')
if b'GET / HTTP/1.1' not in capture.read_bytes():
    raise RuntimeError('最初の要求が失われています')
run([repo/'tools/capture/capture','pcap',capture,'--out',capture.with_name('http.jsonl')])
if not capture.with_name('http.jsonl').read_text():
    raise RuntimeError('capture の先頭を復元できません')
print('LINUX PASS:', repo / 'results' / id / 'SUMMARY.md', flush=True)
