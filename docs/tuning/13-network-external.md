# 13 ネットワークと外部 API

当日のマニュアルが優先する。外部 API（決済・配送・認証など）は主催者が用意したものが多く、呼び方・再試行・並列化に仕様と禁止事項がある。**ベンチ以外から主催者のサービスへ余計にアクセスしない**、**外部のリソースへ処理を委譲しない**、**リージョン・通信遅延・帯域制限の変更や回避をしない**は、失格につながりやすい。

## この章で扱う範囲

- アプリから外へ出る HTTP クライアント: keepalive と接続プール、タイムアウト、並列化、再試行、冪等キー。
- 外部 API の待ちを減らす方法と、主催者のサービスへ余計にアクセスしない注意。
- DNS のキャッシュ、MTU、帯域の上限（`tc` などで絞られている場合）。
- 計測・ログの外部送信の注意。

扱わないもの: nginx の upstream の keepalive（03 章）、OS の TCP の設定（02 章）、台の間の通信の設計（11 章）、画像の転送量（12 章）。

## 最初に見るもの（当日の 5 分チェックリスト）

1. **外へ出る通信を洗い出す。** コードの中の HTTP クライアント（`http.Client`・`reqwest`・`curl` の呼び出し）、DB 以外の接続先（外部 API、メトリクス、ログ送信）。`ss -tnp` で、アプリのプロセスが張っている接続先。
2. **走行中の待ち時間を測る。** 外部 API の応答時間・呼び出し回数・失敗数を、**ベンチの負荷走行の最中に**取る（トレース・アクセスログ。01 章）。手動で叩いて速くても、走行中だけ遅延が入る仕様がある。
3. **クライアントを使い回しているか。** リクエストごとにクライアントを作っていないか。接続の再利用率（`ss -tn state established` の接続先ごとの本数が、リクエスト数に対して少ないか）。
4. **タイムアウトが設定されているか。** 無いと、外部 API が詰まったときに、全部のリクエストが待つ。
5. **再試行の仕様。** 外部 API が一時的に失敗する仕様か（成功しているのにエラーを返すなど）、冪等キーがあるか。再試行が二重の副作用（二重決済）を起こさないか。
6. **マニュアルの禁止事項。** 主催者のサービスへ、ベンチ以外から調査目的で呼んでよいか。台の間の通信・外への通信の宛先の制約。
7. **エラーの出どころを確かめる。** 502・接続エラーは、自分の nginx か、外部 API か（外部 Gateway のエラーを自分の nginx と誤認し、ulimit の調査に時間を使った）。

## 外部 API・HTTP クライアント

### 外部 API の待ちを、走行中に計測する

- **効く条件**: 外部 API（決済・配送・認証など）の呼び出しがある問題。
- **やり方**: 負荷走行の最中に、呼び出し開始、アプリ側の並行数制限の通過、接続確立、最初の応答、本文の読み込み完了を、可能な範囲で測る。接続の再利用率、新規接続数、応答コード、再試行数も対応づける。クライアントが公開しない区間を測定済みと扱わず、新規接続率やクライアントの共有の有無による比較で仮説を検証する。
- **確かめ方**: トレースのスパン、アクセスログの外部呼び出しの時間、`ss -tn` の接続先ごとの本数。
- **危険**: **手動のアクセスで速くても、走行中の待ちが短いとは限らない。** ISUCON9 予選では、配送 API はベンチ走行中だけ 0.8 秒の遅延が入る仕様で、手動の計測では目立たず、並列化を見送ったチームがある。計測・試験は、主催者サービスへの許可されたアクセスの範囲で。
- **出典**: 手元の選定手順 、実例: everysick（ISUCON9 予選）、kyokomi（ISUCON9 予選）。

### HTTP クライアントを使い回す（Go と Rust）

- **効く条件**: リクエストごとにクライアントを作っている。新規接続（TCP・TLS の握手）が多い。外部 API の呼び出しの前に接続の時間が見える。
- **やり方**:
  - **Go**: `http.Client`・`http.Transport` は同時に使ってよく、「1 回だけ作って使い回す」のが公式の説明。`Client` の `Transport` が nil なら `DefaultTransport` を使う。**レスポンスの本文は、最後まで読んで閉じる**（読み切らず閉じないと、接続を再利用できないことがある）。`MaxIdleConnsPerHost` の既定は 2（`DefaultMaxIdleConnsPerHost`）。同じ宛先への同時呼び出しが 2 を超えると、超えた分の接続は使い終わると閉じられ、毎回作り直しになる。
    ```go
    var client = &http.Client{
        Timeout: 5 * time.Second
        Transport: &http.Transport{
            MaxIdleConns:        100,                  // 全宛先の合計（0 は無制限）
            MaxIdleConnsPerHost: 100,                  // 宛先ごと（既定 2）
            IdleConnTimeout:     90 * time.Second
        }
    }
    // resp.Body を io.Copy(io.Discard, resp.Body) で読み切って Close する
    ```
    `DefaultTransport` は、`MaxIdleConns` が 100、`IdleConnTimeout` が 90 秒、`ForceAttemptHTTP2` が true。
  - **Rust（reqwest）**: `Client` 自体が接続プールを持つので、1 つを作って使い回す（公式は「`Rc`・`Arc` で包まなくても、内部で `Arc` を使っているので再利用できる」としている。clone がプールを共有するかは文書に明記がない。出典未確認）。`pool_idle_timeout` の既定は 90 秒、`pool_max_idle_per_host` の既定は上限なし。`tcp_nodelay` の既定は true。
    ```rust
    let client = reqwest::Client::builder
        .pool_idle_timeout(std::time::Duration::from_secs(90))
        .timeout(std::time::Duration::from_secs(5))         // 全体の期限
        .connect_timeout(std::time::Duration::from_secs(1)) // 接続の期限
        .build?;
    ```
  - 外部 API が HTTP/2 を話すなら、1 接続で多重化される（Go は `ForceAttemptHTTP2`、reqwest は `http2` の feature）。1 つの接続への同時の要求が多すぎると、`GOAWAY`・同時ストリームの上限に当たることがある（一般論で出典未確認。nginx の `http2_max_concurrent_streams` の既定は 128。 は GOAWAY で悩み、後にベンチ側の不具合と分かった実例）。
- **確かめ方**: 新規接続の数（`ss -tn` の接続先の本数、`ss -s` の TIME_WAIT）、外部呼び出しの接続時間の p50。
- **危険**: 外部 API の許容接続数を超えると失敗が増える。アイドル接続の上限を増やすだけでは、応答待ちが支配的なときには効かない（idle の数は同時実行の上限ではない）。外部 API 側が先にアイドル接続を閉じると、閉じる瞬間の要求が失敗しうる（アイドルタイムアウトを相手より短くする、という対処は 03 章の nginx の upstream と同じ考え方。クライアントでの扱いは出典未確認）。Go の設定値をそのまま Rust に移さない。
- **出典**: [Go net/http（Transport、DefaultMaxIdleConnsPerHost = 2、Client の再利用、本文を読み切って閉じる）](https://pkg.go.dev/net/http#Transport)、[Go の Transport のソース（MaxIdleConnsPerHost などのコメント）](https://raw.githubusercontent.com/golang/go/master/src/net/http/transport.go)、[reqwest ClientBuilder（pool_idle_timeout、pool_max_idle_per_host、timeout、connect_timeout）](https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html)、[reqwest Client](https://docs.rs/reqwest/latest/reqwest/struct.Client.html)。実例: matsuu（ISUCON9 予選。`MaxIdleConnsPerHost` を増やし、その段階で約 19,000 点）。書籍『達人が教えるWebパフォーマンスチューニング』の 8-3「HTTP クライアントの使い方」（目次のみ参照: [gihyo.jp](https://gihyo.jp/book/2022/978-4-297-12846-3)）。

### タイムアウトを決める

- **効く条件**: 外部 API・別の台・DB が詰まったときに、アプリの全部のリクエストが待つ。ベンチのタイムアウト・応答の上限がある。
- **やり方**: 呼び出しごとに期限を持つ。
  - Go: `Client.Timeout`（全体）、`Transport.ResponseHeaderTimeout`（リクエストを書き終えてからヘッダーを受けるまで）。接続の期限は `net.Dialer` の `Timeout`。
  - reqwest: `timeout`（接続から本文の読み終わりまで）、`connect_timeout`、`read_timeout`（読み込みごと）。
  - nginx: `proxy_connect_timeout`・`proxy_read_timeout`・`proxy_send_timeout` の既定は 60 秒（03 章）。
  期限は、ベンチのタイムアウトより短くする。外部 API の通常の応答時間（走行中）の数倍にする。
- **確かめ方**: タイムアウトで失敗した件数、外部呼び出しの p99。
- **危険**: **タイムアウトで待ちを止めても、相手が受理した操作は取り消せたとは限らない。** 成否が不明な操作（決済など）を再試行する前に、結果の照会・冪等性・補償の契約に従う（下の項）。期限を短くしすぎると、遅いが正しい応答を切って失敗が増える。
- **出典**: [Go Transport（ResponseHeaderTimeout など）](https://raw.githubusercontent.com/golang/go/master/src/net/http/transport.go)、[reqwest ClientBuilder](https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html)、[nginx proxy module（timeouts）](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。

### 独立な外部呼び出しを並列にする

- **効く条件**: 1 つのリクエストの中で、独立な外部 API の呼び出しを順番に待っている（応答時間が呼び出しの合計になる）。
- **やり方**: 依存図を書き、独立な部分だけ、上限つきで並行に実行する（Go の goroutine、Rust の `tokio::join!`）。両方の結果と必要な補償を確定してから応答する。
- **確かめ方**: 外部呼び出しを含む API の応答時間（合計から最大に）。
- **危険**: 一方の結果が次の入力・認可・副作用の前提なら、その二つは順序を維持する。片側が失敗したときの取消、多重実行を保つ。**同じ成功操作を投機的に複数同時に送らない**（冪等キーを付けても、成功後に届いた重複で critical になった実例がある。下の項）。
- **出典**: 実例: p1ass・tomoyat1（ISUCON9 予選。postBuy の独立した外部呼び出しを並行化）。

### 再試行・バックオフ・冪等キー

- **効く条件**: 外部 API が一時的に失敗する（429・5xx・タイムアウト。または、成功しているのにエラーを返す仕様）。
- **やり方**: 許される一時的な失敗だけ、期限・回数つきで再試行する。間隔は指数的に伸ばす（バックオフ）。冪等キー（`Idempotency-Key` など）が使える API では、同じ操作の再試行に**同じキー**を付ける（キーを変えると別の操作になる）。
  - ISUCON14 の決済 API: 実際には成功していてもエラーを返すことが増える。初期実装は、再試行のたびに時間のかかる決済確認 API を叩いていた。想定の解は、`Idempotency-Key` に RideID などを指定して、確認 API を経由せずに再要求すること（作問側は、この要素を「ブラフ」だったと認め、これに囚われたチームは少なかった）。
  - 入力の誤り（400）を一律に再試行しない。対象のエラーの意味と操作の冪等性を確かめる。
- **確かめ方**: 再試行の回数、最終的な成功率、外部への要求の総数。
- **危険**: **再試行・並列化が二重の副作用（二重決済）を起こすと、critical になる。** 実例: 同じ決済を複数同時に送る案で、成功後に届いた重複の要求が critical（`CODE=35`）になった。冪等キーは、投機的な並列送信の許可とは限らない。主催者のサービスへの調査目的の余計なアクセスもしない。上限を増やして失敗が増えるなら逆効果。外部 API から仕様書にない一時的な 400 が返り、再試行を入れた実例があるが、原因と再試行単独の効果は未確定。
- **出典**: [ISUCON14 問題の解説と講評](https://isucon.net/archives/58869617.html)、実例: kotapjp（ISUCON14。指数バックオフ）、ponyo877。rideID を `Idempotency-Key` に、kawaemon。同じ決済を並列送信して失敗、takonomura（ISUCON9 予選）。

### 外部の状態参照を減らす

- **効く条件**: 外部 API の状態（配送状態など）を、一覧・詳細のたびに取りに行っている。
- **やり方**: 更新の所有者と鮮度の仕様を確かめ、許される場合だけ、ローカルの表に保存した値から返す。ISUCON9 予選の実例は、取引一覧で必要な配送状態をローカルの `shippings.status` から返し、外部へのリクエストを省いた。
- **確かめ方**: 外部 API の呼び出し数の減少。ローカルの状態が、いつ・どの契機で更新されるか。
- **危険**: 外部だけで状態が変わる、照合自体が必須の仕様なら省かない。外部 API を呼ぶこと自体が要件かも確かめる。通知の取りこぼし。キャッシュした古い値は、書き込み後の読みの整合性（read-after-write）の仕様を確かめる。外部の URL をキャッシュしたら、initialize でキャッシュも消す（決済先 URL を保存して再利用し、2 回目のベンチで失敗。初期化でキャッシュを消して直した）。
- **出典**: 実例: takonomura（ISUCON9 予選）、Iceman（ISUCON14）。

### 主催者のサービスへ余計にアクセスしない

- **効く条件**: 外部 API の仕様を調べたい、速度を測りたい、動作を確かめたいとき。
- **やり方**: 外部 API（決済など）へのアクセスは、**ベンチの負荷走行の中で、アプリが必要なときだけ**。調査のために、手で・スクリプトで繰り返し叩かない。仕様はマニュアルとコードを読んで確かめ、計測はアプリのログ・トレースで（アプリが通常に呼んだ分を測る）。
- **確かめ方**: マニュアルの禁止事項。外部 API の呼び出しのログ。
- **出典**: 実例: 適用前の確認に「主催者サービスを調査目的で余計に叩かない」、（競技ルールで絶対に踏まないもの）。マニュアルの該当箇所は問題ごとに確認する。

### 外部への処理の委譲と、計測・ログの送信

- **効く条件**: 計測（OTel・Grafana）・ログを外部のサービスへ送りたい。送信が遅く、アプリの待ちや CPU になっている。
- **やり方**: 計測・モニタリングに外部のリソース（PC・外部サーバー）を使うのは、許される問題が多い（マニュアルで確かめる）。ログの外部送信は、即時ではなく、一定時間かバイト数までバッファして一括で送る（ISUCON8 本選の実例: 5 秒または未送信 1MB で送る処理に変えて、0 点から回復した）。送信の失敗は、アプリのリクエストを止めない。終了時に flush する。
- **確かめ方**: 外部送信の待ち時間・CPU・送信失敗の件数。
- **危険**: **外部のリソースへ処理を委譲して、スコアを上げる効果を持たせるのは禁止（複数環境の利用を含む）。** 計測のトレースを 100% 採ると、それだけで CPU を約 1 割使った実例がある（採取率を絞る）。収集のサーバーが量で落ちた実例もある。平文で流れる中身にも注意する。
- **出典**: 実例: nomeaning（ISUCON8 本選）、fukkatsuso（ISUCON14）、yamachu（ISUCON14。手元へ閉じた経路でプロファイルを送る）。

## DNS

### DNS のキャッシュと名前解決

- **効く条件**: 外部 API や別の台を名前で呼んでいて、接続のたびに名前解決している（`tcpdump -n port 53` で大量のクエリ）。名前解決の失敗・遅延。DNS そのものがアプリの一部の問題（登録のたびに DNS を更新し、名前解決の成功数が評価される。ISUCON13。[公式の結果発表](https://isucon.net/archives/57993937.html)にも名前解決成功数の賞がある）。
- **やり方**:
  - クライアントを使い回して、接続ごとの名前解決を減らす（上の項）。
  - 台の間は IP で呼ぶか、`/etc/hosts` に書く。`systemd-resolved` は `/etc/hosts` を最優先で読み、stub（127.0.0.53）で応答をキャッシュする。`resolvectl flush-caches` でキャッシュを消せる。
  - reqwest の既定の名前解決は `getaddrinfo` のスレッドプール。`hickory-dns` の feature で、非同期のリゾルバに替えられる。`resolve`・`resolve_to_addrs` で特定の名前の解決先を固定できる。
  - DNS サーバーそのものが問題なら（ISUCON13）、DNS を別の台へ分離する、拒否・破棄の方式を見直す。
- **確かめ方**: `resolvectl statistics`、`tcpdump -n port 53`、名前解決の時間。
- **危険**: `/etc/hosts` に書いた値は、台が変わると古くなる。**DNS の TTL を延ばしても、ベンチ側のキャッシュには効かず、増点しなかった実例がある**（A レコードを 0 秒から 3600 秒へ変更。整合性検査は通ったが点が変わらず不採用）。動的に名前を登録する問題（DNS の更新が評価される）では、TTL・ゾーンの更新方式が整合性に効く（ゾーンファイルへの移行で登録が直列化した。034: 全サブドメインを解決して HTTP で拒否する案、許可リスト方式は、ベンチの検査に通らない・動的登録に対応できないで失敗・断念）。
- **出典**: [systemd-resolved](https://www.freedesktop.org/software/systemd/man/latest/systemd-resolved.service.html)、[reqwest ClientBuilder（getaddrinfo のスレッドプール、hickory_dns、resolve）](https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html)、[sorah ISUCON13](https://diary.sorah.jp/2023/11/28/isucon13)（PowerDNS から nsd への移行が登録を遅くした。dnsdist で応答せず破棄するのが効いた）。dnsmasq のラウンドロビン。

## MTU

### MTU の不整合（台をまたぐ接続が失敗する）

- **効く条件**: 台をまたぐ接続（DB・内部 API）が、小さな通信は通るのに、大きな通信だけ失敗・停止する。DB の認証で `Bad handshake` が出る。`ss -ti` で再送が多い。
- **やり方**: 経路の MTU を測る。`ping -M do -s N <相手>`（DF ビットを立てて断片化させない）で、通る最大の N を探す。経路の MTU ＝ N + 28（IPv4 の ICMP）。インターフェースの MTU（`ip link show`）と比べる。
  ```sh
  ping -M do -s 1472 -c 3 <相手>   # 通れば MTU 1500 以上
  ping -M do -s 1473 -c 3 <相手>   # 通らなければ経路の MTU は 1500
  ip link show eth0                 # mtu の値
  ```
  不整合なら、MTU を揃える。経路の途中で ICMP が落ちて PMTUD が働かないなら、`net.ipv4.tcp_mtu_probing = 1`（ICMP のブラックホールを検出したら有効。2 は常に有効）が使える。
- **確かめ方**: 大きなパケットが通る。DB の認証が通る。再送の数（`nstat | grep -i retrans`）。
- **危険**: **Jumbo Frame（MTU 9000）を、経路全体で使える前提で設定しない。** 実例（ISUCON10 予選）: MTU を 9000 にしたら台をまたぐ MySQL に `Bad handshake` が出て、1500 に戻すと直った（PMTUD や MSS が働かなかったのかという機序は筆者の推測）。1500 が全環境の最適値という意味ではない。MTU はインターフェースの設定で、再起動で戻る場合がある（永続化の方法を確かめる）。
- **出典**: [ping(8)（-M do、-s）](https://man7.org/linux/man-pages/man8/ping.8.html)、[kernel ip-sysctl（tcp_mtu_probing）](https://docs.kernel.org/networking/ip-sysctl.html)。実例: nonylene（ISUCON10 予選）。書籍『達人が教えるWebパフォーマンスチューニング』の 9-9「MTU（Maximum Transmission Unit）」は目次のみ参照: [gihyo.jp](https://gihyo.jp/book/2022/978-4-297-12846-3)。PMTUD の仕組みそのものの出典は未確認。

## 帯域の上限

### 帯域・遅延が絞られている環境で

- **効く条件**: 台の間・外向きの通信が、運営の設定（`tc` など）で、帯域・遅延が制限されている。CPU・DB に余力があるのに、応答が遅い。`sar -n DEV` が上限に近い。
- **やり方**: **制限そのものを変更・回避せず**、制限の中で通信量・往復を減らす。
  - 転送量を減らす: 304・キャッシュ（12 章）、gzip・brotli（テキスト。03 章）、画像を台に持つ（11 章）、不要な通信の削除。
  - 往復を減らす: クエリ・API 呼び出しをまとめる、keepalive、読みを手元で済ませる（11 章）。
  - 通信を分散する: 複数の入口（11 章。ベンチが複数の台を叩ける場合）。
  - 見積もり: 12 章の「帯域の見積もり」。
- **確かめ方**: `sar -n DEV 1`、`ss -ti`、帯域の測定（`curl` で大きなファイルを取る）。制限があるか確かめるだけなら、読み取りの調査（`tc qdisc show`）。
- **出典**: 2.1 節、実例: 1Gbps の制約下で往復を削っても効かず。

### tc で遅延・帯域を再現する（手元の実験用）

- **効く条件**: 手元の環境で、遅延・帯域の制限を再現して、アプリの挙動（往復の回数 × 遅延、帯域の頭打ち）を確かめたい。
- **やり方**: Linux の `tc` の `netem`（遅延・損失・レート）と `tbf`（帯域の上限）。
  ```sh
  tc qdisc add dev eth0 root netem delay 100ms        # 片道 100ms 遅らせる（往復 200ms になる経路もある）
  tc qdisc change dev eth0 root netem delay 100ms 20ms distribution normal
  tc qdisc add dev eth0 root netem loss 0.1%
  tc qdisc add dev eth0 handle 10: root tbf rate 100mbit burst 32kbit latency 70ms   # 帯域の上限
  tc qdisc del dev eth0 root                          # 元に戻す
  ```
  `netem` の `delay` は、そのインターフェースから出る通信に足される（man の説明は「基本の遅延を設定する」）。往復の遅延を作るには、両側に設定するか、片側に往復分を設定する（一般論で出典未確認）。`tbf` の `rate`・`burst`・`latency` の値は例で、man の例とは違う。
- **確かめ方**: `ping` の RTT が設定どおりか。`tc qdisc show dev eth0`。
- **危険**: **競技サーバーに対しては行わない（遅延・帯域制限の変更に当たる）。** 手元・自分たちの実験環境（`tools/lab-aws` など）だけ。`root` の qdisc を入れ替えると、ほかの設定を消すことがある。SSH の通信にも遅延が入る。
- **出典**: [tc-netem(8)](https://man7.org/linux/man-pages/man8/tc-netem.8.html)、[tc-tbf(8)](https://man7.org/linux/man-pages/man8/tc-tbf.8.html)。

## TCP と台の間の通信（他の章への案内）

- 待ち行列・ポート・TIME_WAIT・ソケットバッファ・`tcp_slow_start_after_idle`: 02 章。
- nginx から upstream への接続の再利用: 03 章。
- 台の間の通信の宛先（プライベート IP、ルーターの IP を宛先にしない）、台の間の keepalive、往復の見積もり: 11 章。
- 画像の転送量・304・圧縮: 12 章。

## よくある失敗

1. **手動で外部 API を叩いて「速い」と判断する。** 走行中だけ遅延が入ることがある。
2. **リクエストごとにクライアントを作る。** 接続・TLS の握手を毎回払う。Go では `MaxIdleConnsPerHost` の既定が 2 で、同時呼び出しが多いと再利用されない。
3. **レスポンスの本文を読み切らず閉じない（Go）。** 接続を再利用できない。
4. **タイムアウトを決めない。** 外部 API が詰まると、全部が待つ。
5. **成否不明の操作を再試行・並列送信する。** 二重決済で critical。冪等キーは並列送信の許可ではない。
6. **主催者のサービスを調査目的で叩く。** 失格につながりうる。
7. **DNS の TTL を延ばして効くと期待する。** ベンチ側のキャッシュには効かなかった。
8. **MTU を 9000 にする。** 台をまたぐ DB の認証が失敗した。
9. **外向き帯域がボトルネックなのに、往復や CPU の手を打つ。** 総転送量を先に見積もる。
10. **帯域・遅延の制限を変える・回避する。** 禁止。
11. **エラーの出どころを確かめずに OS の上限を疑う。** 外部 API のエラーを自分の nginx と取り違えた。
12. **ログ・計測を即時に外部へ送り、アプリを止める。** バッファして一括で送る。

## 出典一覧

公式文書:

- Go: [net/http](https://pkg.go.dev/net/http#Transport)、[net/http/transport.go のコメント](https://raw.githubusercontent.com/golang/go/master/src/net/http/transport.go)
- Rust: [reqwest ClientBuilder](https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html)、[reqwest Client](https://docs.rs/reqwest/latest/reqwest/struct.Client.html)
- Linux: [ping(8)](https://man7.org/linux/man-pages/man8/ping.8.html)、[tc-netem(8)](https://man7.org/linux/man-pages/man8/tc-netem.8.html)、[tc-tbf(8)](https://man7.org/linux/man-pages/man8/tc-tbf.8.html)、[ip-sysctl（tcp_mtu_probing）](https://docs.kernel.org/networking/ip-sysctl.html)
- systemd: [systemd-resolved](https://www.freedesktop.org/software/systemd/man/latest/systemd-resolved.service.html)
- nginx: [proxy module（timeouts）](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)

参加記・解説:

- [ISUCON14 問題の解説と講評（isucon.net）](https://isucon.net/archives/58869617.html)
- [sorah: ISUCON13 本番](https://diary.sorah.jp/2023/11/28/isucon13)

手元の資料:

- 書籍『達人が教えるWebパフォーマンスチューニング』の 8-3（HTTP クライアントの使い方）、9-9（MTU）は目次のみ参照: [gihyo.jp](https://gihyo.jp/book/2022/978-4-297-12846-3)
