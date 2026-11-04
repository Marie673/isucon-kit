#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""競技道具の共通処理。外部通信は ssh/scp だけを使う。"""
from __future__ import annotations
import argparse
import collections
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess as sp
import sys
import tarfile
import time
import tempfile
from isu_summary import cpu_table, processes, profile, journal_counts, timezone, sample_time

Q = shlex.quote

def run(args, **kw):
    return sp.run([str(a) for a in args], check=True, **kw)

def output(args, **kw):
    return run(args, stdout=sp.PIPE, text=True, **kw).stdout.strip()

def fail(s):
    raise RuntimeError(s)

def warn(s):
    print('[warn] ' + s, file=sys.stderr)

def epoch(value):
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()

def read_meta(path):
    data = {}
    if path.exists():
        for line in path.read_text().splitlines():
            k, sep, v = line.partition('=')
            if sep:
                words = shlex.split(v)
                data[k] = words[0] if words else ''
    return data

def write_meta(path, data):
    tmp = path.with_suffix('.tmp')
    clean = {k: str(v).replace('\n', ' ').replace('\r', ' ') for k, v in data.items()}
    tmp.write_text(''.join(f'{k}={Q(v)}\n' for k, v in clean.items()))
    tmp.replace(path)

class Kit:
    def __init__(self):
        self.root = Path(output(['git', 'rev-parse', '--show-toplevel']))
        self.main = Path(output(['git', 'worktree', 'list', '--porcelain']).splitlines()[0].split(' ', 1)[1])
        self.state = Path(output(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'])) / 'isu'
        self.state.mkdir(exist_ok=True)
        envfile = os.environ.get('ISU_ENV_FILE', str(self.root / 'isu.env'))
        # isu.env は持ち主が書く shell の設定。値をコマンド行へ出さない。
        raw = run(['bash', '-c', 'set -a; source "$1"; env -0', 'bash', envfile], stdout=sp.PIPE).stdout
        self.env = dict(x.decode().split('=', 1) for x in raw.split(b'\0') if b'=' in x)
        self.branch = output(['git', 'branch', '--show-current'])
        self.home = self.get('ISU_REMOTE_HOME', '/home/isucon')
        self.lockdir = self.get('ISU_LOCK_DIR', self.home + '/.isu-lock')
        self.token = os.environ.get('ISU_LOCK_TOKEN', '')
        self.results = self.main / 'results'
        self.config = self.state / 'ssh_config'
        self.gen_ssh_config()

    def gen_ssh_config(self):
        lines = []
        for h in self.hosts():
            lines += [f'Host {h}', f'  HostName {self.get("ISU_IP_" + h)}',
                      f'  User {self.get("ISU_SSH_USER", "isucon")}', f'  Port {self.get("ISU_SSH_PORT_" + h, "22")}', f'  IdentityFile {self.get("ISU_SSH_KEY")}']
        lines += ['Host *', '  IdentitiesOnly yes', '  LogLevel ERROR', '  StrictHostKeyChecking accept-new', f'  UserKnownHostsFile {self.state}/known_hosts',
                  '  ControlMaster auto', '  ControlPath ~/.ssh/cm-isu-%C', '  ControlPersist 60s',
                  '  ConnectTimeout 10', '  ServerAliveInterval 15']
        content = '\n'.join(lines) + '\n'
        if self.config.exists() and self.config.read_text() == content:
            return
        fd, name = tempfile.mkstemp(dir=self.state, prefix='ssh-config-')
        try:
            with os.fdopen(fd, 'w') as file:
                file.write(content)
            os.replace(name, self.config)
        finally:
            if Path(name).exists():
                Path(name).unlink()

    def get(self, key, default=''):
        return self.env.get(key, default)

    def hosts(self):
        return [h for h in self.get('ISU_HOSTS').split() if self.get('ISU_IP_' + h)]

    def guard_time(self):
        end = epoch(self.get('ISU_END_AT'))
        if end is not None and time.time() >= end - 60:
            fail('競技終了の60秒前から接続を拒否します（ISU_END_AT）')

    def canonical(self, probe=False):
        if probe and self.branch.startswith('probe/'):
            return
        if self.root != self.main or self.branch != 'main':
            fail('状態の変更は main の canonical checkout だけで行えます')

    def connection_timeout(self):
        end = epoch(self.get('ISU_END_AT'))
        return min(3600, max(0.1, end - 60 - time.time())) if end is not None else 3600

    def ssh(self, host, command, *, data=None, check=True, binary=False):
        self.guard_time()
        if not host or not self.get('ISU_IP_' + host):
            fail(f'ホストの IP がありません: {host}')
        p = sp.run(['ssh', '-F', str(self.config), *([] if data is not None else ['-n']), host, command],
                   input=data, stdout=sp.PIPE, text=not binary, **({} if binary else {'errors': 'replace'}), timeout=self.connection_timeout())
        if check and p.returncode:
            fail(f'{host}: ssh が失敗しました ({p.returncode}): {command}')
        return p

    def script(self, host, script, *args, check=True, binary=False):
        return self.ssh(host, 'bash -s -- ' + shlex.join(map(str, args)),
                        data=script.encode() if binary else script, check=check, binary=binary)

    def scp(self, src, dst):
        self.guard_time()
        run(['scp', '-q', '-F', self.config, src, dst], timeout=self.connection_timeout())

    def owner(self):
        return self.ssh(self.get('ISU_LOCK_HOST'), f'cat {Q(self.lockdir)}/owner 2>/dev/null || true').stdout.strip()

    def held(self):
        return bool(self.token) and self.owner().split('|')[0] == self.token

    def acquire(self, what):
        if self.token and self.held():
            return False
        self.token = secrets.token_hex(24)
        owner = f'{self.token}|{self.branch}|{int(time.time())}|{what}'
        p = self.ssh(self.get('ISU_LOCK_HOST'),
                     f'mkdir {Q(self.lockdir)} 2>/dev/null && printf %s {Q(owner)} > {Q(self.lockdir)}/owner', check=False)
        if p.returncode:
            fail('ロック取得失敗: ' + self.owner())
        return True

    def release(self):
        if not self.held():
            fail('ロックのトークンが一致しません')
        self.ssh(self.get('ISU_LOCK_HOST'), f'rm -rf -- {Q(self.lockdir)}')

    def force_release(self):
        self.canonical()
        self.ssh(self.get('ISU_LOCK_HOST'), 'rm -rf -- ' + Q(self.lockdir))
        path = self.state / 'bench-current'
        if path.exists():
            current = json.loads(path.read_text())
            meta_path = self.results / current['id'] / 'meta.env'
            meta = read_meta(meta_path)
            meta.update(STATE='aborted', RESULT='fail', ABORT_REASON='lock force-release')
            write_meta(meta_path, meta)
            self.summarize(current['id']); self.scoreboard()
            path.unlink()
            warn('残った計測の停止と回収: bin/isu-measure stop ' + current['id'])

    def code_sha(self):
        sha = output(['git', 'log', '-1', '--format=%h', '--', '.', ':!results', ':!docs'])
        if not sha:
            sha = output(['git', 'rev-parse', '--short', 'HEAD'])
        dirty = output(['git', 'status', '--porcelain', '--untracked-files=no', '--', '.', ':!results', ':!docs'])
        return sha + ('-dirty' if dirty else '')

    def commit(self, msg, paths):
        merge = output(['git', '-C', self.main, 'rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD'])
        pending = self.state / 'commit-pending.json'
        if pending.exists():
            paths = list(dict.fromkeys([*json.loads(pending.read_text())['paths'], *paths]))
        if Path(merge).exists():
            warn('マージ中です。記録を残しました。isu-score --regen の後で再コミットしてください')
            pending.write_text(json.dumps({'message': msg, 'paths': paths}, ensure_ascii=False))
            return
        try:
            run(['git', '-C', self.main, 'add', '--', *paths], stdout=sp.DEVNULL)
            changed = sp.run(['git', '-C', str(self.main), 'diff', '--cached', '--quiet', '--', *paths]).returncode
            if changed:
                run(['git', '-C', self.main, 'commit', '-q', '-m', msg, '--', *paths])
            if pending.exists():
                pending.unlink()
        except sp.CalledProcessError:
            pending.write_text(json.dumps({'message': msg, 'paths': paths}, ensure_ascii=False))
            warn('共有物のコミットが失敗しました。commit-pending.json を確認して再試行してください')

    def health_command(self, path=None):
        scheme = self.get('ISU_TARGET_SCHEME', 'https')
        port = self.get('ISU_TARGET_PORT') or ('443' if scheme == 'https' else '80')
        host = self.get('ISU_TARGET_HOST')
        return shlex.join(['curl', '-sk', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '5',
                           '--resolve', f'{host}:{port}:127.0.0.1', f'{scheme}://{host}:{port}{path or self.get("ISU_HEALTH_PATH", "/")}'])

    def health(self):
        if not self.get('ISU_NGINX_HOST'):
            warn('ISU_NGINX_HOST が空です。ヘルスチェックを飛ばします')
            return
        for attempt in range(3):
            p = self.ssh(self.get('ISU_NGINX_HOST'), self.health_command(), check=False)
            code = p.stdout.strip()
            if p.returncode == 0 and re.fullmatch('[234][0-9]{2}', code):
                if code.startswith('4'):
                    warn('ヘルスチェック HTTP ' + code)
                return
            if attempt < 2:
                time.sleep(float(self.get('ISU_HEALTH_RETRY_SECS', '2')))
        fail('ヘルスチェック失敗: HTTP ' + code)

    def db(self, query):
        kind = self.get('ISU_DB_KIND', 'none')
        prefix = self.get('ISU_DB_CMD', 'sudo')
        name = Q(self.get('ISU_DB_NAME'))
        if kind == 'mysql':
            return f'{prefix} mysql -NBr {name} -e {Q(query)}'
        if kind == 'postgresql':
            return f'{prefix} psql -X -v ON_ERROR_STOP=1 -d {name} -c {Q(query)}'
        if kind == 'sqlite':
            return f'{prefix} sqlite3 {Q(self.get("ISU_DB_PATH"))} {Q(query)}'
        fail('DB 操作が未対応です: ' + kind)

    def db_restart(self):
        if self.get('ISU_DB_RESTART_CMD'):
            return self.get('ISU_DB_RESTART_CMD')
        if self.get('ISU_DB_KIND') in ('none', 'sqlite'):
            return 'true'
        prefix = shlex.split(self.get('ISU_DB_CMD', 'sudo'))
        if 'docker' in prefix and 'exec' in prefix:
            at = prefix.index('exec')
            rest = prefix[at + 1:]
            # exec のオプションを含む場合は再起動コマンドを明示する。
            if not rest or rest[0].startswith('-'):
                fail('docker exec のオプション使用時は ISU_DB_RESTART_CMD を指定してください')
            return shlex.join(prefix[:at] + ['restart', rest[0]])
        return 'sudo systemctl restart ' + ('postgresql' if self.get('ISU_DB_KIND') == 'postgresql' else 'mysql')

    def durability(self, final=False):
        cmd = [self.root / 'tools/flags/flags', 'durability', '--decisions', self.root / 'docs/DECISIONS.md']
        if final:
            cmd.append('--final')
        for p in ['hosts', self.get('ISU_SRC_DIR'), *self.get('ISU_DEPLOY_PATHS').split()]:
            if p:
                cmd += ['--conf', self.root / p]
        run(cmd, stdout=sys.stderr)

    def deploy(self, a):
        self.canonical(getattr(a, 'probe', False))
        freeze = epoch(self.get('ISU_FREEZE_AT'))
        if freeze is not None and time.time() >= freeze and not a.after_freeze:
            fail('凍結後の反映は --after-freeze が必要です')
        sha = self.code_sha()
        if sha.endswith('-dirty') and not a.allow_dirty:
            fail('未コミットの変更があります（--allow-dirty）')
        self.durability()
        untracked = output(['git', 'ls-files', '--others', '--exclude-standard', '--', 'hosts', *self.get('ISU_DEPLOY_PATHS').split()])
        if untracked:
            warn('追跡していないファイルも反映します: ' + ' '.join(untracked.splitlines()))
        own = self.acquire('deploy ' + sha)
        try:
            if not a.no_build:
                run([self.root / 'bin/isu-build'], env={**os.environ, 'ISU_LOCK_TOKEN': self.token})
            for h in (a.hosts.split() if a.hosts else self.hosts()):
                if h not in self.hosts():
                    continue
                files = {}
                for layer in ['common', h]:
                    base = self.root / 'hosts' / layer
                    if base.exists():
                        for p in base.rglob('*'):
                            if p.is_file():
                                dest = '/' + p.relative_to(base).as_posix()
                                if dest.startswith('/home/isucon/'):
                                    dest = self.home + dest[len('/home/isucon'):]
                                files[dest] = p
                if h in self.get('ISU_APP_HOSTS').split():
                    for rel in self.get('ISU_DEPLOY_PATHS').split():
                        base = self.root / rel
                        if base.is_file():
                            files[self.home + '/' + rel] = base
                        elif base.is_dir():
                            for p in base.rglob('*'):
                                if p.is_file():
                                    files[self.home + '/' + p.relative_to(self.root).as_posix()] = p
                    if self.get('ISU_LANG') != 'none':
                        art = self.root / '.isu-artifact' / self.get('ISU_BIN_NAME')
                        if not art.is_file():
                            fail('ビルド成果物がありません: ' + str(art))
                        files[self.get('ISU_REMOTE_BIN')] = art
                # manifest はパスと sha。削除の対象は前回この道具が置いたものだけ。
                for dest in files:
                    if not dest.startswith(('/etc/', self.home + '/')) or '..' in Path(dest).parts or '\n' in dest or '\t' in dest:
                        fail('反映先が不正です: ' + dest)
                manifest = ''.join(f'{d}\t{hashlib.sha256(p.read_bytes()).hexdigest()}\n' for d, p in sorted(files.items()))
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode='w') as tar:
                    for dest, p in files.items():
                        tar.add(p, arcname=dest.lstrip('/'), recursive=False)
                    info = tarfile.TarInfo('manifest.new')
                    raw = manifest.encode(); info.size = len(raw)
                    tar.addfile(info, io.BytesIO(raw))
                self.ssh(h, f'mkdir -p {Q(self.home)}/.isu-stage && tar -C {Q(self.home)}/.isu-stage -xf -', data=archive.getvalue(), binary=True)
                script = r'''set -euo pipefail
HOME_DIR=$1; services=$2; sha=$3; kind=$4
stage=$HOME_DIR/.isu-stage
binary=$5
db_restart=$6
is_db_host=$7
db_service=$8
old=$HOME_DIR/.isu-manifest
changed_db=0
changed_sysctl=0
if [ -f "$old" ]; then
  while IFS=$'\t' read -r path hash; do
    if ! cut -f1 "$stage/manifest.new" | grep -Fxq -- "$path"; then
      case "$path" in /etc/*|"$HOME_DIR"/*) ;; *) echo '不正な旧 manifest' >&2; exit 1;; esac
      case "$path" in *../*) exit 1;; esac
      if [ -e "$path" ] || [ -L "$path" ]; then
        removed=$HOME_DIR/.isu-removed/$(date +%s)/${path#/}
        sudo mkdir -p "$(dirname "$removed")"
        sudo mv -- "$path" "$removed"
      fi
      case "$path" in /etc/mysql/*|/etc/postgresql/*) changed_db=1;; /etc/sysctl.d/*) changed_sysctl=1;; esac
    fi
  done < "$old"
fi
while IFS=$'\t' read -r path hash; do
  case "$path" in /etc/mysql/*|/etc/postgresql/*)
    if ! grep -Fxq -- "$path"$'\t'"$hash" "$old" 2>/dev/null; then changed_db=1; fi;;
    /etc/sysctl.d/*) if ! grep -Fxq -- "$path"$'\t'"$hash" "$old" 2>/dev/null; then changed_sysctl=1; fi;; esac
  sudo mkdir -p "$(dirname "$path")"
  # 既存ファイルの所有者とモードを保ち、内容だけを更新する。
  if [ "$path" = "$binary" ]; then
    sudo cp -p "$stage/${path#/}" "$path.new"
    sudo chmod +x "$path.new"
    sudo mv -f "$path.new" "$path"
  elif [ -e "$path" ]; then sudo sh -c 'cat "$1" > "$2"' sh "$stage/${path#/}" "$path"
  else
    sudo cp -p "$stage/${path#/}" "$path"
    case "$path" in /etc/*) sudo chown root:root "$path";; esac
  fi
done < "$stage/manifest.new"
restart() {
  if ! sudo systemctl restart "$@"; then sudo journalctl -n 50 --no-pager >&2; exit 1; fi
}
sudo systemctl daemon-reload
if [ "$changed_sysctl" = 1 ]; then sudo sysctl --system; fi
if [ "$changed_db" = 1 ] && [ "$is_db_host" = 1 ] && systemctl is-enabled "$db_service" >/dev/null 2>&1; then
  case "$kind" in mysql|postgresql)
    if ! bash -c "$db_restart"; then sudo journalctl -n 50 --no-pager >&2; exit 1; fi;; esac
fi
if systemctl is-active --quiet nginx; then sudo nginx -t; sudo systemctl reload nginx; fi
for s in $services; do sudo systemctl enable "$s"; restart "$s"; sudo systemctl is-active --quiet "$s"; done
cp "$stage/manifest.new" "$old"
rm -rf -- "$stage"
'''
                self.script(h, script, self.home, self.get('ISU_SERVICES_' + h), sha, self.get('ISU_DB_KIND'), self.get('ISU_REMOTE_BIN'), self.db_restart(), int(h == self.get('ISU_DB_HOST')), 'docker' if 'docker' in self.get('ISU_DB_CMD') else self.get('ISU_DB_KIND'))
            self.health()
            for h in (a.hosts.split() if a.hosts else self.hosts()):
                if h in self.hosts():
                    self.ssh(h, f'printf %s {Q(sha)} > {Q(self.home)}/.isu-deployed')
        finally:
            if own:
                self.release()
        print('deployed ' + sha, file=sys.stderr)

    def measure(self, action, id, capture=False, best_effort=False):
        r = self.results / id
        r.mkdir(parents=True, exist_ok=True)
        if action == 'wait' and not self.get('ISU_NGINX_HOST'):
            print('流量を見られません。ポータルで job を確認してください。'); return
        if action == 'wait':
            prev = None; started = False; quiet = 0
            until = time.monotonic() + 1800
            while time.monotonic() < until:
                self.guard_time()
                p = self.ssh(self.get('ISU_NGINX_HOST'), f'sudo sh -c {Q("wc -l < " + Q(self.get("ISU_ACCESS_LOG")))}')
                cur = int(p.stdout.strip())
                if prev is not None:
                    delta = max(0, cur - prev)
                    if delta > int(self.get('ISU_IDLE_RPS', '5')):
                        started = True; quiet = 0
                    elif started:
                        quiet += 1
                    if quiet >= int(self.get('ISU_IDLE_SECS', '10')):
                        print('流量が静かです。ポータルで job を確認してください。'); return
                prev = cur
                time.sleep(1)
            fail('流量待ちが時間切れです。ポータルで確認してください')
        for h in self.hosts():
            try:
                m = self.home + '/.isu-measure'
                app = h in self.get('ISU_APP_HOSTS').split()
                nginx = h == self.get('ISU_NGINX_HOST')
                dbhost = h == self.get('ISU_DB_HOST') and self.get('ISU_DB_KIND') != 'none'
                if action == 'start':
                    script = f'set -euo pipefail\nexport LC_ALL=C S_TIME_FORMAT=ISO TZ=UTC\nrm -rf -- {Q(m)}\nmkdir -p {Q(m)}\ndate +%s > {Q(m)}/t_start\ndate +%z > {Q(m)}/tz-offset\n'
                    if nginx:
                        script += f'if sudo test -e {Q(self.get("ISU_ACCESS_LOG"))}; then sudo truncate -s0 {Q(self.get("ISU_ACCESS_LOG"))}; fi\n'
                    if dbhost:
                        if self.get('ISU_DB_KIND') == 'mysql':
                            script += f'{self.get("ISU_DB_CMD", "sudo")} truncate -s0 {Q(self.get("ISU_SLOW_LOG"))}\n' + self.db('SET GLOBAL slow_query_log=ON') + '\n'
                        elif self.get('ISU_DB_KIND') == 'postgresql':
                            script += self.db('SELECT pg_stat_statements_reset()') + '\n'
                    for name, command in [('vmstat', 'vmstat -n -t 1'), ('pidstat', 'pidstat -H -u -h 1')]:
                        script += f'nohup timeout 1800 stdbuf -oL {command} > {Q(m)}/{name}.log 2>&1 < /dev/null &\necho $! > {Q(m)}/{name}.pid\n'
                    if app:
                        script += f'nohup sudo timeout -s INT 1800 perf record -F 99 -a -g -o {Q(m)}/perf.data > {Q(m)}/perf.out 2>&1 < /dev/null &\necho $! > {Q(m)}/perf.pid\n'
                        if self.get('ISU_PPROF_URL'):
                            url = self.get('ISU_PPROF_URL') + ('&' if '?' in self.get('ISU_PPROF_URL') else '?') + 'seconds=' + self.get('ISU_PPROF_SECONDS', '60')
                            script += f'nohup timeout 1800 curl -fsS -o {Q(m)}/cpu.pprof {Q(url)} > {Q(m)}/pprof.out 2>&1 < /dev/null &\necho $! > {Q(m)}/pprof.pid\n'
                        if capture:
                            port = self.get('ISU_APP_PORT')
                            if not port.isdigit():
                                fail('capture には ISU_APP_PORT が必要です')
                            mb = int(self.get('ISU_CAPTURE_MB', '1024'))
                            seconds = int(self.get('ISU_CAPTURE_SECS', '300'))
                            if mb <= 0 or seconds <= 0:
                                fail('ISU_CAPTURE_MB/SECS は正の数にしてください')
                            # RLIMIT_FSIZE は書き込み時に止める。ファイルを回して上書きしない。
                            inner = 'ulimit -f "$1"; exec tcpdump -Z root -i any -s 0 -U -w "$2" "tcp port $3"'
                            script += f'nohup sudo timeout -s INT {seconds} bash -c {Q(inner)} bash {mb * 1024} {Q(m)}/capture.pcap {port} > {Q(m)}/tcpdump.log 2>&1 < /dev/null &\necho $! > {Q(m)}/capture.pid\n'
                            script += f'for i in $(seq 50); do [ ! -s {Q(m)}/capture.pcap ] || break; sleep 0.1; done\n'
                            script += f'[ -s {Q(m)}/capture.pcap ] || {{ cat {Q(m)}/tcpdump.log >&2; exit 1; }}\n'
                    self.script(h, script)
                elif action == 'stop':
                    script = f'''set -euo pipefail
    export LC_ALL=C S_TIME_FORMAT=ISO TZ=UTC
    M={Q(m)}
    mkdir -p "$M"
    for f in "$M"/*.pid; do
      [ -f "$f" ] || continue
      sudo kill -INT "$(cat "$f")" 2>/dev/null || true
      # timeout が子を回収してファイルを閉じるまで待つ。
      for i in $(seq 600); do sudo kill -0 "$(cat "$f")" 2>/dev/null || break; sleep 0.1; done
      if sudo kill -0 "$(cat "$f")" 2>/dev/null; then echo "計測を停止できません: $f" >&2; exit 1; fi
      rm -f "$f"
    done
    '''
                    script += 'analysis() { local name=$1; shift; if ! "$@"; then echo "解析失敗: $name" >> "$M/analysis-warnings.txt"; fi; }\n'
                    if dbhost:
                        if self.get('ISU_DB_KIND') == 'mysql':
                            script += self.db('SET GLOBAL slow_query_log=OFF') + '\n'
                            script += f'{self.get("ISU_DB_CMD", "sudo")} cat {Q(self.get("ISU_SLOW_LOG"))} > "$M/slow.log"\n'
                            script += 'analysis pt-query-digest pt-query-digest --limit 30 "$M/slow.log" > "$M/slow.txt" 2> "$M/slow-errors.txt"\n'
                        elif self.get('ISU_DB_KIND') == 'postgresql':
                            script += 'analysis pg_stat_statements bash -c ' + Q(self.db('SELECT query, calls, total_exec_time, rows FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 30')) + ' > "$M/pg-stat-statements.txt" 2> "$M/pg-errors.txt"\n'
                    if nginx:
                        groups = self.root / 'conf/alp-groups.txt'
                        patterns = [l.strip() for l in groups.read_text().splitlines() if l.strip() and not l.lstrip().startswith('#')] if groups.exists() else []
                        group_arg = ' -m ' + Q(','.join(patterns)) if patterns else ''
                        script += f'if sudo test -e {Q(self.get("ISU_ACCESS_LOG"))}; then\nsudo cp {Q(self.get("ISU_ACCESS_LOG"))} "$M/access.log"\nsudo chmod a+r "$M/access.log"\n'
                        script += f'analysis alp sudo alp json --file "$M/access.log" --sort sum -r -o count,1xx,2xx,3xx,4xx,5xx,method,uri,min,avg,max,sum,p99{group_arg} > "$M/alp.txt" 2> "$M/alp-stderr.txt"\n'
                        script += f'analysis alp-errors sudo alp json --file "$M/access.log" --sort count -r -o count,method,uri,4xx,5xx{group_arg} -f \'Status >= 400\' > "$M/alp-errors.txt" 2> "$M/alp-errors-stderr.txt"\n'
                        script += 'fi\n'
                    if app:
                        script += 'if [ -s "$M/perf.data" ]; then analysis perf sudo perf report -i "$M/perf.data" --stdio --no-children -g none --sort comm,dso,sym --percent-limit 0.3 > "$M/perf-all.txt" 2> "$M/perf-errors.txt"; awk \'!/swapper/ && !/^#/ && NF\' "$M/perf-all.txt" > "$M/perf.txt"; fi\n'
                        for svc in self.get('ISU_SERVICES_' + h).split():
                            script += f'sudo journalctl -u {Q(svc)} --since "@$(cat "$M/t_start")" --no-pager -o short-iso >> "$M/journal-raw.txt"\n'
                    script += 'find "$M" -name \'capture.pcap*\' -type f -exec sudo chmod a+r {} +\n'
                    script += 'COPYFILE_DISABLE=1 tar -C "$M" --exclude=perf.data --exclude="capture.pcap*" -czf - .\n'
                    p = self.script(h, script, binary=True)
                    dest = r / h; dest.mkdir(exist_ok=True)
                    with tarfile.open(fileobj=io.BytesIO(p.stdout), mode='r:gz') as tar:
                        tar.extractall(dest, filter='data')
                    captures = self.ssh(h, f'find {Q(m)} -maxdepth 1 -type f -name \'capture.pcap*\' -exec basename {{}} \\;').stdout.splitlines()
                    for name in captures:
                        if re.fullmatch(r'capture\.pcap[0-9]*', name):
                            self.scp(f'{h}:{m}/{name}', str(dest / name))
                    if (dest / 'journal-raw.txt').exists():
                        (dest / 'journal.txt').write_text('\n'.join(journal_counts((dest / 'journal-raw.txt').read_text(errors='replace'))) + '\n')
                    if (dest / 'analysis-warnings.txt').exists():
                        warn(h + ': ' + (dest / 'analysis-warnings.txt').read_text(errors='replace').strip())
            except (RuntimeError, OSError, sp.SubprocessError, tarfile.TarError) as error:
                if not best_effort:
                    raise
                warn(h + ': 計測停止・回収に失敗: ' + str(error))
                with (r / 'measure-warnings.txt').open('a') as file:
                    file.write(h + ': ' + str(error) + '\n')
        if action == 'stop':
            self.window(id)
            self.summarize(id)

    def window(self, id):
        r = self.results / id
        counts = collections.Counter()
        offset = None
        for path in r.glob('*/access.log'):
            for line in path.read_text(errors='replace').splitlines():
                try:
                    row = json.loads(line)
                    if row.get('time'):
                        parsed = dt.datetime.fromisoformat(row['time'])
                        if parsed.tzinfo:
                            offset = parsed.strftime('%z')
                    t = float(row['msec']) if row.get('msec') else epoch(row['time'])
                    counts[int(t)] += 1
                except (ValueError, KeyError, TypeError):
                    continue
        active = sorted(t for t, n in counts.items() if n > int(self.get('ISU_IDLE_RPS', '5')))
        meta = read_meta(r / 'meta.env')
        for k in ['START', 'END', 'START_HMS', 'END_HMS']:
            meta.pop(k, None)
        if active:
            meta.update(START=str(active[0]), END=str(active[-1]))
            for key, t in [('START', active[0]), ('END', active[-1])]:
                meta['TZ_OFFSET'] = offset or '+0000'
                meta[key + '_HMS'] = dt.datetime.fromtimestamp(t, timezone(meta['TZ_OFFSET'])).strftime('%H:%M:%S')
        else:
            warn('アクセスログから窓を算出できません。START/END は未記録です')
        write_meta(r / 'meta.env', meta)

    def summarize(self, id):
        r = self.results / id; m = read_meta(r / 'meta.env')
        lines = [f'# Bench {id}', '', '| score | result | label | commit | window | note |', '|---|---|---|---|---|---|',
                 '| ' + ' | '.join(str(m.get(k, '?')).replace('|', '\\|').replace('\n', ' ') for k in ['SCORE', 'RESULT', 'LABEL', 'SHA']) + f' | {m.get("START_HMS", "?")}–{m.get("END_HMS", "?")} | ' + m.get('NOTE', '').replace('|', '\\|').replace('\n', ' ') + ' |']
        def text(path):
            return path.read_text(errors='replace') if path.exists() else ''
        def section(title, content):
            if content:
                lines.extend(['', '## ' + title, '', '```', *content, '```'])
        lines += ['', '## CPU（ベンチ中の平均 %）', '', '| host | us | sy | wa | idle | max run queue | samples |', '|---|---|---|---|---|---|---|']
        for h in self.hosts():
            host = r / h
            offset = text(host / 'tz-offset').strip() or m.get('TZ_OFFSET', '')
            cpu = cpu_table(text(host / 'vmstat.log'), m, offset)
            if cpu:
                lines.append('| ' + h + ' | ' + ' | '.join(f'{v:.0f}' for v in cpu) + ' |')
        lines += ['', '## プロセス別 CPU（窓の中の平均 %CPU。100 = 1コア）']
        for h in self.hosts():
            host = r / h
            offset = text(host / 'tz-offset').strip() or m.get('TZ_OFFSET', '')
            section(h + '・上位8プロセス', [f'{name:24s} {value:6.1f}' for name, value in processes(text(host / 'pidstat.log'), m, offset)])
            section(h + '・エンドポイント（alp・上位25）', text(host / 'alp.txt').splitlines()[:29])
            section(h + '・4xx / 5xx', text(host / 'alp-errors.txt').splitlines()[:15])
            section(h + '・スロークエリ（Profile）', profile(text(host / 'slow.txt')))
            section(h + '・PostgreSQL 上位', text(host / 'pg-stat-statements.txt').splitlines())
            section(h + '・perf 上位40', [line for line in text(host / 'perf.txt').splitlines() if 'swapper' not in line][:40])
            if not (host / 'perf.txt').exists():
                section(h + '・perf 計測の警告', text(host / 'perf.out').splitlines()[:15])
            section(h + '・アプリログ（件数順）', text(host / 'journal.txt').splitlines()[:20])
            section(h + '・解析の警告', text(host / 'analysis-warnings.txt').splitlines())
        section('ベンチ出力（末尾）', text(r / 'bench.log').splitlines()[-15:])
        section('計測停止・回収の警告', text(r / 'measure-warnings.txt').splitlines())
        bw = self.root / 'tools/bench-window/bench-window'
        logs = [f'{p.parent.name}={p}' for p in r.glob('*/access.log') if p.stat().st_size]
        if logs and bw.exists():
            args = [str(bw), *logs, '--last', self.get('ISU_WINDOW_LAST', '30s'), '--top', '15']
            if (self.root / 'conf/alp-groups.txt').exists():
                args += ['--group', str(self.root / 'conf/alp-groups.txt')]
            args += ['--tz', m.get('TZ_OFFSET', '+0000')]
            for cpu in r.glob('*/pidstat.log'):
                offset = text(cpu.parent / 'tz-offset').strip() or m.get('TZ_OFFSET', '')
                normalized = []
                for line in text(cpu).splitlines():
                    cells = line.split()
                    stamp = sample_time(cells[:1], m, offset)
                    normalized.append(str(int(stamp)) + ' ' + ' '.join(cells[1:]) if stamp is not None else line)
                path = cpu.with_name('pidstat-epoch.log')
                path.write_text('\n'.join(normalized) + '\n')
                args += ['--cpu', f'{cpu.parent.name}={path}']
            p = sp.run(args, capture_output=True, text=True)
            if p.returncode:
                warn('bench-window が失敗しました: ' + p.stderr)
            else:
                lines += ['', p.stdout]
        if m.get('ABORT_REASON'):
            lines += ['', '中止: ' + m['ABORT_REASON']]
        lines += ['', '## 生データ', '', *['- ' + p.relative_to(r).as_posix() for p in sorted(r.rglob('*')) if p.is_file() and p.name != 'SUMMARY.md']]
        (r / 'SUMMARY.md').write_text('\n'.join(lines) + '\n')

    def scoreboard(self):
        self.results.mkdir(exist_ok=True)
        lines = ['# SCOREBOARD', '', '| id | score | result | label | commit | branch | note |', '|---|---|---|---|---|---|---|']
        for p in sorted(self.results.glob('*/meta.env')):
            m = read_meta(p)
            vals = [f'[{p.parent.name}]({p.parent.name}/SUMMARY.md)', m.get('SCORE', '?'), m.get('RESULT', '?'), m.get('LABEL', ''), m.get('SHA', ''), m.get('BRANCH', ''), m.get('NOTE', '')]
            lines.append('| ' + ' | '.join(v.replace('|', '\\|').replace('\n', ' ') for v in vals) + ' |')
        (self.results / 'SCOREBOARD.md').write_text('\n'.join(lines) + '\n')

    def current(self, id=None, require_lock=True):
        path = self.state / 'bench-current'
        if not path.exists():
            fail('実行中のベンチがありません')
        data = json.loads(path.read_text())
        if id and data['id'] != id:
            fail('実行中の id と一致しません')
        if data.get('root') != str(self.root):
            fail('開始した checkout と一致しません')
        self.token = data['token']
        if require_lock and not self.held():
            fail('ベンチのロックが失われています')
        return data

    def bench(self, a):
        self.canonical(a.probe)
        if a.action in ['start', 'run']:
            if not a.note:
                fail('start/run には --note が必要です')
            if (self.state / 'bench-current').exists():
                fail('実行中のベンチがあります。finish または abort してください')
            if a.action == 'run' and not self.get('ISU_BENCH_CMD') and not a.wait_traffic:
                fail('run には ISU_BENCH_CMD または --wait-traffic が必要です')
            if a.capture and a.no_measure:
                fail('--capture と --no-measure は同時に使えません')
            # start が継承トークンで別のベンチに入れ子になることも拒否する。
            if self.token and self.held():
                fail('既にロックを保持しています。別のベンチを開始できません')
            self.acquire('bench')
            id = dt.datetime.now().strftime('%m%d-%H%M%S-%f') + '-' + self.code_sha()
            r = self.results / id; r.mkdir(parents=True)
            current = {'id': id, 'token': self.token, 'root': str(self.root), 'measure': not a.no_measure, 'measure_started': False, 'probe': a.probe}
            path = self.state / 'bench-current'
            path.write_text(json.dumps(current)); path.chmod(0o600)
            meta = {'ID': id, 'SHA': self.code_sha(), 'BRANCH': self.branch, 'NOTE': a.note, 'LABEL': a.label,
                    'MEASURE': str(not a.no_measure).lower(), 'STATE': 'preparing'}
            write_meta(r / 'meta.env', meta)
            try:
                if not a.no_deploy:
                    self.deploy(a)
                    if not a.skip_golden and list((self.root / 'golden').glob('*.txt')):
                        run([self.root / 'bin/isu-golden', 'check'], env={**os.environ, 'ISU_LOCK_TOKEN': self.token})
                else:
                    deployed = {}
                    for h in self.hosts():
                        deployed[h] = self.ssh(h, f'cat {Q(self.home)}/.isu-deployed').stdout.strip().split('|', 1)[0]
                    meta['SHA'] = ','.join(sorted(set(deployed.values())))
                    meta['DEPLOYED_HOSTS'] = json.dumps(deployed)
                meta['STATE'] = 'running'; write_meta(r / 'meta.env', meta)
                if not a.no_measure:
                    current['measure_started'] = True
                    path.write_text(json.dumps(current))
                    self.measure('start', id, a.capture)
            except Exception as error:
                meta['STATE'] = 'aborted'; meta['ABORT_REASON'] = 'start が失敗しました: ' + str(error)
                write_meta(r / 'meta.env', meta)
                # 部分的な計測開始でも止めてから解放する。止められなければ abort 用に状態を残す。
                if current['measure_started']:
                    try:
                        self.measure('stop', id)
                    except Exception as cleanup:
                        warn('計測停止を再試行してください: isu-bench abort ' + id + ' --reason start失敗')
                        raise error from cleanup
                self.summarize(id); self.scoreboard()
                self.release(); path.unlink()
                self.commit('bench start failed ' + id, ['results/' + id, 'results/SCOREBOARD.md'])
                raise
            print(id, flush=True)
            if a.action == 'start':
                return
            if self.get('ISU_BENCH_CMD'):
                p = self.ssh(self.get('ISU_BENCH_HOST'), self.get('ISU_BENCH_CMD'), check=False)
                (r / 'bench.log').write_text(p.stdout)
                rx = self.get('ISU_SCORE_REGEX')
                found = re.findall(rx, p.stdout) if rx else []
                if not found:
                    fail('スコアを読めません。ポータルを確認し finish/abort してください')
                a.score = float(found[-1]); a.result = 'pass' if p.returncode == 0 else 'fail'
            else:
                self.measure('wait', id)
                if a.score is None or not a.result:
                    print('ポータルを確認し、finish にスコアと合否を渡してください'); return
            a.action = 'finish'; a.id = id
        if a.action == 'wait':
            c = self.current(a.id)
            if not c['measure']:
                fail('--no-measure のベンチはポータルで確認してください')
            self.measure('wait', c['id']); return
        c = self.current(a.id, require_lock=a.action != 'abort'); id = c['id']; r = self.results / id
        if a.action == 'finish' and (a.score is None or not a.result):
            fail('finish には --score と --pass/--fail が必要です')
        if a.action == 'abort' and not a.reason:
            fail('abort には --reason が必要です')
        m = read_meta(r / 'meta.env')
        if a.action == 'abort':
            m.update(STATE='aborted', RESULT='fail', ABORT_REASON=a.reason)
        else:
            m.update(STATE='collecting', SCORE=str(a.score), RESULT=a.result, JOB=a.job or m.get('JOB', ''))
        write_meta(r / 'meta.env', m)
        self.scoreboard()
        if a.portal_file:
            shutil.copyfile(a.portal_file, r / 'portal.txt')
        if c['measure']:
            self.measure('stop', id, best_effort=a.action == 'abort')
        m = read_meta(r / 'meta.env')
        if a.action == 'finish':
            m['STATE'] = 'finished'
        write_meta(r / 'meta.env', m)
        self.summarize(id); self.scoreboard()
        if a.action == 'abort':
            try:
                if self.held():
                    self.release()
                else:
                    warn('ロックは失われています。他の処理のロックは解放しません')
            except (RuntimeError, OSError, sp.SubprocessError) as error:
                warn('ロックを確認できません: ' + str(error))
        else:
            self.release()
        (self.state / 'bench-current').unlink()
        self.commit('bench ' + id, ['results/' + id, 'results/SCOREBOARD.md'])
        print(r / 'SUMMARY.md')

    def doctor(self, a):
        if a.bootstrap:
            required = ['ISU_HOSTS', 'ISU_PORTAL_URL', 'ISU_END_AT', 'ISU_FREEZE_AT', 'ISU_BOOT_USER', 'ISU_BOOT_KEY']
            missing = [key for key in required if not self.get(key)]
            missing += ['ISU_IP_' + h for h in self.get('ISU_HOSTS').split() if not self.get('ISU_IP_' + h)]
            if missing:
                fail('bootstrap: isu.env を埋めてください: ' + ', '.join(missing))
            if not Path(self.get('ISU_BOOT_KEY')).is_file():
                fail('起動用の鍵がありません: ISU_BOOT_KEY')
            if epoch(self.get('ISU_FREEZE_AT')) >= epoch(self.get('ISU_END_AT')) - 60:
                fail('凍結は終了の60秒前より早くしてください')
            self.guard_time()
            print('doctor bootstrap: OK（ssh はまだ使いません）')
            return
        required = ['ISU_HOSTS', 'ISU_APP_HOSTS', 'ISU_LOCK_HOST', 'ISU_TARGET_HOST',
                    'ISU_TARGET_SCHEME', 'ISU_TARGET_PORT', 'ISU_PORTAL_URL', 'ISU_END_AT', 'ISU_FREEZE_AT', 'ISU_DB_KIND', 'ISU_LANG']
        if self.get('ISU_LANG') != 'none':
            required += ['ISU_SRC_DIR', 'ISU_BIN_NAME', 'ISU_REMOTE_BIN']
        if self.get('ISU_DB_KIND') != 'none':
            required += ['ISU_DB_HOST', 'ISU_DB_NAME']
        missing = [k for k in required if not self.get(k)]
        missing += ['ISU_IP_' + h for h in self.get('ISU_HOSTS').split() if not self.get('ISU_IP_' + h)]
        missing += ['ISU_SERVICES_' + h for h in self.get('ISU_APP_HOSTS').split() if not self.get('ISU_SERVICES_' + h)]
        if missing:
            fail('isu.env を埋めてください: ' + ', '.join(missing))
        if self.get('ISU_DB_KIND') not in ['mysql', 'postgresql', 'sqlite', 'none']:
            fail('ISU_DB_KIND は mysql|postgresql|sqlite|none です')
        if self.get('ISU_DB_KIND') == 'sqlite' and not self.get('ISU_DB_PATH'):
            fail('sqlite には ISU_DB_PATH が必要です')
        if self.get('ISU_TARGET_SCHEME') not in ['http', 'https'] or not self.get('ISU_TARGET_PORT').isdigit():
            fail('ISU_TARGET_SCHEME/PORT が不正です')
        if epoch(self.get('ISU_FREEZE_AT')) >= epoch(self.get('ISU_END_AT')) - 60:
            fail('凍結は終了の60秒前より早くしてください')
        for h in self.hosts():
            self.ssh(h, 'true')
            services = self.get('ISU_SERVICES_' + h).split()
            if a.final:
                if h == self.get('ISU_NGINX_HOST'):
                    services.append('nginx')
                if h == self.get('ISU_DB_HOST'):
                    if 'docker' in self.get('ISU_DB_CMD'):
                        services.append('docker')
                    elif self.get('ISU_DB_KIND') in ('mysql', 'postgresql'):
                        services.append(self.get('ISU_DB_KIND'))
            for svc in services:
                fragment = self.ssh(h, 'systemctl show -p FragmentPath --value ' + Q(svc)).stdout.strip()
                if not fragment.startswith('/'):
                    fail(h + ': サービスの実体がありません: ' + svc)
                if a.final:
                    self.ssh(h, 'systemctl is-enabled ' + Q(svc))
                    self.ssh(h, 'systemctl is-active ' + Q(svc))
            if not a.final:
                self.want(h, 'vmstat', 'pidstat')
                if h in self.get('ISU_APP_HOSTS').split():
                    self.want(h, 'perf', 'tcpdump')
            if a.final:
                deployed = self.ssh(h, f'cat {Q(self.home)}/.isu-deployed').stdout.strip()
                if deployed != self.code_sha() or deployed.endswith('-dirty'):
                    fail(h + ': 反映した sha とコードの sha が一致しません')
                self.ssh(h, f'! pgrep -f "[p]erf record|[p]idstat -u|[t]cpdump -i"')
                free = self.ssh(h, "df -Pk / | awk 'NR==2{print $4}'").stdout.strip()
                if not free.isdigit() or int(free) < 102400:
                    fail(h + ': ディスクの空きが100MiB未満、または読めません')
        n = self.get('ISU_NGINX_HOST')
        if n and self.ssh(n, 'command -v nginx', check=False).returncode == 0:
            self.ssh(n, 'sudo nginx -t')
            if not a.final:
                self.want(n, 'alp')
            logs = self.ssh(n, 'sudo cat /etc/nginx/conf.d/isu-log.conf 2>/dev/null || true').stdout
            if re.search(r'^\s*access_log\s+off\s*;', logs, re.M):
                if not a.final:
                    warn('アクセスログが off です。計測つきベンチの前に isu-logs on、最終ベンチは --no-measure を使ってください')
            elif a.final:
                fail('最終検査: isu-logs off でログを止めてください')
        else:
            warn('入口が nginx ではありません。先へ進みます。CPU・プロセス・perf・DB の計測はそのまま動きます。'
                 '入口のログを JSON で ISU_ACCESS_LOG に出せば、窓と capture flows も使えます。'
                 'ログが無ければ tcpdump の --capture で読むか、ISU_NGINX_HOST を空にします')
        self.health()
        if self.get('ISU_DB_KIND') != 'none':
            h = self.get('ISU_DB_HOST')
            self.ssh(h, self.db('SELECT 1'))
            if self.get('ISU_DB_KIND') == 'postgresql':
                self.ssh(h, self.db('SELECT count(*) FROM pg_stat_statements'))
            if self.get('ISU_DB_KIND') == 'mysql':
                if not a.final:
                    self.want(h, 'pt-query-digest')
                elif self.ssh(h, self.db('SELECT @@global.slow_query_log')).stdout.strip() != '0':
                    fail('slow_query_log を止めてください')
        if a.final:
            if (self.state / 'bench-current').exists() or self.owner():
                fail('実行中のベンチまたはロックがあります')
            self.durability(final=True)
        print('doctor: OK')

    def want(self, host, *tools):
        # 足りない計測の道具で止めない。無いまま進める道を伝える。
        for tool in tools:
            if self.ssh(host, 'command -v ' + Q(tool), check=False).returncode:
                warn(f'{host}: {tool} がありません。bin/isu-setup で入れられます。無ければその計測なしで進みます')

    def ssh_cli(self, a):
        if self.owner() and not a.during_hold:
            fail('HOLD 中の接続を拒否します（--during-hold）')
        command = shlex.join(a.command) if len(a.command) > 1 else ' '.join(a.command)
        if not command:
            fail('コマンドを明示してください')
        # 読み取りを止めない。リダイレクトや合成だけでは拒否しない。
        risky = (r'\b(?:rm|mv|tee|apt|apt-get|kill|pkill|reboot|shutdown|chmod|chown|truncate)\b|\bsed\b.*\s-i'
                 r'|\bsystemctl\b[^|&\n]*\b(?:restart|stop|start|enable|disable|reload)\b'
                 r'|\b(?:mysql|psql|sqlite3)\b.*\b(?:insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|load)\b')
        if re.search(risky, command, re.I):
            if self.root != self.main or self.branch != 'main':
                fail('変更系に見えるコマンドです。worktree からは変更しません。メインエージェントに返してください')
            warn('変更系に見えるコマンドを通します。残す変更は hosts/ と bin/isu-deploy に入れてください')
        p = self.ssh(a.host, command, check=False)
        print(p.stdout, end='')
        return p.returncode

    def close_mux(self, host):
        self.guard_time()
        sp.run(['ssh', '-F', str(self.config), '-O', 'exit', host], stdout=sp.DEVNULL, stderr=sp.DEVNULL, timeout=self.connection_timeout())

    def reboot_host(self, host):
        try:
            self.ssh(host, 'sudo systemctl reboot', check=False)
        finally:
            self.close_mux(host)

    def reboot(self, a):
        self.canonical(); own = self.acquire('reboot-test')
        try:
            hosts = self.hosts()
            if a.random:
                import random
                random.shuffle(hosts)
            before = {h: self.ssh(h, 'cat /proc/sys/kernel/random/boot_id').stdout.strip() for h in hosts}
            services = {h: set(self.ssh(h, 'systemctl list-units --type=service --state=running --no-legend --plain').stdout.splitlines()) for h in hosts}
            if a.random:
                for h in hosts:
                    self.reboot_host(h)
                    time.sleep(secrets.randbelow(15))
            else:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
                    list(pool.map(lambda h: self.reboot_host(h), hosts))
            for h in hosts:
                until = time.monotonic() + 300
                while True:
                    p = self.ssh(h, 'cat /proc/sys/kernel/random/boot_id', check=False)
                    if p.returncode == 0 and p.stdout.strip() and p.stdout.strip() != before[h]:
                        break
                    if time.monotonic() >= until:
                        fail(h + ': 再起動後に復帰しません')
                    self.close_mux(h)
                    time.sleep(5)
                self.ssh(h, 'timeout 180 systemctl is-system-running --wait', check=False)
                required = self.get('ISU_SERVICES_' + h).split()
                if h == self.get('ISU_NGINX_HOST'):
                    required.append('nginx')
                if h == self.get('ISU_DB_HOST'):
                    if 'docker' in self.get('ISU_DB_CMD'):
                        required.append('docker')
                    elif self.get('ISU_DB_KIND') in ('mysql', 'postgresql'):
                        required.append(self.get('ISU_DB_KIND'))
                for s in required:
                    self.ssh(h, 'systemctl is-enabled ' + Q(s) + ' && systemctl is-active ' + Q(s))
                after = set(self.ssh(h, 'systemctl list-units --type=service --state=running --no-legend --plain').stdout.splitlines())
                missing = [s for s in services[h] - after if not re.match(r'(user@|session-|getty@|serial-getty@|cloud-|snap)', s)]
                if missing:
                    fail(h + ': 起動しなかったサービス: ' + ', '.join(missing))
            self.health()
        finally:
            if own:
                self.release()
        print('再起動試験 OK。--no-deploy のベンチでデータとスコアを確認してください')

    def backup(self, label):
        if not re.fullmatch(r'[\w.-]+', label):
            fail('label が不正です')
        for h in self.hosts():
            self.script(h, 'set -euo pipefail\nmkdir -p "$1/backup"\npaths=()\nfor p in etc/nginx etc/mysql etc/postgresql etc/systemd/system; do [ ! -e "/$p" ] || paths+=("$p"); done\nsudo tar -C / -czf "$1/backup/$2-etc.tgz" "${paths[@]}"\n', self.home, label)
        kind = self.get('ISU_DB_KIND'); prefix = self.get('ISU_DB_CMD', 'sudo'); name = Q(self.get('ISU_DB_NAME'))
        if kind == 'none':
            return
        command = {'mysql': f'{prefix} mysqldump --single-transaction --routines {name}',
                   'postgresql': f'{prefix} pg_dump {name}', 'sqlite': f'{prefix} sqlite3 {Q(self.get("ISU_DB_PATH"))} .dump'}[kind]
        self.script(self.get('ISU_DB_HOST'), f'''set -euo pipefail
out={Q(self.home + '/backup/' + label + '-db.sql.gz')}
tmp=$(mktemp)
trap 'rm -f "$tmp" "$out.tmp"' EXIT
{command} > "$tmp"
[ -s "$tmp" ] || {{ echo '空のダンプです' >&2; exit 1; }}
gzip -1 < "$tmp" > "$out.tmp"
mv "$out.tmp" "$out"
''')

    def setup_db(self):
        self.canonical()
        kind = self.get('ISU_DB_KIND')
        h = self.get('ISU_DB_HOST')
        if kind in ('none', 'sqlite') or not h:
            return
        prefix = self.get('ISU_DB_CMD', 'sudo')
        if kind == 'mysql':
            script = r"""set -euo pipefail
SLOW=$1
DIR=/etc/mysql/mysql.conf.d
[ -d "$DIR" ] || DIR=/etc/mysql/conf.d
mkdir -p "$(dirname "$SLOW")"
touch "$SLOW"
if id mysql >/dev/null 2>&1; then chown mysql "$SLOW"; fi
printf '[mysqld]\nlong_query_time=0\nslow_query_log=0\nslow_query_log_file=%s\n' "$SLOW" > "$DIR/zz-isu.cnf"
"""
            tokens = shlex.split(prefix)
            if 'docker' in tokens and 'exec' in tokens and '-i' not in tokens:
                at = tokens.index('exec')
                tokens.insert(at + 1, '-i')
            command = shlex.join(tokens) + ' bash -s -- ' + Q(self.get('ISU_SLOW_LOG'))
            self.ssh(h, command, data=script)
            self.ssh(h, self.db_restart())
            self.ssh(h, self.db('SELECT @@long_query_time, @@slow_query_log_file'))
        elif kind == 'postgresql':
            # 既存の preload を保ち、pg_stat_statements を足す。
            query = "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements'"
            existing = self.ssh(h, prefix + ' psql -XAt -v ON_ERROR_STOP=1 -d ' + Q(self.get('ISU_DB_NAME')) + ' -c ' + Q('SHOW shared_preload_libraries')).stdout.strip()
            libraries = [x.strip().strip("'\"") for x in existing.split(',') if x.strip()]
            if 'pg_stat_statements' not in libraries:
                libraries.append('pg_stat_statements')
            if not all(re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', x) for x in libraries):
                fail('shared_preload_libraries を確認してください')
            query = "ALTER SYSTEM SET shared_preload_libraries = '" + ','.join(libraries) + "'"
            self.ssh(h, self.db(query))
            self.ssh(h, self.db_restart())
            self.ssh(h, self.db('CREATE EXTENSION IF NOT EXISTS pg_stat_statements'))
        else:
            fail('ISU_DB_KIND を設定してください')

    def export_seed(self, out):
        out = Path(out); out.mkdir(parents=True, exist_ok=True)
        kind = self.get('ISU_DB_KIND'); h = self.get('ISU_DB_HOST')
        prefix = self.get('ISU_DB_CMD', 'sudo'); name = Q(self.get('ISU_DB_NAME'))
        if kind == 'mysql':
            tables = self.ssh(h, self.db('SHOW TABLES')).stdout.split()
        elif kind == 'postgresql':
            tables = self.ssh(h, f'{prefix} psql -XAt -v ON_ERROR_STOP=1 -d {name} -c ' + Q("SELECT tablename FROM pg_tables WHERE schemaname='public'")).stdout.split()
        elif kind == 'sqlite':
            tables = self.ssh(h, self.db("SELECT name FROM sqlite_master WHERE type='table'")).stdout.split()
        else:
            fail('DB がありません')
        for t in tables:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', t):
                fail('テーブル名を安全に扱えません: ' + t)
            if kind == 'mysql':
                sql = "SET SESSION group_concat_max_len=1048576; SELECT group_concat(concat(quote(column_name), ', `', replace(column_name,'`','``'), '`') order by ordinal_position separator ', ') FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=" + "'" + t + "'"
                cols = self.ssh(h, self.db(sql)).stdout.strip()
                if not cols or cols == 'NULL':
                    fail('列の一覧を取得できません')
                command = self.db(f'SELECT JSON_OBJECT({cols}) FROM `{t}`')
            elif kind == 'postgresql':
                command = f'{prefix} psql -XAt -v ON_ERROR_STOP=1 -d {name} -c ' + Q(f'SELECT row_to_json(t) FROM "{t}" t')
            else:
                command = f'{prefix} sqlite3 -json {Q(self.get("ISU_DB_PATH"))} ' + Q(f'SELECT * FROM "{t}"')
            data = self.ssh(h, command).stdout
            if kind == 'sqlite':
                data = ''.join(json.dumps(row) + '\n' for row in json.loads(data or '[]'))
            for line in data.splitlines():
                json.loads(line)
            tmp = out / (t + '.tmp'); tmp.write_text(data); tmp.replace(out / (t + '.jsonl'))

    def intake(self, a):
        paths = output(['git', 'diff', '--no-renames', '--name-only', '--diff-filter=ACDMRTUXB', '-z', 'main...' + a.branch]).split('\0')
        paths = [p for p in paths if p]
        for p in paths:
            if not any(p == x.rstrip('/') or p.startswith(x.rstrip('/') + '/') for x in a.paths):
                fail('宣言したパスの外です: ' + p)
            if re.search(r'(^|/)(public|static|assets|dist|build)(/|$)|\.(?:css|js|png|jpe?g|svg|webp|gif|ico|woff2?)$', p) and not any(p == x.rstrip('/') or p.startswith(x.rstrip('/') + '/') for x in a.allow_static):
                fail('静的ファイルの変更です: ' + p)
        # ブランチの内容を一時領域で調べる。checkout は変えない。
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for p in paths:
                data = sp.run(['git', 'show', a.branch + ':' + p], capture_output=True)
                if data.returncode:
                    continue
                dest = Path(tmp) / p; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(data.stdout)
            run([self.root / 'tools/flags/flags', 'durability', '--conf', tmp, '--decisions', '/dev/null'])
        print('intake: OK')

def parser(name):
    p = argparse.ArgumentParser(prog=name)
    if name == 'isu-bench':
        p.add_argument('action', choices=['start', 'finish', 'abort', 'wait', 'run'])
        p.add_argument('id', nargs='?')
        p.add_argument('--note', default=''); p.add_argument('--label', default='')
        for x in ['no-deploy', 'no-measure', 'capture', 'allow-dirty', 'skip-golden', 'wait-traffic', 'probe', 'after-freeze', 'no-build']:
            p.add_argument('--' + x, action='store_true')
        p.add_argument('--hosts', default='')
        p.add_argument('--score', type=float); g = p.add_mutually_exclusive_group()
        g.add_argument('--pass', dest='result', action='store_const', const='pass'); g.add_argument('--fail', dest='result', action='store_const', const='fail')
        p.add_argument('--job'); p.add_argument('--portal-file'); p.add_argument('--reason')
    elif name == 'isu-deploy':
        for x in ['no-build', 'allow-dirty', 'after-freeze']:
            p.add_argument('--' + x, action='store_true')
        p.add_argument('--hosts', default='')
    elif name == 'isu-measure':
        p.add_argument('action', choices=['start', 'stop', 'wait']); p.add_argument('id')
    elif name == 'isu-score':
        p.add_argument('id', nargs='?'); p.add_argument('score', nargs='?', type=float); p.add_argument('note', nargs='?')
        p.add_argument('--regen', action='store_true'); p.add_argument('--job')
        g = p.add_mutually_exclusive_group(); g.add_argument('--pass', dest='result', action='store_const', const='pass'); g.add_argument('--fail', dest='result', action='store_const', const='fail')
    elif name == 'isu-lock':
        p.add_argument('action', choices=['status', 'acquire', 'release', 'force-release'], default='status', nargs='?'); p.add_argument('what', default='manual', nargs='?')
    elif name == 'isu-doctor':
        g = p.add_mutually_exclusive_group()
        g.add_argument('--final', action='store_true'); g.add_argument('--bootstrap', action='store_true')
    elif name == 'isu-ssh':
        p.add_argument('--write', action='store_true'); p.add_argument('--during-hold', action='store_true'); p.add_argument('host'); p.add_argument('command', nargs=argparse.REMAINDER)
    elif name == 'isu-reboot-test':
        p.add_argument('--yes', action='store_true', required=True); p.add_argument('--random', action='store_true')
    elif name == 'isu-intake':
        p.add_argument('branch'); p.add_argument('--paths', nargs='+', required=True); p.add_argument('--allow-static', action='append', default=[])
    elif name == 'isu-backup':
        p.add_argument('label', nargs='?', default='initial')
    elif name == 'isu-export-seed':
        p.add_argument('out', nargs='?', default='seed')
    elif name == 'isu-summarize':
        p.add_argument('id')
    elif name == 'guard':
        p.add_argument('kind', choices=['time', 'canonical', 'health'])
    return p

def main(name=None, argv=None):
    name = name or Path(sys.argv[0]).name
    if name == 'isu_runtime.py':
        if sys.argv[1] == 'ssh-config':
            Kit()
            return 0
        if sys.argv[1] == 'setup-db':
            Kit().setup_db()
            return 0
        if sys.argv[1] == 'inventory-db':
            k = Kit()
            kind = k.get('ISU_DB_KIND')
            queries = {'mysql': ['SELECT VERSION()', 'SHOW TABLE STATUS', 'SHOW GLOBAL VARIABLES'], 'postgresql': ['SELECT version()', 'SELECT * FROM pg_stat_user_tables', 'SELECT * FROM pg_indexes', 'SHOW ALL'], 'sqlite': ['SELECT sqlite_version()', 'SELECT sql FROM sqlite_master']}
            for query in queries[kind]:
                print(k.ssh(k.get('ISU_DB_HOST'), k.db(query)).stdout)
            prefix = k.get('ISU_DB_CMD', 'sudo')
            name = Q(k.get('ISU_DB_NAME'))
            command = {'mysql': f'{prefix} mysqldump --no-data {name}', 'postgresql': f'{prefix} pg_dump --schema-only {name}', 'sqlite': f'{prefix} sqlite3 {Q(k.get("ISU_DB_PATH"))} .schema'}[kind]
            (k.root / 'docs/inventory/schema.sql').write_text(k.ssh(k.get('ISU_DB_HOST'), command).stdout)
            return 0
        mapping = {'time-guard': 'time', 'canonical-guard': 'canonical', 'health-guard': 'health'}
        argv = [mapping[sys.argv[1]]]; name = 'guard'
    a = parser(name).parse_args(argv)
    try:
        k = Kit()
        if name == 'isu-bench':
            if a.score is not None and not math.isfinite(a.score):
                fail('スコアは有限の数にしてください')
            if a.id and not re.fullmatch(r'[\w.-]+', a.id):
                fail('id が不正です')
            k.bench(a)
        elif name == 'isu-deploy': k.deploy(a)
        elif name == 'isu-measure':
            k.canonical(); k.measure(a.action, a.id)
        elif name == 'isu-summarize': k.summarize(a.id)
        elif name == 'isu-doctor': k.doctor(a)
        elif name == 'isu-ssh': return k.ssh_cli(a)
        elif name == 'isu-intake': k.intake(a)
        elif name == 'isu-reboot-test': k.reboot(a)
        elif name == 'isu-backup': k.canonical(); k.backup(a.label)
        elif name == 'isu-export-seed': k.export_seed(a.out)
        elif name == 'isu-score':
            if a.regen:
                k.scoreboard()
            elif a.id:
                if a.score is None or not math.isfinite(a.score): fail('スコアが必要です')
                path = k.results / a.id / 'meta.env'
                if not path.exists(): fail('結果がありません')
                m = read_meta(path); m['SCORE'] = str(a.score)
                if a.note is not None: m['NOTE'] = a.note
                if a.result: m['RESULT'] = a.result
                if a.job: m['JOB'] = a.job
                write_meta(path, m); k.summarize(a.id); k.scoreboard()
                k.commit('score ' + a.id, ['results/' + a.id, 'results/SCOREBOARD.md'])
            else:
                k.scoreboard(); print((k.results / 'SCOREBOARD.md').read_text())
        elif name == 'isu-lock':
            if a.action == 'status': print(k.owner() or 'free')
            elif a.action == 'acquire':
                k.canonical(); k.acquire(a.what); print('ISU_LOCK_TOKEN=' + k.token)
            elif a.action == 'release': k.release()
            else:
                k.force_release()
        elif name == 'guard':
            if a.kind == 'time': k.guard_time()
            elif a.kind == 'canonical': k.canonical()
            else: k.health()
        return 0
    except (RuntimeError, OSError, ValueError, sp.CalledProcessError, sp.TimeoutExpired, tarfile.TarError) as e:
        print('[err] ' + str(e), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
