#!/usr/bin/env python3
"""テスト専用。実際の ssh と sudo を呼ばない。"""
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile

name = Path(sys.argv[0]).name
args = sys.argv[1:]
server = Path(os.environ['FAKE_SERVER'])
with (server / 'commands.jsonl').open('a') as f:
    f.write(json.dumps([name, args]) + '\n')
if name == 'ssh':
    while args and args[0].startswith('-'):
        flag = args.pop(0)
        if flag in ['-F', '-o', '-O']:
            args.pop(0)
    host = args.pop(0)
    command = ' '.join(args)
    home = server / host / 'home'
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'HOME': str(home), 'FAKE_HOST': host}
    if 'tar -C ' in command and '-xf -' in command:
        raw = sys.stdin.buffer.read()
        dst = Path(shlex.split(command.split('tar -C ', 1)[1])[0])
        dst.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            tar.extractall(dst, filter='data')
        manifest = dst / 'manifest.new'
        if manifest.exists():
            manifest.write_text(manifest.read_text().replace('/etc/', str(server / host / 'etc') + '/'))
            if (dst / 'etc').exists():
                new = dst / str(server / host / 'etc').lstrip('/')
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst / 'etc'), str(new))
        sys.exit(0)
    data = sys.stdin.read() if 'bash -s' in command else None
    def localize(s):
        for prefix in ['/etc/', '/var/', '/tmp/']:
            s = s.replace(prefix, str(server / host) + prefix)
        s = s.replace('"/$p"', '"' + str(server / host) + '/$p"')
        s = s.replace(' /etc ', ' ' + str(server / host / 'etc') + ' ')
        s = s.replace(' /etc\n', ' ' + str(server / host / 'etc') + '\n')
        return s
    if data is not None:
        data = localize(data)
    command = localize(command)
    p = subprocess.run(['bash', '-c', command], input=data, text=True, env=env, cwd=home)
    sys.exit(p.returncode)
if name == 'scp':
    src, dst = args[-2:]
    host, dest = dst.split(':', 1)
    shutil.copy2(src, dest)
    sys.exit(0)
if name == 'sudo':
    # テスト外の root 権限やプロセスへ触れない。
    if args and args[0] == 'install':
        Path(args[-1]).mkdir(parents=True,exist_ok=True); sys.exit(0)
    if args[:2] == ['kill', '-0']:
        sys.exit(1)
    if args and args[0] in ['kill', 'pkill', 'sysctl', 'apt-get', 'chown']:
        sys.exit(0)
    remote = server / os.environ.get('FAKE_HOST', 'host')
    for i, arg in enumerate(args):
        if arg == '/':
            args[i] = str(remote)
        elif arg.startswith(('/etc/', '/var/', '/tmp/')):
            args[i] = str(remote) + arg
    sys.exit(subprocess.run(args).returncode)
if name == 'mktemp':
    import tempfile
    directory=server/os.environ.get('FAKE_HOST','host')/'tmp'
    if '-d' in args:
        print(tempfile.mkdtemp(dir=directory))
    else:
        fd,path=tempfile.mkstemp(dir=directory);os.close(fd);print(path)
elif name == 'chown':
    pass
elif name == 'uname':
    print('aarch64' if '-m' in args else 'Linux')
elif name == 'sed':
    # setup の GNU sed のふるまい。follow-symlinks の有無を再現する。
    target=Path(args[-1])
    text=target.read_text()
    text=re.sub(r'(?m)^(\s*)(access_log\s+)',r'\1# isu-setup: \2',text)
    if target.is_symlink() and '--follow-symlinks' not in args:
        target.unlink()
    target.write_text(text)
elif name == 'systemctl':
    if args[0] == 'restart' and os.environ.get('FAKE_RESTART_FAIL'):
        print('起動失敗', file=sys.stderr); sys.exit(1)
    if args[0] == 'show':
        print('/etc/systemd/system/app.service')
    elif args[0] == 'list-units':
        print('app.service loaded active running app')
    elif args[0] == 'reboot':
        (server / 'boot').write_text('changed')
    else:
        print('active' if args[0] == 'is-active' else 'enabled')
elif name == 'curl':
    if '-o' in args and args[args.index('-o')+1] != '/dev/null':
        Path(args[args.index('-o')+1]).write_text('{"ok":true}')
    print(os.environ.get('FAKE_HTTP', '200'), end='')
    if os.environ.get('FAKE_CURL_FAIL'):
        sys.exit(7)
elif name == 'nginx':
    if '-T' in args:
        print('access_log off;' if os.environ.get('FAKE_LOGS_OFF') else 'access_log /var/log/nginx/access.log isu_json;')
elif name == 'mysql':
    sql = ' '.join(args)
    if os.environ.get('FAKE_MYSQL_FAIL'):
        print('mysql failure', file=sys.stderr); sys.exit(1)
    if 'SHOW TABLES' in sql: print('items')
    elif 'group_concat' in sql: print("'id', `id`")
    elif 'JSON_OBJECT' in sql: print('{"id":1}')
    elif 'slow_query_log' in sql and 'SELECT' in sql: print('0')
    elif 'SELECT' in sql: print('1')
elif name == 'psql':
    query=' '.join(args)
    if 'SHOW shared_preload_libraries' in query: print('auto_explain')
    elif 'SELECT tablename' in query: print('items')
    elif 'row_to_json' in query: print('{"id":1}')
    else: print('1')
elif name == 'sqlite3':
    print('1')
elif name in ['mysqldump', 'pg_dump']:
    if os.environ.get('FAKE_DUMP_FAIL'):
        sys.exit(1)
    if not os.environ.get('FAKE_DUMP_EMPTY'):
        print('CREATE TABLE items(id int);')
elif name == 'alp':
    print('alp v1.0.22' if '--version' in args else 'alp table')
elif name == 'journalctl':
    print('journal: 起動に失敗しました')
elif name == 'codex':
    print('review stderr', file=sys.stderr)
    text = sys.stdin.read()
    (server / 'review-input').write_text(text)
    sys.exit(int(os.environ.get('FAKE_CODEX_EXIT', '0')))
elif name == 'cat':
    if args == ['/proc/sys/kernel/random/boot_id']:
        print((server / 'boot').read_text() if (server / 'boot').exists() else 'initial')
    else:
        sys.exit(subprocess.run(['/bin/cat', *args]).returncode)
elif name == 'timeout':
    if 'tcpdump' in args:
        target=Path(args[args.index('-w')+1]+'0')
        target.write_bytes(b'pcap header')
    # 計測の子プロセスを作らない。
    sys.exit(0)
elif name == 'docker':
    if args and args[0] == 'exec':
        if args[1:2] == ['-i']:
            args.pop(1)
        if args[1:2] == ['db']:
            sys.exit(subprocess.run(args[2:]).returncode)
else:
    print(name + ' fake output')
