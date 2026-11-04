# 03 nginx

当日のマニュアルが優先する。nginx の設定のうち、静的ファイルの内容の改変、ベンチの識別（User-Agent などでベンチを見分けて特別扱いする）、ルーターの迂回、外部への処理の委譲は、失格につながりやすい。

## この章で扱う範囲

- ワーカーと接続: `worker_*`、`events`、keepalive（クライアント側と upstream 側）、Unix ソケット。
- ファイル配信: `sendfile`、`open_file_cache`、静的ファイルの直接配信、画像、圧縮、キャッシュヘッダ、`X-Accel-Redirect`。
- バッファとキャッシュ: `proxy_buffering`、ボディのバッファ、`proxy_cache`、`fastcgi_cache`。
- プロトコル: HTTP/2、HTTP/3、TLS。
- ログ、`limit_req`、upstream の振り分け、OpenResty。

扱わないもの: OS の設定（02 章）、複数台の配置（11 章）、画像の保存方法と帯域の見積もり（12 章）、アプリから外への HTTP クライアント（13 章）。

## 最初に見るもの（当日の 5 分チェックリスト）

1. **今の設定と版を読む。** `nginx -V`（版と有効なモジュール。`gzip_static`・`http_v2`・`http_dav` などは既定では入らないことがある）、`sudo nginx -T`（有効な設定を全部出す。include の先まで読める）。
2. **アクセスログに応答時間を足す。** 計測の道具（alp など）は 01 章。最低限、`$request_time`、`$upstream_response_time`、`$upstream_connect_time`、`$upstream_cache_status`、`$status`、`$body_bytes_sent` を出す。
3. **error.log を見る。** 次の警告は、設定で直せる症状を指す。
   | error.log の文言 | 指していること | 項目 |
   | --- | --- | --- |
   | `worker_connections are not enough` | 接続の上限 | worker の項 |
   | `Too many open files` | FD の上限 | worker の項、02 章 |
   | `a client request body is buffered to a temporary file` | ボディがバッファに入りきらない | ボディのバッファの項 |
   | `an upstream response is buffered to a temporary file` | 応答がバッファに入りきらない | proxy_buffering の項 |
   | `upstream prematurely closed connection`、`connect failed (111/110)` | アプリが落ちている・遅い・keepalive の切れ違い | upstream keepalive の項 |
   | `Cannot assign requested address` | ローカルポートの枯渇 | upstream keepalive の項、02 章 |
4. **ステータスを数える。** 499（クライアントが先に切った。応答が遅い）、502（upstream に繋がらない・途中で切れた）、504（upstream のタイムアウト）、413（ボディが大きすぎる）。
5. **upstream に keepalive があるか** と **静的ファイルをアプリが返していないか** を見る。この 2 つは、効く場面が多く、危険が小さい。

効果の大きさは問題で違う。「nginx を入れ替えただけでスコアが一気に落ちた」実例も、「keepalive を有効にしたら全体が約 67,000 点から 30,000 点に落ちた」実例もある。1 つずつ変えて、ベンチで比べる。

## ワーカーと接続

### worker_processes・worker_connections・worker_rlimit_nofile

- **効く条件**: error.log に `worker_connections are not enough` か `Too many open files`。同時接続が数千以上。nginx のワーカーが 1 本の CPU に張り付く（`top` で nginx の 1 プロセスが 100%）。
- **やり方**:
  ```nginx
  worker_processes auto;          # コア数。迷ったらコア数（公式の説明）。既定は 1
  worker_rlimit_nofile 65536;     # FD の上限を nginx 自身で上げる。既定は未設定
  events {
    worker_connections 8192;      # 1 ワーカーあたり。既定は 512
  }
  ```
  - `worker_connections` には、upstream（アプリ）への接続も数える。クライアントとアプリの 2 本で 1 リクエストを処理する。実効の上限は `worker_rlimit_nofile`（open files の上限）を超えない。
  - systemd 経由の nginx で `worker_rlimit_nofile` が効かないなら、unit の `LimitNOFILE` も見る（02 章）。
- **確かめ方**: `cat /proc/$(pgrep -f 'nginx: worker' | head -1)/limits | grep 'open files'`、error.log、`ss -s`。
- **危険**: 特になし。CPU が 2 コアの台で `worker_processes` を 2 にすると、アプリと CPU を奪い合う。台の役割（11 章）を考えて決める。`worker_cpu_affinity` で固定するなら、台のコア数を確かめる（は感想戦で 1 worker に固定。効果は未分離）。

### use epoll・multi_accept・accept_mutex

- **効く条件**: 特になし（通常は変えなくてよい）。
- **やり方**: `use epoll;` は Linux では nginx が自動で選ぶので、書かなくてよい。`multi_accept`（既定 off）は、1 回の通知で全部の新しい接続を受ける。`accept_mutex` の既定は off（1.11.3 以降）。
  ```nginx
  events {
    use epoll;           # 省略可
    # multi_accept on;   # 効果は計測で比べる。既定は off
  }
  ```
- **確かめ方**: ベンチの点と `ss -ltn` の `Recv-Q`。
- **危険**: 効果は小さく、実例の根拠も見つかっていない。入れるなら単独で比べる。
- **出典**: [nginx events](https://nginx.org/en/docs/events.html)（`use epoll`）。`multi_accept`・`accept_mutex` の既定値は一般的な nginx の知識として答えが返っただけで、events の頁には書かれていなかった。出典未確認。

## keepalive と upstream

### クライアント側の keepalive（keepalive_timeout・keepalive_requests）

- **効く条件**: ベンチのクライアントが keepalive を使う場合に、毎回の接続が `ss -s` の TIME_WAIT を増やす。接続の確立（TLS なら握手）が CPU を食う。逆に、アイドル接続が滞留して詰まる場合もある。
- **やり方**: 既定は `keepalive_timeout 75s;`、`keepalive_requests 1000;`（1.19.10 以降。それ以前は 100）、`keepalive_time 1h;`。
  ```nginx
  keepalive_timeout  65;
  keepalive_requests 1000000;   # 負荷が大きく、1 接続で何万回も使われるなら上げる
  ```
  - 負荷でアイドル接続が増えて詰まるなら、短くする（実例）。
  - HTTP/2 では `keepalive_timeout`・`keepalive_requests` が接続の寿命を決める（`http2_idle_timeout`・`http2_max_requests` は 1.19.7 で廃止予定）。
- **確かめ方**: `ss -s`、`ss -tan state established | wc -l`、ベンチの点。
- **危険**: 1 つの変更で逆効果になる実例がある。初期実装が keepalive を切っていた nginx で、複数台化の後に keepalive を有効にしたら点が約 67,000 → 30,000 に落ち、無効に戻して直った（原因は未記載）。2011 年の実例では、`keepalive_timeout` を 65 秒から 5 秒にして点が上がり、最後は keepalive を切った。**必ず前後を比べ、悪くなったら戻す。**
- **出典**: [nginx core（keepalive_timeout、keepalive_requests、keepalive_time）](https://nginx.org/en/docs/http/ngx_http_core_module.html)、[v2 module](https://nginx.org/en/docs/http/ngx_http_v2_module.html)、[fujiwara 初回 ISUCON](https://sfujiwara.hatenablog.com/entry/20110829/1314597283)（keepalive_timeout を 5 秒に、最終的に off）。TLS で keepalive の寿命と上限を調整。

### upstream の keepalive（proxy_http_version 1.1 と Connection ""）

- **効く条件**: nginx からアプリへの接続を毎回作っている。`ss -s` の TIME_WAIT が多い。error.log に `Cannot assign requested address`。アクセスログの `$upstream_connect_time` が大きい。アプリ側のアクセスで接続数が多い。
- **やり方**:
  ```nginx
  upstream app {
    server 127.0.0.1:8080;
    keepalive 64;                   # 1 ワーカーが持つ「アイドル」の接続の上限
    keepalive_requests 1000000;
  }
  server {
    location / {
      proxy_http_version 1.1;       # 1.29.7 より前の既定は 1.0
      proxy_set_header Connection "";
      proxy_set_header Host $host;
      proxy_pass http://app;
    }
  }
  ```
  - 版による違い: 公式の最新の文書では、1.29.7 から `proxy_http_version` の既定が 1.1、upstream の keepalive も既定で有効（`keepalive 32 local`）。それより前（Ubuntu の標準パッケージの版は出典未確認）では、`proxy_http_version 1.1;` と `proxy_set_header Connection "";` を自分で書く。書いておけば版に依存しない。
  - `keepalive N` は、アイドル接続の上限であって、接続の総数の上限ではない（公式の説明）。上流がその分の接続を新しく受けられる数にする。
  - `upstream` ブロックの中では、`least_conn` など round-robin 以外の振り分けは `keepalive` より前に書く（1.29.7 より前）。
  - `location` では `proxy_pass http://app;`（upstream 名）と書く。`proxy_pass http://127.0.0.1:8080;` のように直接書くと upstream ブロックの `keepalive` が使われない、と一般に言われる（出典未確認。`$upstream_connect_time` で確かめる）。
- **確かめ方**: `ss -tn state established '( dport = :8080 )' | wc -l` が、ベンチ中も一定数で収まる。TIME_WAIT が減る。`$upstream_connect_time` が 0 に近い。アプリのログの接続数。

### upstream を Unix ソケットにする

- **効く条件**: nginx とアプリが同じ台で、ループバックの TCP のコストが見える（CPU の `sys`、syscall）。ほかの手（keepalive、ワーカー数）を済ませた後。
- **やり方**: アプリが Unix ソケットで待ち受け、nginx は `server unix:/run/app/app.sock;` と書く。
  ```nginx
  upstream app {
    server unix:/run/isu/app.sock;
    keepalive 64;
  }
  ```
  ソケットのファイルのディレクトリ、所有者、権限（nginx のワーカーのユーザーが書き込めること）、アプリの起動時にソケットを作り直す（古いファイルを消してから bind）ことを確かめる（これらは出典未確認の一般論。実例: の「適用前の確認」に同じ指摘がある）。
- **確かめ方**: ベンチの点、`sys` の CPU。再起動して `ls -l /run/isu/` にソケットがあり、疎通できること。
- **危険**: **再起動で壊れた実例がある。** UDS 化した後の最終の再起動でソケットのファイルが無く、デーモンも上がらず、時間切れで 0 点だった。`/run` は tmpfs で再起動のたびに空になる（出典未確認）ので、ソケットのディレクトリを unit の `RuntimeDirectory=` などで毎回作る。複数台にまたがる接続には使えない。効果は小さいことが多い。TCP のままで keepalive があれば先に十分。
- **出典**: [nginx upstream（server unix:）](https://nginx.org/en/docs/http/ngx_http_upstream_module.html)。実例: 再起動で失敗し 0 点、作問者の模範解答は UDS。kazeburo は `/dev/shm/app.sock`（[ISUCON4 事後練習](https://kazeburo.hatenablog.com/entry/2014/10/14/170129)）。

## ファイル配信

### sendfile・tcp_nopush・tcp_nodelay

- **効く条件**: nginx が静的ファイル（画像・CSS・JS）を配信する。
- **やり方**:
  ```nginx
  sendfile on;      # 既定 off
  tcp_nopush on;    # sendfile が on のときだけ効く（Linux では TCP_CORK）
  tcp_nodelay on;   # 既定 on
  ```
  - `tcp_nopush` は、ヘッダとファイルの先頭を 1 つのパケットで送り、ファイルを満杯のパケットで送る。
  - `sendfile_max_chunk`（既定 2m）は、1 本の速い接続がワーカーを占有しないための上限。
- **確かめ方**: 静的配信の応答時間と、ワーカーの CPU の `sys`。
- **危険**: 特になし。個別の寄与は実例でも未分離。
- **出典**: [nginx core module（sendfile、tcp_nopush、tcp_nodelay）](https://nginx.org/en/docs/http/ngx_http_core_module.html)。実例: 個別の数字なし。

### open_file_cache

- **効く条件**: nginx が静的ファイルを大量に返し、strace で `open`・`fstat` が多い。
- **やり方**:
  ```nginx
  open_file_cache          max=10000 inactive=60s;
  open_file_cache_valid    60s;
  open_file_cache_min_uses 1;
  open_file_cache_errors   off;
  ```
  保持するのは FD・サイズ・更新時刻・ディレクトリの有無（応答の本体はキャッシュしない）。`max` を超えると、最近使われていないものから外す。
- **確かめ方**: `strace -c -p <worker pid>` の `open`・`fstat`、配信の応答時間。
- **危険**: **ファイルを置き換えた（同じ名前で上書き・差し替えた）直後に、古い内容や古いサイズを返す**ことがある（`open_file_cache_valid` の間）。画像をアップロードで差し替える仕様や、initialize で画像を置き直す仕様では、反映の速さを確かめる。FD を `max` 本使うので、`worker_rlimit_nofile` も合わせる。単独の増点は実例でも不明。
- **出典**: [nginx core module（open_file_cache とその関連）](https://nginx.org/en/docs/http/ngx_http_core_module.html)。実例: kazeburo（ISUCON4 の事後練習で `open_file_cache max=100`。複数設定の合計）。

### 静的ファイルを nginx から直接返す

- **効く条件**: アクセスログで、`/css/`・`/js/`・`/images/`・`/favicon.ico` などの静的ファイルがアプリに届いている（`$upstream_response_time` がある）。アプリの CPU がファイル読み出しに使われている。
- **やり方**: `root` か `alias` と `try_files` を使う。`location` の一致の順序（完全一致 `=`、`^~`、正規表現、最長の接頭辞）を理解してから書く。
  ```nginx
  location /css/ {
    root /home/isucon/webapp/public;     # URI をそのまま足す（/css/a.css → .../public/css/a.css）
    expires 1d;
  }
  location /images/ {
    alias /home/isucon/webapp/images/;   # location 部分を置き換える
  }
  location / {
    try_files $uri @app;                 # ファイルがあれば返し、なければアプリへ
  }
  location @app {
    proxy_pass http://app;
  }
  ```
  - `alias` は location の接頭辞部分を置き換える。location と alias の末尾のスラッシュを揃える（揃っていないと、組み立てた先のパスがずれる。出典未確認の一般論）。公式は、最後の部分が一致する接頭辞の location には `root` を勧めている。
  - 正規表現の location で `alias` を使うなら、完全なパスを書く（`$1` を使う）。
- **確かめ方**: アクセスログに `$upstream_response_time` が出ない（アプリに届いていない）こと。応答の `Content-Type`・`Content-Length`・本文のハッシュが元と同じこと（`curl -s URL | sha256sum` を、初期実装の応答と比べる）。
- **危険**: **配布された静的ファイルの内容を変えない（変えると失格）。** nginx で返すだけなら中身は変わらないが、`gzip_static` や画像の再圧縮は「内容の改変」と判断されうる（マニュアルで確かめる）。**移行後に画面が崩れて戻した実例が複数ある**（MIME・パス・相対 URL・圧縮ヘッダの食い違いが候補。原因は記事に未記載）。動的に生成していた JS をダミー内容に替えて、応答検査に落ちた実例もある。
- **出典**: [nginx core（root、alias、try_files、location）](https://nginx.org/en/docs/http/ngx_http_core_module.html)、書籍『達人が教えるWebパフォーマンスチューニング』の 8-4「静的ファイル配信をリバースプロキシから直接配信する」（目次のみ参照: [gihyo.jp](https://gihyo.jp/book/2022/978-4-297-12846-3)）。失敗、Envoy で静的コンテンツだけ nginx へ。

### DB に入った画像をファイルに書き出して返す（概要）

- **効く条件**: 画像の本体を DB の BLOB（bytea）から読んでアプリが返していて、DB の I/O・アプリの CPU・接続が画像で埋まる。
- **やり方**: 初期データの画像を、initialize か初回アクセスでファイルに書き出す。アップロード時にもファイルに書く。nginx は `try_files $uri @app;` でファイルがあれば直接返し、なければアプリが DB から返す。認可が要る画像は `X-Accel-Redirect`（下の項）。詳細は 12 章。
- **確かめ方**: 12 章を参照。
- **危険**: 書き込み直後の読みで、ファイルがまだ無いとき（`@app` へのフォールバック）の挙動、initialize でファイルも初期状態に戻すこと、削除した画像を返し続けないこと。詳細は 12 章。
- **出典**: [kujira16 ISUCON11 予選](https://kujira16.hateblo.jp/entry/2021/08/22/231020)（画像を nginx から配信）。

### 圧縮（gzip）

- **効く条件**: 大きな JSON・HTML・CSS・JS の応答があり、帯域か転送時間がボトルネック（応答の `$body_bytes_sent` が大きい、リージョン間の帯域が細い）。CPU には余力がある。
- **やり方**:
  ```nginx
  gzip on;
  gzip_comp_level 1;           # 既定 1。上げても CPU が増えるだけのことが多い
  gzip_min_length 1000;        # 既定 20（Content-Length だけで判断）
  gzip_types application/json text/css application/javascript;   # 既定は text/html のみ
  gzip_proxied any;            # プロキシした応答も圧縮する（既定 off）
  gzip_vary on;
  ```
  - JPEG・PNG など、圧縮済みの画像は `gzip_types` に入れない（PNG をさらに圧縮してもサイズが変わらなかった実例）。
- **確かめ方**: `curl -H 'Accept-Encoding: gzip' -sI URL` の `Content-Encoding`。アクセスログに `$gzip_ratio` を足す。nginx のワーカーの CPU。
- **危険**: **CPU がボトルネックの台では逆効果。** 圧縮に CPU を使う。ベンチのクライアントが `Accept-Encoding: gzip` を送らなければ効かない。TLS 上の圧縮は BREACH の注意がある（公式）。圧縮は表現の変更で、展開後の内容は変わらない（静的ファイルの改変にはならないが、マニュアルで確かめる）。

### 事前に圧縮して返す（gzip_static・brotli_static）

- **効く条件**: 変わらない大きな CSS・JS・JSON が、毎回圧縮されている。クライアントが `br` を送る（ブラウザは通常 `br` に対応。ベンチは未確認）。
- **やり方**: あらかじめ `.gz` か `.br` を隣に作る。
  ```sh
  gzip -k -9 style.css && touch -r style.css style.css.gz
  ```
  ```nginx
  gzip_static on;       # --with-http_gzip_static_module が要る
  # brotli_static on;   # サードパーティの ngx_brotli が要る（動的モジュールにもできる）
  ```
  - `gzip_static on` は、クライアントが gzip に対応している場合だけ `.gz` を返す。`always` は常に返す。
  - 元のファイルと圧縮後のファイルの更新日時を揃える（公式の推奨）。
  - 作問者の模範解答（ISUCON10 本選）は static brotli を使い、転送量がかなり減った。
- **確かめ方**: `curl -H 'Accept-Encoding: br' -sI URL`、転送量。
- **危険**: **配布された静的ファイルの内容を変えない。** 圧縮は表現の変更で、元のファイルは残す。`nginx -V` にモジュールが入っているか確かめる。元のファイルを更新したら、圧縮ファイルも作り直す（古い `.gz` を返し続ける）。
- **出典**: [gzip_static](https://nginx.org/en/docs/http/ngx_http_gzip_static_module.html)、[ngx_brotli](https://github.com/google/ngx_brotli)、[sorah ISUCON10 本選の解答](https://isucon.net/archives/55140539.html)。実例: ISUCON5 本選、gzip_static で改善。

### Cache-Control・expires（クライアントのキャッシュ）

- **効く条件**: 同じ静的ファイルや画像を、クライアントが何度も取りに来る（アクセスログに同じ URI が大量）。クライアントがキャッシュをするか分からない（ベンチはブラウザと違うことがある）。
- **やり方**:
  ```nginx
  location /images/ {
    expires 1d;                          # Expires と Cache-Control: max-age=86400 を付ける
    add_header Cache-Control "public";   # 共有キャッシュにも許す（下の注意を読む）
  }
  ```
  - `expires` は 200・301・304 などの応答に付く。`max` は約 10 年。
  - **`add_header` は、現在のレベルに `add_header` が 1 つでもあれば、上のレベルのものを継承しない**（公式）。`expires` が付ける `Cache-Control` と二重にならないよう、応答のヘッダを `curl -I` で確かめる。
  - `rewrite` や `try_files` の後の内部リダイレクトでは、最終的に選ばれる `location` に `expires` を置く（元の `location` に書いても効かない）。
  - HTTP の仕様上、`public` が無くても、共有キャッシュ（中間のキャッシュ）が保存できる場合はある。`Authorization` つきの要求への応答は、`public`・`s-maxage`・`must-revalidate` が無ければ共有キャッシュは再利用してはならない（RFC 9111）。
- **確かめ方**: `curl -sI URL` で `Cache-Control`・`Expires`。**ベンチのログに 304 や同じ URI の減少が出るか。** ベンチがキャッシュを使うかどうかは、実際に流して見る。
- **危険**: **利用者ごとに違う応答・認証が要る応答に `public` や長い `max-age` を付けない。** 更新の反映を即時に求められる仕様（権限の変更、削除した画像が 5 秒以内に取れなくなる、など）に長い TTL を使わない。実例では、`expires` だけでは増点せず、`Cache-Control: public` を足すと得点が約倍になった（ベンチが共有キャッシュのように振る舞うという説明は筆者の推定）。逆に、ヘッダを付けても効かない場合もある。**ベンチがキャッシュするかは問題による。**
- **出典**: [nginx headers module（expires、add_header）](https://nginx.org/en/docs/http/ngx_http_headers_module.html)、[RFC 9111（public、private、max-age、共有キャッシュ）](https://www.rfc-editor.org/rfc/rfc9111.html)。rewrite 後の location に expires、public で約倍、ETag・Last-Modified だけでは条件付き要求が来ず、Cache-Control も返すと 304 が使われた、Expires と Cache-Control の併用を一般則にしない。

### ETag・Last-Modified・304（条件付き GET）

- **効く条件**: 同じ画像・静的ファイルへの要求が多く、帯域か転送時間がボトルネック。クライアントが `If-None-Match`・`If-Modified-Since` を送ってくる（アクセスログに出して確かめる）。
- **やり方**:
  - nginx が返すファイルは、既定で `ETag`（`etag on`）と `Last-Modified` を付け、条件付き要求に 304 を返す（`if_modified_since exact`）。
  - アプリが返す応答（画像を DB から返す、など）には、アプリが `ETag` と `Last-Modified` を付け、`If-None-Match` で 304 を返す。
  - 複数台で同じファイルを返すなら、台によって `Last-Modified`・`ETag` が違わないようにする（nginx の自動の ETag は、ファイルの更新時刻などから作られる。台ごとに更新時刻が違うと値が合わない可能性がある。作り方の詳細は出典未確認）。
  ```nginx
  etag on;                     # 既定
  if_modified_since exact;     # 既定。before は「更新時刻 <= 要求の時刻」で 304
  ```
- **確かめ方**: `curl -sI URL`（`ETag`）、`curl -sI -H 'If-None-Match: "..."' URL` が 304。アクセスログの 304 の割合と `$body_bytes_sent`。
- **危険**: 内容が変わったのに古い検証値で 304 を返さない。別の利用者・別の表現を同じ検証値で扱わない。アプリで ETag の判定だけをして画像を返す構成は、304 を返しても CPU が残った実例がある。複数台に複製した画像が条件付き GET で 304 を返せず帯域が飽和した実例は、更新時刻の不一致が原因という事後の推定（確定していない）。
- **出典**: [nginx core（etag、if_modified_since）](https://nginx.org/en/docs/http/ngx_http_core_module.html)、[RFC 9110（条件付き要求）](https://www.rfc-editor.org/rfc/rfc9110.html#name-conditional-requests)。

### X-Accel-Redirect（認可はアプリ、本体は nginx）

- **効く条件**: 認可が必要な画像・ファイル（署名付き URL、ログインした本人だけ）を、アプリがファイルを読んで返している。アプリの CPU・メモリ・接続がファイル転送で埋まる。
- **やり方**: アプリは認可を済ませ、本体を返さずに、内部の URI を `X-Accel-Redirect` ヘッダで返す。nginx が内部のロケーションからファイルを返す。
  ```nginx
  location /_internal_images/ {
    internal;                          # 外から直接は 404
    alias /home/isucon/image-store/;
    sendfile on;
  }
  location /api/ {
    proxy_pass http://app;             # X-Accel-Redirect を受けて内部リダイレクト
  }
  ```
  ```text
  # アプリの応答（認可が通ったとき）
  X-Accel-Redirect: /_internal_images/abc123.jpg
  Content-Type: image/jpeg
  ```
  - アプリが付けた `Content-Type`・`Cache-Control` などが内部リダイレクト後にどうなるかは出典未確認。実際の応答のヘッダを `curl -I` で確かめる。
  - ファイルが無かったとき（403・404）にアプリへフォールバックさせる設計もできる（`error_page 404 = @app`）。
- **確かめ方**: 外から `/_internal_images/...` を直接叩いて 404 になる。認可が通らない要求にファイルが返らない。応答の `Content-Type`・`Cache-Control`。
- **危険**: **内部のパスに直接アクセスできる設定ミスは、認可の失敗（critical）になる。** `internal;` を付ける。認可・署名の検証結果は変えない。書き込みの直後の読み取り、削除した画像の無効化が仕様どおりか（12 章）。

### aio threads・directio（大きなファイル）

- **効く条件**: 大きなファイル（動画など）を配信し、ディスクの読み出し待ちで nginx のワーカーが止まる（`top` の `wa`、`iostat`）。
- **やり方**: `aio threads;`（`--with-threads` が要る。`sendfile` と併用）、`directio 4m;`（大きなファイルは `sendfile` の代わりに `O_DIRECT`）。
  ```nginx
  location /video/ {
    sendfile on;
    aio threads;
  }
  ```
- **確かめ方**: 配信の応答時間と `iostat`。
- **危険**: 帯域がボトルネックのときは効かない（動画を直接配信に替えても、帯域の上限は残った実例）。画像のように小さいファイルでは通常不要。
- **出典**: [nginx core（aio、directio）](https://nginx.org/en/docs/http/ngx_http_core_module.html)、[core module（thread_pool）](https://nginx.org/en/docs/ngx_core_module.html)。

## バッファ

### proxy_buffering・proxy_buffers（応答のバッファ）

- **効く条件**: error.log に `an upstream response is buffered to a temporary file`。応答が大きく（画像、大きな JSON）、一時ファイル（`proxy_temp_path`）の I/O が見える。
- **やり方**: 既定は `proxy_buffering on`、`proxy_buffers 8 4k|8k`、`proxy_max_temp_file_size 1024m`。応答の最大に合わせて増やす。
  ```nginx
  proxy_buffer_size 16k;     # 最初の部分（ヘッダ）の大きさ。ヘッダがこれを超えると 502
  proxy_buffers 16 64k;      # 1 接続あたり 16 × 64k = 1MiB
  ```
  - **SSE・ロングポーリングなど、応答を逐次送る location は `proxy_buffering off;`**（または、アプリが `X-Accel-Buffering: no` を返す）。バッファがあるとイベントが届かない。
- **確かめ方**: error.log の警告が消えるか。nginx のワーカーのメモリ。
- **危険**: バッファは 1 接続ごとに確保されるので、`バッファの合計 × 接続数` がメモリを使う。台のメモリを見積もる。効果の単独の確認が難しい（他の設定と一緒に変えて判定できなかった実例）。

### client_body_buffer_size・client_max_body_size・proxy_request_buffering（リクエストのボディ）

- **効く条件**: POST（画像アップロードなど）が大きい。error.log に `a client request body is buffered to a temporary file`。アップロードが 413（`client_max_body_size` 超過）で失敗する。
- **やり方**: `client_body_buffer_size` の既定は 8k（x86-64。2 ページ分）、`client_max_body_size` の既定は 1m（超えると 413）。
  ```nginx
  client_max_body_size 32m;           # 仕様の上限に合わせる。0 で検査なし
  client_body_buffer_size 1m;         # ボディの大半がメモリに載る大きさ
  # proxy_request_buffering off;      # ボディを読みながらアプリへ流す（再送できなくなる）
  ```
  - ボディが `client_body_buffer_size` を超えると、一時ファイルに書く。
  - `proxy_request_buffering off` は、ボディをバッファせずに流す。nginx が送り始めた後は次の upstream へ再送できない。
- **確かめ方**: error.log の警告、413 の数。
- **危険**: `client_max_body_size` は**仕様のエラーの形（413 か 400 か）を変えうる**。「大きすぎる画像は 400」と仕様で決まっているなら、nginx で 413 にしない。バッファを大きくすると、同時接続数 × サイズでメモリを使う。
- **出典**: [nginx core（client_body_buffer_size、client_max_body_size、client_body_temp_path）](https://nginx.org/en/docs/http/ngx_http_core_module.html)、[proxy module（proxy_request_buffering）](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。実例: matsuu、一時ファイルの警告を見て client_body_buffer_size を調整。

## キャッシュ

### proxy_cache・fastcgi_cache（応答をキャッシュする）

- **効く条件**: 同じ GET への応答が、利用者に依らず同じ（匿名のランキング、公開情報、一覧）で、アプリが毎回作り直している。アクセスログで、同じ URI のリクエストが多い。
- **やり方**:
  ```nginx
  proxy_cache_path /var/cache/nginx levels=1:2 keys_zone=api:10m max_size=1g inactive=10m;
  location /api/public/ {
    proxy_cache api;
    proxy_cache_key "$scheme$host$request_uri";
    proxy_cache_valid 200 1s;           # 短い TTL（鮮度の上限より短く）
    proxy_cache_lock on;                # 同じキーの同時ミスを 1 本にまとめる
    proxy_cache_use_stale updating;     # 更新中は古い応答を返す
    proxy_cache_background_update on;   # 背景で更新（use_stale updating が要る）
    add_header X-Cache $upstream_cache_status;
    proxy_pass http://app;
  }
  ```
  - キャッシュされる条件: 既定は `GET`・`HEAD`、`proxy_cache_valid` で時間を指定した応答（時間だけ書くと 200・301・302）。`Set-Cookie` つきの応答、`Vary: *` の応答は保存されない。上流の `Cache-Control`・`Expires`・`X-Accel-Expires` があればそちらが優先される。
  - `keys_zone` は 1MB で約 8,000 キー。
  - fastcgi（PHP-FPM など）は `fastcgi_cache_path`・`fastcgi_cache`・`fastcgi_cache_key`・`fastcgi_cache_valid` が同じ形。
- **確かめ方**: `$upstream_cache_status`（HIT・MISS・EXPIRED・UPDATING など）をアクセスログに出し、HIT 率を数える。**全部 MISS なら採用を見直す**（Squid で全 MISS の調査に 1 時間使い断念した実例）。
- **危険**: **利用者ごとに違う応答（認証・閲覧権限・公開範囲で変わるもの）をキャッシュキーに利用者を含めずに保存すると、他人に見せてしまう（critical）。** 書き込みの直後の読み（自分で書いたものが見えること、削除・ブロックの即時反映）を壊さない。複数のプロセス・台で同じキャッシュを使えるとは限らない（台ごとの別のキャッシュ）。鮮度の上限が 1 秒の仕様に、長い TTL を使わない。外向き帯域がボトルネックなら、キャッシュしても効かない。
- **出典**: [nginx proxy module（proxy_cache_path、proxy_cache_valid、proxy_cache_lock、proxy_cache_use_stale、proxy_cache_background_update、キャッシュの条件）](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[fastcgi module](https://nginx.org/en/docs/http/ngx_http_fastcgi_module.html)。ハッシュ URL の proxy_cache は外向き帯域がボトルネックのため効かず、作問者解答 ISUCON10 本選、Varnish で TTL 0.5 秒・grace 0.2 秒。

## プロトコル（HTTP/2・HTTP/3・TLS）

### HTTP/2

- **効く条件**: クライアントが 1 台から大量の要求を出し、接続の確立（TLS の握手）や多重化が効く。ベンチのクライアントが HTTP/2 に対応している。
- **やり方**:
  ```nginx
  server {
    listen 443 ssl;
    http2 on;                         # 1.25.1 以降。それ以前は listen 443 ssl http2;
    http2_max_concurrent_streams 128; # 既定 128
  }
  ```
  - HTTP/2 には `--with-http_v2_module` と TLS（ALPN）が要る。
  - nginx から upstream（アプリ）への HTTP/2 は、`proxy_http_version` に 2 を指定できる版もあるが、通常は 1.1 のままでよい。
- **確かめ方**: `curl -sI --http2 -k https://host/`、アクセスログの `$server_protocol`。
- **危険**: ベンチのクライアントが HTTP/2 に対応していなければ、効果は無い。接続の終了（GOAWAY）の扱いでベンチが失敗することがある（ベンチ側の不具合と後に判明）。TLS が競技の入口で使えるかは、マニュアルとルーターの経路で決まる。
- **出典**: [nginx v2 module](https://nginx.org/en/docs/http/ngx_http_v2_module.html)。実例: matsuu（ISUCON9 予選で HTTP/2 を有効に。単独効果は未分離）。

### HTTP/3（QUIC）

- **効く条件**: 通常は効く場面が無い。
- **やり方**: `listen 443 quic reuseport;` と `listen 443 ssl;`、`add_header Alt-Svc 'h3=":443"; ma=86400';`。`--with-http_v3_module` が要る。UDP の 443 を開ける。
- **確かめ方**: `$http3` をログに出す。
- **危険**: 公式が experimental としている。ベンチのクライアントが対応していなければ意味が無い。UDP の通信が経路で通るかも問題。
- **出典**: [nginx v3 module](https://nginx.org/en/docs/http/ngx_http_v3_module.html)。ISUCON での実例は見つかっていない（出典未確認）。

### TLS（セッション再開・プロトコル・暗号・証明書）

- **効く条件**: TLS の握手が CPU を食う（`perf top` に RSA・ECDH・`ossl` 系）。接続数が多く、接続の使い回しが少ない。
- **やり方**:
  ```nginx
  ssl_protocols TLSv1.2 TLSv1.3;         # 既定（1.23.4 以降）
  ssl_session_cache shared:SSL:10m;      # 1MB で約 4,000 セッション
  ssl_session_timeout 10m;               # 既定 5m
  ssl_session_tickets on;                # 既定 on
  ssl_buffer_size 16k;                   # 既定。小さい応答の最初の 1 バイトを早くするなら 4k
  ```
  - `keepalive_timeout`・`keepalive_requests` で接続を長く再利用するのが先（ISUCON14 の感想戦の実例は、keepalive の寿命と上限、TLS のセッション再利用を合わせて CPU の飽和を解消した）。
  - 証明書を RSA から ECDSA に替える実例もある（ISUCON14 感想戦。自己署名の証明書）。
  - `ssl_early_data`（0-RTT）は、リプレイの危険がある。冪等な要求だけに限る。
- **確かめ方**: `openssl s_client -connect host:443 -reconnect`（セッション再開されるか）、`perf top`、`$ssl_session_reused` をログに出す。
- **危険**: 当日のルールと、証明書を検証する要件（自己署名が使えるか）を確認する。暗号の指定は単独で比べる（軽そうな暗号に変えただけでは改善の証拠にならない）。
- **出典**: [nginx ssl module](https://nginx.org/en/docs/http/ngx_http_ssl_module.html)。

## ログ

### access_log の buffer・off・条件つき

- **効く条件**: アクセスログの書き込みが CPU・I/O に見える（大量のリクエスト、`iostat` で nginx のログへの書き込み）。計測が終わった後の本番ベンチ。
- **やり方**:
  ```nginx
  access_log /var/log/nginx/access.log ltsv buffer=64k flush=5s;   # バッファして書く
  # access_log off;                                                 # 完全に止める
  # access_log /var/log/nginx/access.log main if=$loggable;         # 条件つき
  ```
  - `buffer=` は、次の行が入らないとき、`flush` の時間を過ぎたとき、ワーカーの再読み込み・終了のときに書く。
  - `gzip` を付けると圧縮して書く（`zcat` で読める）。`open_log_file_cache` は、パスに変数を含むログで FD を使い回す。
- **確かめ方**: nginx のワーカーの `sys`・`wa`、ディスクの書き込み。
- **出典**: [nginx log module](https://nginx.org/en/docs/http/ngx_http_log_module.html)。実例: 条件付き GET と圧縮のヘッダを専用ログへ。

### error_log

- **効く条件**: 警告・エラーが大量にログへ出ている。
- **やり方**: `error_log /var/log/nginx/error.log warn;`（既定は `error`）。`debug` は重い。
- **確かめ方**: error.log のサイズと内容。
- **危険**: レベルを上げすぎると、上の 5 分チェックの警告が見えなくなる。`warn` までは残す。
- **出典**: [nginx core（error_log）](https://nginx.org/en/docs/ngx_core_module.html)。

## 制限と振り分け

### limit_req・limit_conn は使ってよいか

- **効く条件**: 特定の IP・URL に要求が集中して、ほかの利用者を巻き込む。bot（ベンチ以外のアクセス）を抑えたい。
- **やり方**:
  ```nginx
  limit_req_zone $binary_remote_addr zone=perip:10m rate=10r/s;
  location /api/search {
    limit_req zone=perip burst=20 nodelay;
    limit_req_status 429;               # 既定は 503
  }
  ```
  超過した要求は、`burst` の範囲では遅延（`nodelay` で即時）、超えると拒否される。`limit_req_dry_run on` で、拒否せずに数だけ数えられる。
- **確かめ方**: `$limit_req_status`（PASSED・DELAYED・REJECTED）をログに出して、まず `dry_run` で拒否される件数を見る。
- **危険**: **ベンチのリクエストを拒否する（429・503 が返る）と、エラー率の増加や失格になる。** ベンチは多数の利用者を、少数の IP から（または `X-Client-IP` のようなヘッダで IP を変えて）送ることがある。ベンチを識別して特別扱いすることも失格の原因になりうる（出典未確認。マニュアルで確かめる）。**過負荷を 503 で逃がす実装は、判定の対象になる可能性がある**。使うなら `dry_run` で数字を見てから、仕様上許されることを人間に確かめる。
- **出典**: [nginx limit_req module](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)。実例: 負荷が上がって出現した bot を nginx で扱う、（D-17）。

### upstream の振り分け（round-robin・weight・least_conn・ip_hash・hash）

- **効く条件**: 複数台のアプリ（11 章）を nginx が束ねる。
- **やり方**:
  ```nginx
  upstream app {
    server 10.0.0.11:8080 weight=2;
    server 10.0.0.12:8080;
    # least_conn;                   # 接続が少ない台へ
    # ip_hash;                      # 利用者（IPv4 の先頭 3 オクテット）で固定
    # hash $cookie_session consistent;   # キーで固定。台が増減しても再配置が少ない
    # random two least_conn;        # ランダムに 2 台選び、接続の少ないほうへ
    keepalive 64;
  }
  ```
  - `server` のパラメータ: `weight`、`max_conns`、`max_fails`（既定 1）、`fail_timeout`（既定 10s）、`backup`、`down`。
  - 状態を持つアプリ（メモリのキャッシュ、メモリのセッション）を複数台に振るなら、`ip_hash`・`hash` で同じ利用者を同じ台に固定するか、状態を共有する（11 章）。
- **確かめ方**: アクセスログに `$upstream_addr` を出し、台ごとの件数・応答時間・CPU を比べる。
- **危険**: **振り分けだけでは速くならない。** DB・帯域・共通のキャッシュがボトルネックなら、`least_conn` でアプリ 2 台に振ってもスコアが下がった実例がある（約 30 分かけて戻した）。ロックが必要な URI だけを 1 台に寄せる分散は、ベンチに通らなかった実例がある。`ip_hash` は、ベンチが少数の IP から送ると偏る。
- **出典**: [nginx upstream module](https://nginx.org/en/docs/http/ngx_http_upstream_module.html)。acidlemon（ISUCON12 予選の作問者の解答。テナント名の長さで台を分けた）。

### パス・メソッドで振り分ける（map・location）

- **効く条件**: 重い処理（画像の検査、bcrypt のログイン）を別の台やプロセスへ送りたい。GET と書き込みで処理先を分けたい（11 章）。
- **やり方**:
  ```nginx
  map $request_method $upstream_for_method {
    GET     app_read;
    HEAD    app_read;
    default app_write;
  }
  location /api/ {
    proxy_pass http://$upstream_for_method;   # 変数を使う場合は upstream 名だけ（URI を書かない）
  }
  location = /login {
    proxy_pass http://app_login; # bcrypt を別の台へ
  }
  ```
  - `location` の一致は、完全一致 `=`、`^~`、正規表現（書かれた順で最初に一致したもの）、最長の接頭辞の順（公式）。`location /api/isu` と `/api/isu/` の一致の範囲の違いで、意図しない台に送った実例がある。
  - `proxy_pass` に変数を使うと、URI の扱いが変わることがある（URI 部分を書くと置き換えが働かない、など）。出典未確認。実際の `$upstream_addr` と、アプリが受け取る URI で確かめる。
- **確かめ方**: アクセスログの `$upstream_addr` で、想定の台に届いているか。
- **危険**: 状態（セッション・キャッシュ）がプロセス・台で分かれる。書き込みと読みで台が分かれると、書いた直後の読みに反映されない可能性がある。
- **出典**: [nginx core（location の一致の順）](https://nginx.org/en/docs/http/ngx_http_core_module.html)、[proxy module（proxy_pass）](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。5.3（`map $request_method`）。

### 失敗時の再試行（proxy_next_upstream・max_fails）

- **効く条件**: 複数台で 1 台が落ちたとき、要求を別の台に流したい。
- **やり方**: `proxy_next_upstream` の既定は `error timeout`。`non_idempotent` を足さない限り、POST・LOCK・PATCH は再試行されない。`proxy_next_upstream_tries`・`proxy_next_upstream_timeout` で上限を決める。
- **確かめ方**: アクセスログの `$upstream_addr`（カンマ区切りで複数の台が出れば再試行している）。
- **危険**: **POST を再試行すると、書き込みが二重になる（二重予約・二重課金など、critical）。** `non_idempotent` は付けない。
- **出典**: [nginx proxy module（proxy_next_upstream）](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。

### タイムアウトと切断（proxy_read_timeout・send_timeout・reset_timedout_connection）

- **効く条件**: 遅い要求が接続を占有する。ベンチが切った後の接続が残る。504 が出る。
- **やり方**: 既定は `proxy_connect_timeout 60s`・`proxy_read_timeout 60s`・`proxy_send_timeout 60s`（いずれも 2 つの読み書きの間の時間で、応答全体ではない）、`send_timeout 60s`。`reset_timedout_connection on;` でタイムアウトした接続を RST で切って、ソケットのメモリを早く解放する。
- **確かめ方**: 504 の件数、`ss -s` の `closed`・`timewait`。
- **危険**: 短くしすぎると、遅いが正しい応答を切って、失敗が増える。SSE・ロングポーリングは `proxy_read_timeout` を長く。
- **出典**: [nginx proxy module（timeouts）](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[core（send_timeout、reset_timedout_connection、lingering_close）](https://nginx.org/en/docs/http/ngx_http_core_module.html)。

### SSE・WebSocket の location

- **効く条件**: SSE（`text/event-stream`）・WebSocket・ロングポーリングを使う。
- **やり方**: SSE は `proxy_buffering off;`、`proxy_read_timeout` を長く、`proxy_http_version 1.1; proxy_set_header Connection "";`。WebSocket は `proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`。アプリは、データのイベントが無くても心拍を送る必要がある場合がある。
- **確かめ方**: クライアントでイベントが遅れずに届くか。切断の後の再接続（`Last-Event-ID`）の挙動。
- **危険**: バッファが残るとイベントが届かず、仕様の検査に落ちる。
- **出典**: [nginx proxy module（proxy_buffering、X-Accel-Buffering）](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。実例: SSE の location の proxy_buffering。

## OpenResty・Lua

### OpenResty / lua-nginx-module

- **効く条件**: 簡単な認可・キャッシュ・集計を、アプリに届く前に nginx で済ませたい。共有メモリ（`lua_shared_dict`）に小さな状態を置きたい。
- **やり方**: OpenResty は、nginx、LuaJIT、Lua のライブラリを束ねたプラットフォーム。
  ```nginx
  lua_shared_dict cache 10m;
  location /api/hot {
    access_by_lua_block {
      local v = ngx.shared.cache:get(ngx.var.uri)
      if v then
        ngx.say(v)
        return ngx.exit(200)
      end
    }
    proxy_pass http://app;
  }
  ```
  - `lua_code_cache` は既定 on（off は本番で強く非推奨）。
  - Lua でブロックする処理（ブロックするライブラリ、重い計算）は、イベントループを止める。ネットワークは cosocket（モジュールの API）を使う。
  - 共有メモリは、設定の再読み込み（HUP）では消えない。再起動では消える。
- **確かめ方**: `ngx.shared` の利用量、ヒット率、nginx のワーカーの CPU。
- **危険**: **共有メモリはプロセス（台）をまたがず、再起動で消える。** キャッシュなら、ミスのときに DB から作り直せる形にする。永続化が要るデータには使わない。利用者別の応答を共通のキーで返さない。導入（ビルド・モジュール）に時間がかかり、運営の環境で動くかも確かめる。ISUCON での使用実績は、この調査では確認していない。
- **出典**: [OpenResty](https://openresty.org/en/)、[lua-nginx-module](https://github.com/openresty/lua-nginx-module)（lua_shared_dict、access_by_lua_block、lua_code_cache、ブロックの注意）。ISUCON での実例は出典未確認。

## 設定の反映と戻し方

### nginx の設定をリポジトリで管理し、検証してから reload

- **効く条件**: 設定の編集先と実行先がずれる。台ごとに設定が違う。
- **やり方**: 設定をリポジトリに置き（`include` で読む。実例: `nginx.conf` から `webapp/infra/nginx/sites-enabled` を include）、デプロイで台に配る。反映は `sudo nginx -t && sudo systemctl reload nginx`。
  ```sh
  sudo nginx -t              # 構文の検査
  sudo nginx -T | less       # 有効な設定を全部出す
  sudo systemctl reload nginx
  ```
- **確かめ方**: `nginx -T` で、期待の設定が有効か。反映の後に `curl` で主要な URL を確かめる。
- **危険**: 設定が壊れたまま `reload` しても気づかないことがある（出典未確認）。`nginx -t` を必ず通す。設定を台ごとに違えるなら、台ごとの上書きをリポジトリに置く。再起動で nginx が上がること（`systemctl is-enabled nginx`）。
- **出典**: 実例: sue445、設定の所在を固定、nginx をコンテナから切り離す。`nginx -t`・`-T` の動作は出典未確認（nginx のコマンドラインの文書）。

## よくある失敗

1. **keepalive を有効にして、前後を比べずに残す。** 点が約 67,000 → 30,000 に落ちた実例がある。1 つずつ変え、悪くなったら戻す。
2. **upstream の keepalive のタイムアウトが、アプリ側のアイドル切断より長い。** 閉じる瞬間の要求が 502 になる。upstream の `keepalive_timeout` をアプリより短く。
3. **静的ファイルの配信に移したら画面が崩れた。** MIME・`alias` の末尾のスラッシュ・相対 URL・圧縮ヘッダ・`add_header` の継承を見る。
4. **Unix ソケットにして、再起動後にソケットが無い。** 最終の再起動で 0 点。
5. **複数の変更をまとめて入れて、効いたか分からない。** `proxy_buffers` を他の設定と一緒に変えて判定できなかった実例がある。
6. **ヘッダを付けたのにキャッシュされない・されすぎる。** `expires` と `add_header` の継承、`rewrite` 後の `location`、`Set-Cookie` つきの応答、利用者ごとに違う応答への `public`。
7. **振り分けが速さにならない。** DB・帯域がボトルネックのとき、アプリを増やしても効かない。
8. **帯域がボトルネックなのに、CPU 側の手を打つ。** 動画の直接配信、`proxy_cache`、リダイレクトの削減は、外向き帯域が上限のときは効かない。先に転送量と帯域を見積もる（12 章）。
9. **`limit_req` や UA での分岐を入れる。** ベンチのリクエストを拒否して失敗が増える、ベンチの識別になる。

## 出典一覧

公式文書:

- nginx: [core module](https://nginx.org/en/docs/ngx_core_module.html)、[http core](https://nginx.org/en/docs/http/ngx_http_core_module.html)、[upstream](https://nginx.org/en/docs/http/ngx_http_upstream_module.html)、[proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[ssl](https://nginx.org/en/docs/http/ngx_http_ssl_module.html)、[gzip](https://nginx.org/en/docs/http/ngx_http_gzip_module.html)、[gzip_static](https://nginx.org/en/docs/http/ngx_http_gzip_static_module.html)、[headers](https://nginx.org/en/docs/http/ngx_http_headers_module.html)、[log](https://nginx.org/en/docs/http/ngx_http_log_module.html)、[limit_req](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)、[v2](https://nginx.org/en/docs/http/ngx_http_v2_module.html)、[v3](https://nginx.org/en/docs/http/ngx_http_v3_module.html)、[fastcgi](https://nginx.org/en/docs/http/ngx_http_fastcgi_module.html)、[events](https://nginx.org/en/docs/events.html)
- HTTP: [RFC 9111（キャッシュ）](https://www.rfc-editor.org/rfc/rfc9111.html)、[RFC 9110（条件付き要求）](https://www.rfc-editor.org/rfc/rfc9110.html#name-conditional-requests)
- その他: [ngx_brotli](https://github.com/google/ngx_brotli)、[OpenResty](https://openresty.org/en/)、[lua-nginx-module](https://github.com/openresty/lua-nginx-module)

参加記・解説・資料:

- [kazeburo: ISUCON4 予選問題の事後練習](https://kazeburo.hatenablog.com/entry/2014/10/14/170129)
- [fujiwara: 初回 ISUCON](https://sfujiwara.hatenablog.com/entry/20110829/1314597283)
- [sorah: ISUCON10 本選の解答（作問者の事後解答）](https://isucon.net/archives/55140539.html)
- [kujira16: ISUCON11 予選](https://kujira16.hateblo.jp/entry/2021/08/22/231020)
- 書籍『達人が教えるWebパフォーマンスチューニング』（技術評論社）の第 6 章「リバースプロキシの利用」（nginx のアーキテクチャ、圧縮、バッファリング、コネクション管理、TLS）と第 8 章（8-4 静的ファイル配信、8-5 HTTP ヘッダでクライアントにキャッシュさせる）は目次のみ参照: [gihyo.jp](https://gihyo.jp/book/2022/978-4-297-12846-3)
