# 02 OS とカーネル

当日のマニュアルが優先する。OS の設定のうち、変えてよいもの・変えてはいけないもの（メモリや CPU の増減、運営用ユーザー、再起動の妨害など）はマニュアルで決まる。

## この章で扱う範囲

- カーネルパラメータ（sysctl）: ネットワーク、ファイル記述子、メモリ、書き戻し。
- プロセスの上限: ulimit、systemd の `LimitNOFILE`・`TasksMax`。
- 不要なサービスの停止、journald のログ量。
- CPU（governor、irqbalance、THP、NUMA）、ディスク（`noatime`、I/O スケジューラ、tmpfs）、メモリ（swap、OOM）。
- systemd の unit の書き方（再起動・依存・CPU の取り分）。
- 時刻と DNS。

扱わないもの: nginx（03 章）、DB の設定、アプリのコード、台の配置（11 章）、HTTP クライアントと MTU・帯域（13 章）。

## 最初に見るもの（当日の 5 分チェックリスト）

OS の設定は土台で、効果は劇的でないことが多い（kazeburo は自分の作業を「足場を組んだだけ」と書いている。[ISUCON4 事後練習の記録](https://kazeburo.hatenablog.com/entry/2014/10/14/170129)）。計測でボトルネックになってから入れる。ただし次の項目は、始めに 5 分で見ておくと、後で「CPU が空いているのに遅い」で時間を失わない。

| 見るもの | コマンド | 異常のしるし |
| --- | --- | --- |
| 待ち行列の溢れ | `nstat -az \| grep -E 'ListenOverflows\|ListenDrops'` | 0 でない（増え続ける）。01 章（計測）の窓の前後で差を見る |
| listen の溜まり具合 | `ss -ltn` | `Recv-Q` が `Send-Q` に張り付く（listen ソケットでは `Recv-Q` が accept 待ちの数、`Send-Q` が backlog の上限という読み方は一般的な解釈で、ss(8) の man には書かれていない。出典未確認） |
| ソケットの数と状態 | `ss -s` | TIME_WAIT が数万、estab が極端に多い |
| FD の上限 | `cat /proc/$(pidof -s <proc>)/limits \| grep 'open files'` | Soft が 1024。ログに `Too many open files` |
| カーネルログ | `dmesg -T \| tail -50`、`journalctl -k -p warning` | `nf_conntrack: table full`、`Out of memory`、`TCP: request_sock_TCP: Possible SYN flooding` |
| CPU・メモリ・ディスク | `top`、`free -m`、`vmstat 1`、`iostat -x 1` | `si/so`（swap）、`wa`（I/O 待ち）、`st`（steal） |
| 余計に動いているもの | `systemctl list-units --type=service --state=running`、`ps aux --sort=-%cpu \| head` | 使っていないサービス・初期実装の残りが CPU を食う |
| 再起動後も残る設定か | 設定を `/etc/sysctl.d/*.conf`・systemd drop-in に置いたか | `sysctl -w` や `echo >` だけでは再起動で消える |

読む資料: [ss(8)](https://man7.org/linux/man-pages/man8/ss.8.html)、[nstat と SNMP カウンタ](https://docs.kernel.org/networking/snmp_counter.html)。

## sysctl の入れ方（共通）

### 設定の置き場所と永続化

- **効く条件**: どの sysctl を入れるときも。
- **やり方**: `/etc/sysctl.d/90-<名前>.conf` に書き、`sudo sysctl --system` で反映する。`sysctl -w` や `echo > /proc/sys/...` だけでは再起動で消える。
  ```conf
  # /etc/sysctl.d/90-isu.conf
  net.core.somaxconn = 65535
  ```
- **確かめ方**: `sysctl net.core.somaxconn` で値を読む。**再起動試験の後にもう一度読む**。
- **出典**: 出典未確認（`sysctl.d` の仕様は man sysctl.d で確かめる）。

### コピペの設定を丸ごと入れない

- **効く条件**: 参加記や検索結果の設定例を入れたくなったとき。
- **やり方**: 1 項目ずつ、症状（上の 5 分チェックの数字）に対応するものだけ入れる。カーネルのバージョンで既定値や存在が変わる。
  - `somaxconn` の既定は Linux 5.4 より前が 128、5.4 以降は 4096（[ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html)）。
  - `tcp_tw_reuse` の既定は 2（ループバックだけ有効）。1 にするとループバック以外にも効く（同じ出典）。
  - `tcp_tw_recycle` は古い記事にあるが、現行カーネルには無い（Linux 4.12 で削除。出典未確認）。入れようとしてはいけない。
- **確かめ方**: `sysctl <キー>` で現在の値を先に読んでから、変えるか決める。変えたあとはベンチ前後で `nstat` と `ss -s` を比べる。
- **危険**: 古い値をそのまま流用すると、既定より悪くなる値もある。入れた設定は「何の症状に入れたか」を commit に書く。

## ネットワークの sysctl

### 接続の受付待ち行列（somaxconn・tcp_max_syn_backlog・アプリの backlog）

- **効く条件**: 負荷の立ち上がりで接続が失敗する・遅い。`nstat` の `TcpExtListenOverflows`・`TcpExtListenDrops` が増える。`ss -ltn` で listen ソケットの `Recv-Q` が `Send-Q` に近い（読み方は出典未確認、上の表を参照）。nginx やアプリが 502・接続拒否・SYN の再送で 1 秒の遅れを出す。
- **やり方**: カーネルとアプリの両方を揃えて上げる。片方だけでは効かない。
  ```conf
  net.core.somaxconn = 65535
  net.ipv4.tcp_max_syn_backlog = 65535
  ```
  - nginx は `listen 80 backlog=4096;` のように明示する。指定しなければ Linux では 511（[nginx listen](https://nginx.org/en/docs/http/ngx_http_core_module.html#listen)）。
  - アプリの backlog は言語・サーバーごとに指定する（kazeburo の実例は start_server の `--backlog 16384`）。
  - MySQL は `back_log`（既定は `max_connections` と同じ。OS の上限を超えても効かない）。
  - Go の `net.Listen` と Rust の tokio の既定 backlog は出典未確認。使う版の既定を確かめる。
- **確かめ方**: `ss -ltn` の `Send-Q`（実際に効いている backlog）が狙いの値になっているか。`nstat -az | grep -i listen` がベンチ中に増えないこと。
- **危険**: 待ち行列を大きくすると、処理が追いつかない場合に待ち時間（遅い応答）が増える。「溢れて失敗する」が「遅いが成功する」に変わるだけで、ベンチのタイムアウトで別の失敗になることがある。処理能力そのものが足りないときは効かない。失格・永続化への影響は特になし。
  - [ip-sysctl（somaxconn、tcp_max_syn_backlog）](https://docs.kernel.org/networking/ip-sysctl.html)
  - [snmp_counter（ListenOverflows、ListenDrops）](https://docs.kernel.org/networking/snmp_counter.html)
  - [kazeburo ISUCON4 事後練習](https://kazeburo.hatenablog.com/entry/2014/10/14/170129)（somaxconn=32768、start_server `--backlog 16384`）
  - puma で 502 が出て SOMAXCONN と backlog を調整。単独効果は不明

### tcp_syncookies と tcp_abort_on_overflow は触らない

- **効く条件**: backlog を上げても溢れが出るとき、設定を疑いたくなる。
- **やり方**: 触らない。`tcp_syncookies` の既定は 1（SYN の溢れで cookie を送る）。`tcp_abort_on_overflow` の既定は 0（溢れたら無視して再送を待つ）。
- **確かめ方**: `sysctl net.ipv4.tcp_syncookies net.ipv4.tcp_abort_on_overflow`。
- **危険**: カーネル文書は `tcp_abort_on_overflow=1` を「クライアントを傷つけうる」とし、リスナーをこれ以上速くできないと確信できるときだけに限る。`tcp_syncookies` は TCP の拡張が使えなくなる副作用がある。ベンチのクライアントは再送で動くので、1 にして失敗が増えるだけになりやすい。
- **出典**: [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html)

### netdev_max_backlog（NIC の受信キュー）

- **効く条件**: パケットが多く（小さい要求が大量）、`/proc/net/softnet_stat` の第 2 列（drop）が増える。listen の溢れとは別の層。
- **やり方**: `net.core.netdev_max_backlog = 5000` 以上。
- **確かめ方**: `awk '{print $2}' /proc/net/softnet_stat`（16 進。ベンチ前後で増えるか）。
- **危険**: 特になし。メモリを少し食う。
- **出典**: [kernel sysctl net](https://docs.kernel.org/admin-guide/sysctl/net.html)（「入力側のキューのパケット数」）。実例は [kazeburo](https://kazeburo.hatenablog.com/entry/2014/10/14/170129)（8192）。`softnet_stat` の読み方は出典未確認。

### ローカルポートの枯渇（ip_local_port_range）

- **効く条件**: nginx からアプリ・memcached・DB への接続を毎回作り直している（upstream の keepalive が無い）。`ss -s` で TIME_WAIT が数万。nginx の error.log に `Cannot assign requested address`・`connect failed (99)`。
- **やり方**: 根本は接続を使い回す（03 章の upstream keepalive、13 章の HTTP クライアントのプール）。そのうえで範囲を広げる。
  ```conf
  net.ipv4.ip_local_port_range = 10000 65535
  ```
  既定は 32768〜60999（約 28,000 ポート）。1 つの宛先（IP:ポート）への同時接続は、この数で頭打ちになる。
  - 範囲に、アプリや DB の待ち受けポートが入らないようにする。入れたくない番号は `net.ipv4.ip_local_reserved_ports` で除外できる。
- **確かめ方**: `ss -s`（TIME_WAIT の数）、`ss -tan state time-wait | wc -l`、ベンチ中の error.log。
- **危険**: 範囲に待ち受けポートが入ると、起動時に bind できないことがある。範囲を広げても、接続の使い回しをしなければ TIME_WAIT は減らない。
- **出典**: [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html)（`ip_local_port_range`、`ip_local_reserved_ports`）。[fujiwara 初回 ISUCON](https://sfujiwara.hatenablog.com/entry/20110829/1314597283)（nginx→memcached のポート枯渇に、範囲の拡大と TIME_WAIT の短縮で対処）。

### TIME_WAIT 対策（tcp_tw_reuse・tcp_fin_timeout・tcp_max_tw_buckets）

- **効く条件**: 上のポート枯渇のとき。短命の接続が大量で、接続の使い回し（keepalive）ができない相手（ベンチのクライアントが keepalive を切っている場合の、クライアントからの接続は自分では変えられない）。
- **やり方**: まず `tcp_tw_reuse`（自分から張る接続、つまりアウトバウンドに効く設定という説明が一般的。カーネル文書の該当箇所では確認していない。出典未確認）。
  ```conf
  net.ipv4.tcp_tw_reuse = 1     # 既定は 2（ループバックだけ）。1 で全体に効く
  net.ipv4.tcp_fin_timeout = 10 # 既定 60。FIN_WAIT_2 の時間であって TIME_WAIT の長さではない
  ```
  - `tcp_fin_timeout` は「アプリから切り離された接続が FIN_WAIT_2 に留まる秒数」（カーネル文書の定義）。TIME_WAIT の長さを変える設定ではない（TIME_WAIT の長さがカーネル内で固定であることは、この章では出典を確認していない。出典未確認）。名前から効くと思い込まない。
  - `tcp_max_tw_buckets` は上限。超えると TIME_WAIT が即座に破棄され警告が出る。カーネル文書は「人為的に下げず、必要なら増やす」としている。
- **確かめ方**: `ss -s` の TIME_WAIT の数、`nstat -az | grep -i TW`、nginx の error.log、新しい接続の失敗率。
- **危険**: カーネル文書は `tcp_tw_reuse` を「専門家の助言なしに変えるべきでない」としている。通常は変えずに済むよう、まず接続を使い回す。値を変えて点が変わらないなら戻す。
- **出典**: [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html)。実例: [kazeburo](https://kazeburo.hatenablog.com/entry/2014/10/14/170129)（`tcp_max_tw_buckets=2000000`、`tcp_tw_reuse=1`、`tcp_fin_timeout=10`。ベンチの keepalive が無効だったための対処と本人が書いている）。

### conntrack（接続追跡）の溢れ

- **効く条件**: 点が急に落ちる。CPU には余力がある。`dmesg` に `nf_conntrack: table full, dropping packet`。iptables/nftables や docker・ufw でファイアウォールが動いている台。
- **やり方**: 溢れているか確かめ、使っていなければ conntrack を使うルールを外す。使うなら上限を上げる。
  ```sh
  cat /proc/sys/net/netfilter/nf_conntrack_count   # 今の数
  sysctl net.netfilter.nf_conntrack_max            # 上限
  ```
  上限の既定は `nf_conntrack_buckets`（メモリ ÷ 16384 を 1024〜262144 に収めた値）。TCP の established の保持は 5 日（432000 秒）、TIME_WAIT は 120 秒が既定。
- **確かめ方**: `dmesg -T | grep conntrack`、`nf_conntrack_count` が上限に近づくか。
- **危険**: conntrack の無効化（2011 年の実例）は、ファイアウォールの NAT・状態判定に依存するものを壊す。OS のファイアウォールを変えてよいか、台の間の通信の経路を変えてよいかは、マニュアルで確かめる。
- **出典**: [nf_conntrack-sysctl](https://docs.kernel.org/networking/nf_conntrack-sysctl.html)。[fujiwara 初回 ISUCON](https://sfujiwara.hatenablog.com/entry/20110829/1314597283)（ip_conntrack の溢れ。無効化で対処）。

### tcp_slow_start_after_idle（つなぎっぱなしの接続の遅れ）

- **効く条件**: 台の間の往復遅延が大きい（数十 ms 以上）。keepalive でつなぎっぱなしの接続で、間隔をおいて大きな応答（画像など）を送ると毎回遅い。`ss -ti` の `cwnd` が小さいまま。
- **やり方**: `net.ipv4.tcp_slow_start_after_idle = 0`。既定は 1（アイドルの後に輻輳ウィンドウを縮める）。
- **確かめ方**: 大きな応答の時間（nginx の `$upstream_response_time`）。往復遅延が小さい台の間では差が出ない。
- **危険**: 特になし。回線が細い場合は、バーストが損失を増やす可能性がある。

### ソケットバッファ（rmem・wmem・tcp_rmem・tcp_wmem）

- **効く条件**: 往復遅延が大きく帯域も大きい経路（BDP = 帯域 × 遅延が、受信バッファの上限を超える）で、1 接続の速度が頭打ち。例: 100Mbps・100ms の BDP は約 1.25MB。`ss -tim` の `skmem` で `rb`・`tb` が上限に張り付いている。
- **やり方**: 上限（`tcp_rmem`・`tcp_wmem` の第 3 値と `net.core.rmem_max`・`wmem_max`）を BDP 以上にする。既定は `tcp_rmem` の max が 131072〜32MB（RAM で決まる）、`tcp_wmem` の max が 64K〜4MB。`rmem_max`・`wmem_max` は 4194304。
  ```conf
  net.core.rmem_max = 16777216
  net.core.wmem_max = 16777216
  net.ipv4.tcp_rmem = 4096 131072 16777216
  net.ipv4.tcp_wmem = 4096 16384 16777216
  ```
  自動調整が効くので、`SO_RCVBUF`・`SO_SNDBUF` をアプリが指定していれば自動調整は無効になり、上限も無視される。アプリで固定していないか確かめる。
- **確かめ方**: `ss -tim` の `skmem`、1 接続の転送速度（`iperf3` ではなく、アプリの実際の転送）。台の間でしか効かない。
- **危険**: 接続数が多いとメモリを食う（上限 × 接続数）。台のメモリが小さいと OOM の原因になる。

### TCP Fast Open

- **効く条件**: 新しい接続の確立の 1 往復が支配的（往復遅延が大きく、接続を使い回せない）。
- **やり方**: `net.ipv4.tcp_fastopen = 3`（ビット 0x1=クライアント、0x2=サーバー）。サーバー側は待ち受けソケットにも `TCP_FASTOPEN` を付ける必要がある。クライアントが使わなければ効かない。
- **確かめ方**: `nstat | grep -i fastopen` の利用数。設定した値と、実際に使われたかは別。
- **危険**: 効果は小さいことが多い。実例（ISUCON4 参考解答、`tcp_fastopen=2055`）は影響が小さかった。
- **出典**: [ip-sysctl（tcp_fastopen のビット）](https://docs.kernel.org/networking/ip-sysctl.html)。実例: 効果不明。

### tcp_keepalive・tcp_mtu_probing など（触らない候補）

- **効く条件**: 台の間の接続が長時間アイドルで切れる（NAT・ルーターのタイムアウト）。または大きなパケットだけ通らない（MTU の不整合）。
- **やり方**: `tcp_keepalive_time` の既定は 2 時間、`tcp_keepalive_intvl` 75 秒、`tcp_keepalive_probes` 9。keepalive を使うにはアプリが `SO_KEEPALIVE` を付ける必要がある。MTU の問題は 13 章。
- **確かめ方**: 切れるまでの時間とログの時刻。
- **危険**: 競技中の通常の負荷では、効く場面は少ない。
- **出典**: [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html)

## ファイル記述子とプロセスの上限

### ulimit と systemd の LimitNOFILE

- **効く条件**: ログに `Too many open files`（EMFILE）、nginx の `worker_connections are not enough`、DB の `Can't open file`。同時接続が数千になる。
- **やり方**: systemd のサービスには `limits.conf` が効かない。unit の drop-in に書く。
  ```sh
  sudo systemctl edit isu-app.service
  ```
  ```ini
  [Service]
  LimitNOFILE=1006500
  ```
  ```sh
  sudo systemctl daemon-reload && sudo systemctl restart isu-app.service
  cat /proc/$(systemctl show -p MainPID --value isu-app.service)/limits | grep 'open files'
  ```
  - ハード上限の既定は 524288。ソフトの既定は 1024。1024 を超えると `select(2)` を使うプログラムが壊れうる、とマニュアルが注意している。
  - nginx は `worker_rlimit_nofile`（03 章）で自分の上限を上げられる。
  - サービスの上限は、unit に書かなければ `DefaultLimit*`（systemd-system.conf）かカーネルの既定になる（systemd.exec）。ログインシェルの `ulimit -n` や `limits.conf` の値が、systemd のサービスに引き継がれるとは限らない（`limits.conf` の側の仕様は出典未確認）。
- **確かめ方**: `/proc/<pid>/limits` の `Max open files`。`ls /proc/<pid>/fd | wc -l` で今の使用数。
- **危険**: 特になし。DB（MySQL の `open_files_limit`）は DB 側の設定も合わせる。
- **出典**: [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)（LimitNOFILE）。実例: sue445、MySQL の接続上限が増えず LimitNOFILE を設定。

### fs.file-max と nr_open

- **効く条件**: カーネルログに `VFS: file-max limit <n> reached`。
- **やり方**: `fs.file-max` はシステム全体の上限、`fs.nr_open`（既定 1048576）は 1 プロセスが持てる FD の天井。`LimitNOFILE` を `nr_open` より大きくできない。
  ```sh
  cat /proc/sys/fs/file-nr    # 割り当て済み / 0 / 上限
  ```
- **確かめ方**: `file-nr` の第 1 値と第 3 値。
- **危険**: 特になし。まず `LimitNOFILE` で足りる。
- **出典**: [kernel sysctl fs](https://docs.kernel.org/admin-guide/sysctl/fs.html)

### TasksMax と LimitNPROC

- **効く条件**: スレッドやプロセスを大量に作るアプリ（スレッドを 1 リクエスト 1 本にするもの）で `fork: Resource temporarily unavailable`、`pthread_create failed`。
- **やり方**: `LimitNPROC` はユーザー単位でマニュアルが `TasksMax=` を勧めている。unit に `TasksMax=infinity`（または十分大きな数）。既定は `DefaultTasksMax`（systemd-system.conf）。
  ```ini
  [Service]
  TasksMax=infinity
  ```
- **確かめ方**: `systemctl status <unit>` の `Tasks: n (limit: m)`。
- **危険**: 無制限にすると、暴走したときにマシンを巻き込む。
- **出典**: [systemd.resource-control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)、[systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。

## メモリ

### swap と vm.swappiness

- **効く条件**: `vmstat 1` の `si`・`so` が 0 でない。`free -m` で swap が使われ、レイテンシが揺れる。
- **やり方**: まずメモリを食う原因（アプリの worker 数、DB のバッファ、キャッシュ）を減らす。それでも swap が出るなら `vm.swappiness` を下げる（既定 60。0〜200 の範囲）。swap を完全に外す（`swapoff -a` と fstab の編集）のは再起動後も効くように。
  ```conf
  vm.swappiness = 10
  ```
- **確かめ方**: `vmstat 1` の `si/so`、`/proc/meminfo` の `MemAvailable`、`SwapFree`。
- **危険**: swap を無くすと、メモリが尽きたときに OOM killer が動く（下の項）。小さな台（2〜4GB）で DB とアプリが同居していると危ない。マニュアルがメモリ量の変更を禁じていることがある（メモリの増減を伴う操作は失格）。swap の追加はメモリの増加とは別だが、念のためマニュアルで確かめる。
- **出典**: [kernel sysctl vm](https://docs.kernel.org/admin-guide/sysctl/vm.html)（swappiness は「swap と FS の paging の I/O コスト比」で 0〜200、既定 60）。`vm.swappiness=10` は出典未確認の例。

### 書き戻し（vm.dirty_*）

- **効く条件**: 書き込みが断続的に止まる（`iostat` で `w_await` が周期的に跳ねる）。大きなファイルや DB の書き込みの後に I/O 待ち。
- **やり方**: 溜める量（`dirty_background_ratio`・`dirty_ratio`、または `_bytes`）と、期限（`dirty_expire_centisecs`・`dirty_writeback_centisecs`）を調整する。`_ratio` と `_bytes` はどちらか一方。
  ```conf
  vm.dirty_background_ratio = 5
  vm.dirty_ratio = 20
  ```
- **確かめ方**: `/proc/meminfo` の `Dirty`・`Writeback`、`iostat -x 1`。
- **危険**: 溜める量を増やすと、電源断・OS のクラッシュでデータを失いやすくなる。**ベンチ中に書いたデータが再起動後に残ること（永続化）**が条件。通常の `shutdown`/`reboot` ではフラッシュされるが、強制停止の再起動試験があると危ない。DB の `fsync` や `synchronous_commit` を切るのと同じ種類の話で、永続化を弱める調整は避ける。
- **出典**: [kernel sysctl vm](https://docs.kernel.org/admin-guide/sysctl/vm.html)（定義）、[proc（Dirty、Writeback）](https://docs.kernel.org/filesystems/proc.html)。値の例は出典未確認。

### OOM と overcommit、メモリの上限

- **効く条件**: `dmesg` に `Out of memory: Killed process`。DB やアプリが勝手に落ちる。
- **やり方**: まず食っているプロセスを特定する（`ps aux --sort=-rss | head`）。守りたいプロセス（DB）には `OOMScoreAdjust=-500` を unit に（-1000 で OOM 対象外。範囲は -1000〜1000）。暴走しうるアプリには `MemoryHigh=` を付ける（超えると遅くなる。`MemoryMax=` は超えると OOM が動く最後の砦）。
  ```ini
  [Service]
  OOMScoreAdjust=-500
  MemoryHigh=1G
  ```
  `vm.overcommit_memory` の既定は 0（明らかな過大要求だけ拒否）。2 にすると上限が厳しくなる。通常は変えない。
- **確かめ方**: `dmesg -T | grep -i oom`、`cat /proc/<pid>/oom_score`、`systemctl status`。
- **危険**: OOM の対象外にしたプロセスが暴走すると、ほかの必要なプロセスが代わりに殺される。
- **出典**: [proc（oom_score_adj は -1000〜1000）](https://docs.kernel.org/filesystems/proc.html)、[systemd.exec（OOMScoreAdjust）](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)、[systemd.resource-control（MemoryHigh/MemoryMax）](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)、[sysctl vm（overcommit_memory）](https://docs.kernel.org/admin-guide/sysctl/vm.html)。

### tmpfs に置く（DB・ロックファイル・SQLite）

- **効く条件**: ディスクの I/O 待ち（`iowait`）が支配的で、データが小さい。
- **やり方**: `/dev/shm` や tmpfs に置く。起動のたびに内容を作り直す（初期データから復元する）仕組みとセットで。
- **確かめ方**: `iowait` が消えるか。ただし `sys` の CPU とシステムコールが増えて、トータルは変わらないことがある（実例）。
- **危険**: **再起動で消える。追試で「再起動後にベンチ中のデータが残っていること」を満たせず失格になる。** fstab の誤りで OS が起動しなくなった実例もある。永続化が要るデータには使わない。ロックファイルや一時ファイルなど、消えてよいものに限る。
- **出典**: 実例: SQLite を tmpfs。iowait は消えたが syscall が増え不採用、tmpfs の構成が OS 再起動に失敗し復旧できず、flock のファイルを tmpfs に。効果なし。

## CPU

### CPU の governor（周波数制御）

- **効く条件**: ベンチ中の CPU 周波数が低い（`/proc/cpuinfo` の `cpu MHz`、`cpupower frequency-info`）。物理機・一部の VM。
- **やり方**: `/sys/devices/system/cpu/cpufreq/policyX/scaling_governor` を `performance` に。クラウドの VM では cpufreq の sysfs が無いことが多く、その場合は何もできない（出典未確認）。
  ```sh
  cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor
  echo performance | sudo tee /sys/devices/system/cpu/cpufreq/policy*/scaling_governor
  ```
- **確かめ方**: 変更前後の `cpu MHz` とベンチの点。
- **危険**: 再起動で戻る（永続化するなら systemd の unit か tmpfiles.d）。電力は増える。競技環境で効く場面は少ない。
- **出典**: [kernel cpufreq](https://docs.kernel.org/admin-guide/pm/cpufreq.html)。

### irqbalance

- **効く条件**: NIC の割り込みが 1 つの CPU に偏り、その CPU だけ `si`（softirq）が高い（`top` で 1 コアだけ張り付く、`/proc/interrupts` が 1 列だけ増える）。
- **やり方**: `irqbalance` の起動を確かめる（`systemctl status irqbalance`）。コア数が 2 の VM では、効果が出る余地は小さい。
- **確かめ方**: `mpstat -P ALL 1` の `%soft`、`/proc/interrupts`。
- **危険**: 特になし。
- **出典**: [irqbalance](https://github.com/Irqbalance/irqbalance)（割り込みの多い源を 1 つの CPU に割り当てて分散する）。2 コアの VM での効果は出典未確認。

### CPU の取り分と固定（CPUAffinity・AllowedCPUs・Nice・CPUWeight）

- **効く条件**: 1 台に nginx・アプリ・DB が同居していて、片方が CPU を食い切る。nginx の worker とアプリのスレッドが同じコアで奪い合う。`pidstat -t`、`perf sched` で実行待ちが長い。
- **やり方**:
  - 取り分: `CPUWeight=`（1〜10000、既定 100）で相対的に増やす。アプリ 200・DB 100 など。
  - 優先度: `Nice=-5`（-20〜19）。
  - 固定: `CPUAffinity=0 1`（unit）、`AllowedCPUs=`（cgroup v2 のみ）、nginx は `worker_cpu_affinity`。
  ```ini
  [Service]
  CPUWeight=200
  Nice=-5
  ```
- **確かめ方**: コアごとの使用率（`mpstat -P ALL 1`）、`pidstat -w`（context switch）、レイテンシ。
- **危険**: 固定すると空いている他のコアを使えなくなる。台のコア数は環境で違うので、マスクを固定値で書かない。単一スレッド化や固定だけを理由に、排他制御を外さない。
- **出典**: [systemd.resource-control（CPUWeight、AllowedCPUs）](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)、[systemd.exec（Nice、CPUAffinity、CPUSchedulingPolicy）](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)、[nginx worker_cpu_affinity](https://nginx.org/en/docs/ngx_core_module.html#worker_cpu_affinity)。実例: 感想戦。nginx 1 worker とアプリの CPU 固定。個別の得点差なし。

### Transparent Huge Pages（THP）

- **効く条件**: メモリを大量に使う DB・JVM・インメモリのアプリで、レイテンシが周期的に跳ねる。`/proc/meminfo` の `AnonHugePages` が大きい。
- **やり方**: `/sys/kernel/mm/transparent_hugepage/enabled` が `always` なら `madvise` か `never` にして比べる。`defrag` の既定は `madvise`。
  ```sh
  cat /sys/kernel/mm/transparent_hugepage/enabled
  echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
  ```
- **確かめ方**: レイテンシの p99、`AnonHugePages`。変更は新しい割り当てにだけ効くので、プロセスを再起動する。
- **危険**: 再起動で戻る（永続化するなら unit か kernel の起動引数）。文書は、`always` と直接のメモリ整理（compaction）が page fault を止めうること、1 バイトの接触で 2MB が割り当たってメモリが膨らむことを注意している。
- **出典**: [kernel transhuge](https://docs.kernel.org/admin-guide/mm/transhuge.html)。

### NUMA

- **効く条件**: 複数ソケットの物理機・大きなインスタンス（`lscpu` で NUMA node が 2 以上）。
- **やり方**: `numactl --hardware`、`numastat` で偏りを見る。2〜4 vCPU の小さな VM は 1 ノードなので、何もしなくてよい。
- **確かめ方**: `lscpu | grep -i numa`。
- **危険**: 競技環境で効く場面はほぼない。
- **出典**: 出典未確認（`lscpu`、`numactl` の man で確かめる）。

## ディスク

### noatime（アクセス時刻の更新）

- **効く条件**: 小さなファイルを大量に読む（画像・静的ファイルを nginx がファイルから返す）。`iostat` で読み取りしかないのに書き込みが多い。
- **やり方**: 既定は `relatime`（更新があった、または 1 日以上前のときだけ atime を更新）。さらに減らすなら `/etc/fstab` に `noatime`（ディレクトリにも効く）。`lazytime` は時刻の更新をメモリに留める。
  ```fstab
  UUID=...  /  ext4  defaults,noatime  0 1
  ```
  ```sh
  sudo mount -o remount,noatime /
  ```
- **確かめ方**: `mount | grep ' / '`、`iostat -x 1` の書き込み。
- **危険**: fstab の誤りで起動しなくなる。`mount -o remount` で先に試し、fstab は `findmnt --verify` で確かめてから再起動する。`relatime` が既定なので、効果は小さいことが多い。
- **出典**: [mount(8)](https://man7.org/linux/man-pages/man8/mount.8.html)（noatime、relatime、lazytime）。

### I/O スケジューラ

- **効く条件**: NVMe や EBS（クラウドの仮想ディスク）で、スケジューラによる遅延。ディスクがボトルネック（`iostat -x` で `%util` が高い、`await` が大きい）。
- **やり方**: `cat /sys/block/<dev>/queue/scheduler`（`[mq-deadline] kyber bfq none` のように角括弧が今の値）。`echo none | sudo tee /sys/block/<dev>/queue/scheduler` で切り替える。再起動で戻る。
- **確かめ方**: `iostat -x 1` の `await`、`%util`、DB のレイテンシ。
- **危険**: 効果は小さいことが多い。永続化するなら udev の規則か起動引数。
- **出典**: [kernel switching-sched](https://docs.kernel.org/block/switching-sched.html)。

## 不要なサービスとログ

### 不要なサービス・初期実装の残りを止める

- **効く条件**: `top` で、使っていないプロセスが CPU・メモリを食っている。役割を変えた台（DB 専用にした台）に初期のアプリや定期ジョブが残っている。
- **やり方**: 動いているものを一覧し、使っていないものを `systemctl disable --now` する。**止めるだけでなく `disable` まで**（再起動で復活しないように）。
  ```sh
  systemctl list-units --type=service --state=running
  systemctl list-timers
  sudo systemctl disable --now <unit>
  ```
  候補（Ubuntu の標準イメージ）。どれも出典未確認で、台ごとに実際に動いているかを確かめてから止める。
  | 候補 | 止めてよい条件 |
  | --- | --- |
  | `unattended-upgrades`、`apt-daily.timer`、`apt-daily-upgrade.timer` | 競技中は apt の自動更新が要らない。毎日 1 回、`apt update`/`upgrade` を走らせ、CPU・ネットワークを使う（出典: Ubuntu Server ドキュメント）。`Persistent` が既定で、起動直後に走ることがある |
  | `snapd` | snap を使っていない。Ubuntu の標準 AMI には入っている（出典未確認） |
  | `apparmor` | 止める前に、DB（mysqld など）のプロファイルへの影響を確かめる。止めずに済ませるほうが安全（出典未確認） |
  | `multipathd`、`ModemManager`、`lxd` など | クラウドの VM では不要（出典未確認） |
  | 初期実装の別言語のアプリ（`isu-ruby`、`isu-python` など） | 使う言語のサービスだけ有効にする |
  | 役割を変えた台の旧アプリ・定期ジョブ | 必ず止める。DB 専用にした台で旧ジョブが二重配車を起こした実例がある |
- **確かめ方**: `systemctl list-units --state=running`、`top`、再起動後にもう一度一覧を見る。
- **危険**: **追試・運営用のサービス・ユーザー・鍵・エージェント（例: ポータルが使うもの、競技用の guard など）は止めない・消さない。** マニュアルで禁じられている。何のためのサービスか分からないものは、止める前に調べる（`systemctl cat <unit>`）。
- **出典**: [Ubuntu Server: automatic updates](https://ubuntu.com/server/docs/how-to/software/automatic-updates/)（unattended-upgrades と timer）。実例: 採用した言語のサービスだけ有効に、役割を変えた台の旧サービスを止める、複数台で、元の台にだけあった起動設定の移し忘れ。

### journald とログの量

- **効く条件**: アプリが大量にログを出している。`journalctl --disk-usage` が大きい。`top` で `systemd-journald` や `rsyslogd` が CPU を使う。journald が「Suppressed N messages」でログを落とす。
- **やり方**:
  - アプリのログの水準を下げる（`info` 以上に）。これが最も効く。ログを標準出力に出すと journald が受ける。
  - journald: `/etc/systemd/journald.conf` の `Storage=volatile`（メモリだけ）、`SystemMaxUse=200M`、`RateLimitBurst`（既定 30 秒で 10000 件）。`ForwardToSyslog` は既定で無効。
  ```ini
  [Journal]
  Storage=volatile
  RuntimeMaxUse=200M
  ```
  ```sh
  sudo systemctl restart systemd-journald
  ```
  - nginx の `access_log` は 03 章。
- **確かめ方**: `journalctl --disk-usage`、`top` の `systemd-journald`、`pidstat`。
- **危険**: `Storage=volatile` にすると、再起動でログが消える。失格の原因の調査（再起動後のエラー）ができなくなる。競技の終盤は persistent に戻す。**ログを消す・間引くと、失格が出たときに原因を確かめられない**。ログ自体を締め出しの判定に使うサービスを壊さない。
- **出典**: [journald.conf](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)（Storage、SystemMaxUse、RateLimit の既定）。

## systemd の unit の書き方

### 落ちても自動で上がる・起動順（Restart・After・Wants）

- **効く条件**: 再起動試験（再起動後に手作業なしでアプリが動くこと）。アプリが DB より先に起動して落ちる。
- **やり方**:
  ```ini
  [Unit]
  After=network-online.target postgresql.service
  Wants=network-online.target

  [Service]
  Type=exec
  Restart=on-failure
  RestartSec=1s
  LimitNOFILE=1006500
  ExecStart=/home/isucon/webapp/app

  [Install]
  WantedBy=multi-user.target
  ```
  - `Restart=on-failure`（長く動くサービスにマニュアルが勧める）。`RestartSec` の既定は 100ms。`always` は `systemctl stop` では再起動しない。
  - `After=` は順序だけ（起動を待つわけではない）。DB が別の台にあると `After=` では待てないので、アプリ側で**接続のリトライ**を入れる（DB の起動前の要求で落ちた実例がある）。
  - `Type=exec` は実行を始めた時点で成功。ほかの unit が「完全に初期化された」ことを待つ必要があるなら `Type=notify`。
  - `systemctl enable` を忘れない。`enable` していない unit は、再起動で上がらない。
- **確かめ方**: `systemctl is-enabled <unit>`、実際に再起動して `systemctl status`。**台ごとに、1 台ずつ、全台同時の再起動も両方**。
- **危険**: 再起動で動かないと失格。`systemctl start` の手作業が要るなら失敗とみなされる。Unix ソケットを使う場合は、ソケットのディレクトリとファイルを起動時に作り直す（最終の再起動で `.sock` が無く、復旧できず 0 点）。
- **出典**: [systemd.service（Restart、RestartSec、Type）](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)。実例: UDS 化の後の再起動で復旧できず 0 点、DB の起動前の要求で 500、複数台で起動設定の移し漏れ。`After=` と `Wants=` の使い分けの一般論は、systemd.unit の文書を参照（出典未確認）。

### 起動の失敗が連続したとき

- **効く条件**: デプロイで壊れたバイナリを入れたとき、`Restart=` の再起動が回り続けて起動の連続失敗で止まる（`start request repeated too quickly`）。
- **やり方**: `StartLimitIntervalSec=0` で上限を外すか、`RestartSec` を長くする。`RestartSteps=`・`RestartMaxDelaySec=` で指数的に伸ばす。
- **確かめ方**: `journalctl -u <unit> -e`、`systemctl reset-failed <unit>`。
- **危険**: 壊れたまま永遠に再起動し続け、ログと CPU を食う。デプロイの検証は別に持つ。
- **出典**: [systemd.service（RestartSteps、RestartMaxDelaySec）](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)。`StartLimit*` の既定値は systemd.unit の文書に書かれる（この章では確認していない。出典未確認）。

## 時刻と DNS

### 時刻の同期

- **効く条件**: 台の間でログやトークンの時刻がずれる。署名付き URL・セッションの期限・DB のレプリケーションの遅れの計算で、台ごとの時計の差が効く。
- **やり方**: `timedatectl status` で `System clock synchronized: yes` を確かめる。クラウドの標準イメージは `systemd-timesyncd` か `chrony` が動いている（出典未確認）。止めない。
- **確かめ方**: `timedatectl`、台の間で `date +%s.%N` を比べる。
- **危険**: 時刻を手で合わせると、期限つきの値（署名 URL、セッション）が突然切れることがある。
- **出典**: 出典未確認（`timedatectl` の man で確かめる）。

### DNS の名前解決（/etc/hosts・systemd-resolved）

- **効く条件**: 外部 API や別の台を名前で呼んでいて、接続のたびに名前解決が遅い・失敗する。strace や tcpdump で 53 番へのクエリが多い。
- **やり方**: 台の間は IP で呼ぶか、`/etc/hosts` に書く（`systemd-resolved` は `/etc/hosts` を最優先で読んで内部にキャッシュする）。`systemd-resolved` は stub（127.0.0.53）で応答をキャッシュする。アプリの言語側でも（Go・Rust の `getaddrinfo` は同期で、reqwest の既定は別スレッドプール）、HTTP クライアントを使い回して接続ごとの解決を減らす（13 章）。
  ```sh
  resolvectl statistics
  resolvectl flush-caches
  ```
- **確かめ方**: `resolvectl statistics` のキャッシュのヒット、`tcpdump -n port 53`。
- **危険**: `/etc/hosts` に書いたものは、台が変わると古くなる。DNS の TTL を延ばしてもベンチ側の名前解決は変わらない例がある。
- **出典**: [systemd-resolved](https://www.freedesktop.org/software/systemd/man/latest/systemd-resolved.service.html)。reqwest の既定は [ClientBuilder](https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html)（`getaddrinfo` のスレッドプール）。実例: TTL 変更で増点せず。

## よくある失敗

1. **コピペの sysctl を丸ごと入れて、効いたか確かめない。** 1 項目ずつ症状に対応して入れる。
2. **カーネルだけ・アプリだけ backlog を上げる。** `ss -ltn` の `Send-Q` で実際の値を確かめる。
3. **`sysctl -w` だけで終える。** 再起動で消える。`/etc/sysctl.d/` に置く。
4. **`LimitNOFILE` を `limits.conf` に書いて効かない。** systemd のサービスは unit の drop-in。
5. **tmpfs・Unix ソケット・fstab の変更を再起動で試さない。** 再起動後に起動できない・ソケットが無い・データが無い、で失格や 0 点になった実例が複数ある。
6. **OS の設定で点が上がると期待しすぎる。** 土台であり、アプリの改善と組み合わせて効く（kazeburo の振り返り）。まず計測。
7. **エラー文字列だけで OS の上限を疑う。** 外部 API のゲートウェイのエラーを自分の nginx と誤認し、ulimit の調査に時間を使った実例がある。ログの出どころ・接続先・時刻を照合する。
8. **再起動で戻る設定（governor、THP、I/O スケジューラ）を永続化しない・逆に、永続化したつもりで試験しない。** 最終の再起動試験で挙動を確かめる。
9. **追試・運営用のサービスや鍵を止める・消す。** 失格になる。

## 出典一覧

公式文書:

- Linux カーネル: [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html)、[sysctl/net](https://docs.kernel.org/admin-guide/sysctl/net.html)、[sysctl/vm](https://docs.kernel.org/admin-guide/sysctl/vm.html)、[sysctl/fs](https://docs.kernel.org/admin-guide/sysctl/fs.html)、[snmp_counter](https://docs.kernel.org/networking/snmp_counter.html)、[nf_conntrack-sysctl](https://docs.kernel.org/networking/nf_conntrack-sysctl.html)、[transhuge](https://docs.kernel.org/admin-guide/mm/transhuge.html)、[cpufreq](https://docs.kernel.org/admin-guide/pm/cpufreq.html)、[switching-sched](https://docs.kernel.org/block/switching-sched.html)、[proc](https://docs.kernel.org/filesystems/proc.html)
- systemd: [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)、[systemd.resource-control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)、[systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[journald.conf](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)、[systemd-resolved](https://www.freedesktop.org/software/systemd/man/latest/systemd-resolved.service.html)
- man: [ss(8)](https://man7.org/linux/man-pages/man8/ss.8.html)、[mount(8)](https://man7.org/linux/man-pages/man8/mount.8.html)
- nginx: [ngx_core_module](https://nginx.org/en/docs/ngx_core_module.html)、[ngx_http_core_module（listen）](https://nginx.org/en/docs/http/ngx_http_core_module.html)
- その他: [Ubuntu Server: automatic updates](https://ubuntu.com/server/docs/how-to/software/automatic-updates/)、[irqbalance](https://github.com/Irqbalance/irqbalance)、[reqwest ClientBuilder](https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html)

参加記:

- [kazeburo: ISUCON4 予選問題の事後練習](https://kazeburo.hatenablog.com/entry/2014/10/14/170129)
- [fujiwara: 初回 ISUCON](https://sfujiwara.hatenablog.com/entry/20110829/1314597283)
- 書籍『達人が教えるWebパフォーマンスチューニング』（技術評論社）の第 9 章「OSの基礎知識とチューニング」（9-8 Linuxカーネルパラメータ、9-9 MTU など）は目次のみ参照: [gihyo.jp](https://gihyo.jp/book/2022/978-4-297-12846-3)
