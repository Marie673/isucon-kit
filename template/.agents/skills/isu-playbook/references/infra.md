# インフラの定石（nginx・カーネル・複数台）

詳しくは `docs/tuning/README.md`。

## nginx（hosts/<host>/etc/nginx/ を編集）

- 静的ファイル（`/assets/`, `/images/`, favicon 等）は nginx が直接返す（`location` + `root` / `try_files`）。`expires` / `Cache-Control` を付けると 304 が返る（仕様が許すか RULES.md で確認）
- アプリへの upstream は keepalive を張る:

```nginx
upstream app {
  server 127.0.0.1:8080;
  keepalive 128;
  keepalive_requests 1000000;
}
location /api/ {
  proxy_pass http://app;
  proxy_http_version 1.1;
  proxy_set_header Connection "";
  proxy_set_header Host $host;
}
```

- `worker_processes auto; worker_rlimit_nofile 65536; events { worker_connections 8192; }`
- DB に入っている画像は、初回アクセスか initialize でファイルに書き出し、nginx の `try_files $uri @app` で配信する
- gzip はテキストの大きいレスポンスにだけ（CPU とのトレードオフを計測で確認）

## カーネル / systemd（hosts/<host>/etc/sysctl.d/99-isu.conf と unit）

```conf
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_tw_reuse = 1
net.core.netdev_max_backlog = 65535
```

- unit に `LimitNOFILE=1006500`（EMFILE 対策。実例: 当日の不具合はこれで吸収された）
- sysctl.d の内容が変わったときはデプロイで `sysctl --system` が走る
- 効果は劇的ではないことが多い（土台）。計測でボトルネックになってから入れる

## 複数台構成

- 典型: isu1 = nginx + app、isu2 = DB、isu3 = app（nginx の upstream に2台）
- 決める前に「どれが CPU を食っているか」を SUMMARY.md の CPU 表で確認する
- アプリを複数台にすると、メモリキャッシュやインメモリ状態が台ごとに分かれる → 共有が要る状態はどの台が持つか決める
- 構成を変えたら isu.env（`ISU_SERVICES_<host>`、`ISU_DB_HOST`、`ISU_APP_HOSTS`）と docs/ARCH.md を同時に直す
- 使わなくなったサービスは `systemctl disable` まで（再起動試験で余計なものが起きない / 必要なものが起きる）
- メモリに持つ形にすると、台の分け方も変わる。1 台に集めるとネットワークの往復が消えるが、区分ごとに台が要る問題では使えない。制約から考える
