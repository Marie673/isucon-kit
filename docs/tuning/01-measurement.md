# 01 計測

当日のマニュアルが優先する。計測ツールの導入・外部への送信・ポートの開放・ログの置き場所は、その回のレギュレーションと環境（競技用サーバーの台数、EBS の容量、運営用ファイル）で決まる。

## この章で扱う範囲

- アクセスログの集計（alp、kataribe、LTSV と JSON の `log_format`、URI のまとめ方）。
- スロークエリ（MySQL の slow log と pt-query-digest、PostgreSQL の `pg_stat_statements`・`auto_explain`・`log_min_duration_statement`）。
- プロファイラ（Go の pprof・fgprof、Rust の perf・cargo flamegraph・pprof-rs、perf top、フレームグラフ）。
- トレース（OpenTelemetry の採取率とオーバーヘッド）。
- OS の計測（top、vmstat、mpstat、pidstat、iostat、sar、ss、iftop、nethogs、netdata）。
- DB 内部の計測（InnoDB status、performance_schema、`pg_stat_activity`・`pg_locks`・`pg_stat_user_tables`）。
- 計測ツール自体のオーバーヘッド。
- ベンチ 1 本ごとにログを退避して回収する流れ。
- ベンチの山の窓に揃えた集計（`tools/bench-window`）。

扱わないもの: 計測で見つけたボトルネックの直し方（各層の章）、ログを切る終盤の手順（[15 章](15-endgame.md)）、スコアのモデル（[14 章](14-benchmarker-and-scoring.md)）。

## 最初に見るもの（当日の 5 分チェックリスト）

「推測するな、計測せよ」が出題者の講評の定番（[ISUCON11 予選の講評](https://isucon.net/archives/56044867.html)）。計測してから直す。次の順で 5 分。

| # | 確かめること | どうやって | 次に見る項目 |
| --- | --- | --- | --- |
| 1 | どの層の CPU が張り付くか | ベンチ中に全台で `top`、`vmstat 1`、`mpstat -P ALL 1` | OS の計測 |
| 2 | どの URL が合計時間を食うか | nginx に `request_time` 入りのログを出して alp を `--sort sum -r` | アクセスログ |
| 3 | どの SQL が合計時間を食うか | MySQL は slow log + pt-query-digest、PostgreSQL は `pg_stat_statements` | スロークエリ |
| 4 | アプリの CPU はどの関数か | Go は pprof、Rust は perf + フレームグラフ | プロファイラ |
| 5 | 1 本ごとに結果を残せるか | ベンチ前にログを退避し、ベンチ後に集計する手順を 1 コマンドにする | ベンチ 1 本ごとの回収 |
| 6 | 平均で隠れた山がないか | 走行の最後の数十秒だけを切り出して集計 | 山の窓 |
| 7 | 計測自体が重くないか | 計測を入れた版と切った版を同じ commit で比べる | オーバーヘッド |

- 1 と 2 と 3 は、ベースラインのベンチ 1 本で全部そろう。仕込みはベンチを流す前に終える。
- 出題者の講評に共通する最初の一歩は「DB のプロファイルとインデックス」。ISUCON10 予選と ISUCON12 予選の講評は、スロークエリログと pt-query-digest の名前を挙げる。ISUCON13 の講評は「DB のプロファイリング」を最初の一歩と推測し、N+1 の特定には各言語のプロファイラや APM を挙げる（[ISUCON10 予選](https://isucon.net/archives/55025156.html)、[ISUCON12 予選](https://isucon.net/archives/56850281.html)、[ISUCON13](https://isucon.net/archives/58001272.html)）。
- 計測の結果は、まず「どの層がボトルネックか」を言葉にする。層が分かるまで、直す手は入れない。

---

## 1. アクセスログの集計

### nginx の `log_format` を LTSV か JSON にして、処理時間を入れる

- **効く条件**: どの URL が遅いか、どの URL の合計時間が大きいか分からない。nginx のログに `request_time` が無い。
- **やり方**: `log_format` に `$request_time`（nginx が受けてから返し終わるまで）と `$upstream_response_time`（アプリの応答時間）を入れる。alp の README が出している形は次のとおり。
  ```nginx
  # LTSV（alp ltsv）
  log_format ltsv "time:$time_local"
                  "\thost:$remote_addr"
                  "\treq:$request"
                  "\tstatus:$status"
                  "\tmethod:$request_method"
                  "\turi:$request_uri"
                  "\tsize:$body_bytes_sent"
                  "\treqtime:$request_time"
                  "\tcache:$upstream_http_x_cache"
                  "\tapptime:$upstream_response_time"
                  "\tvhost:$host";
  access_log /var/log/nginx/access.log ltsv;
  ```
  ```nginx
  # JSON（alp json）。escape=json を付ける
  log_format json escape=json '{"time":"$time_local",'
                              '"method":"$request_method",'
                              '"uri":"$request_uri",'
                              '"status":"$status",'
                              '"body_bytes":$body_bytes_sent,'
                              '"request_time":$request_time,'
                              '"response_time":"$upstream_response_time"}';
  ```
- **確かめ方**: ベンチを流した後に `tail -3 /var/log/nginx/access.log` で、キーが全部出ているか見る。alp の既定のキー名（LTSV は `reqtime`・`apptime`、JSON は `request_time`・`response_time`）と合わせると、名前の指定が要らない。
- **危険**:
  - `$upstream_response_time` は、upstream が複数回呼ばれたり失敗したりすると `,` 区切りや `-` になる。JSON では引用符付きの文字列にしておく（alp の README の例もそう）。数値として入れると、JSON が壊れる回がある。
  - `escape=json` を付けないと、ユーザーエージェントや URI の引用符で JSON が壊れる。
  - 毎リクエストのログ書き込みは I/O と CPU を使う。終盤にバッファか停止にする（次項、[15 章](15-endgame.md)）。
  - ログは本物のユーザー情報・認証情報を含みうる。外へ出さない。
- **出典**: [tkuchiki/alp README](https://github.com/tkuchiki/alp)、[nginx ngx_http_log_module](https://nginx.org/en/docs/http/ngx_http_log_module.html)。

### alp でエンドポイント別に集計する（合計時間の降順）

- **効く条件**: 「1 回が遅い URL」と「1 回は速いが回数が多い URL」を分けたい。
- **やり方**: `--sort sum -r` で合計時間の大きい順に並べる。
  ```sh
  alp ltsv --file /var/log/nginx/access.log --sort sum -r \
    --format markdown -o count,2xx,4xx,5xx,method,uri,avg,p99,sum --show-footers
  alp json --file access.json --sort p99 -r --percentiles 50,90,99
  ```
  - `--sort` は `count`（既定）・`max`・`avg`・`sum`・`p90`・`p95`・`p99`・`stddev`・`uri`・`method`・`max-body`。
  - `-f`（`--filters`）で絞る。例: `-f "Status >= 500"`、`-f "Uri matches '^/api/'"`。
  - `--pos alp.pos` で前回の続きから読む。`--nosave-pos` は位置を更新しない。
- **確かめ方**: 上位の URL の `sum` が、ベンチ時間（60 秒）× 同時数に収まっているか見る。`sum` は CPU 時間ではなく待ち時間を含む。DB 待ちが大きい URL と CPU が大きい URL は、`sum` だけでは分からない。
- **出典**: [tkuchiki/alp README](https://github.com/tkuchiki/alp)。

### URI のまとめ方（`:id` を 1 行にする）

- **効く条件**: `/api/items/123`、`/api/items/124` が別の行になり、上位が数千行で読めない。クエリ文字列付きの URI が別の行になる。
- **やり方**: alp の `-m`（`--matching-groups`）に正規表現を渡す。先に一致したものが勝つ。
  ```sh
  alp ltsv --file access.log --sort sum -r -m '^/api/items/[0-9]+$'
  ```
  - 複数の正規表現をまとめて渡す書き方（カンマ区切りなど）は、alp の `--help` で確かめる。本章は出典を確認していない。
  - `conf/alp-groups.txt` は 1 行 1 つの正規表現で、`tools/bench-window` の `--group` も同じ形のファイルを読む。1 つのファイルを alp とバッチ集計で共有できる。
  - クエリ文字列は、`$request_uri` ではなく `$uri`（クエリなし）をログに出す手もある。`tools/bench-window` はクエリを落として URI を数える。
- **確かめ方**: まとめた後の行数が 20〜40 行に収まり、上位 10 行が合計時間の大半を占めるか見る。
- **危険**: 正規表現が広すぎると、重い URL と軽い URL が 1 行に混ざる（例: `/api/.+`）。パスのパラメータが違うだけで重さが違う URL（検索条件など）は、まとめずに別の行にする。
- **出典**: [tkuchiki/alp README](https://github.com/tkuchiki/alp)（`-m` の説明）、`isucon-kit/tools/bench-window/README.md`（クエリを落とす）。

### 変更の前後を alp の `--dump`・`diff` で比べる

- **効く条件**: 変更が効いたかを、点ではなく URL ごとの時間で見たい。
- **やり方**:
  ```sh
  alp json --file before.json --dump before.yaml
  alp json --file after.json  --dump after.yaml
  alp diff before.yaml after.yaml
  ```
- **確かめ方**: 狙った URL の `avg`・`p99` が下がり、他の URL が悪化していないか見る。
- **危険**: ベンチの負荷（人数や回数）が前後で違うと、`count` や `sum` は比べられない。ベンチごとに利用者が増えるアプリ（[14 章](14-benchmarker-and-scoring.md)）では、点が上がるほど `sum` も増える。`avg` と `p99` で比べる。
- **出典**: [tkuchiki/alp README](https://github.com/tkuchiki/alp)。実例: 毎回保存して前後を比較、ログ集計の差分を表示。

### kataribe を使う場合

- **効く条件**: alp ではなく kataribe の出力形式が好みか、チームの既存の手順が kataribe。
- **やり方**: nginx は `log_format` の末尾に `$request_time` を足す。
  ```sh
  go install github.com/matsuu/kataribe@latest
  kataribe -generate          # kataribe.toml を作る
  cat /var/log/nginx/access.log | kataribe
  ```
  - 出力は URL ごとの Count、Total、Mean、Stddev、Min、P50/P90/P95/P99、Max、ステータス別の件数と、遅いリクエストの上位。
  - README によると、`$upstream_response_time` は本文の対象外。
- **確かめ方**: Total の降順で上位を読む。
- **危険**: `kataribe.toml` の設定項目（スケール、パーセンタイル、URL のまとめ方）は README に説明が無い。`kataribe -generate` が作るファイルのコメントで確かめる。本章は確認していない。
- **出典**: [matsuu/kataribe README](https://github.com/matsuu/kataribe)。

### ログに入れておくと後で効く項目（リクエスト ID・利用者 ID・キャッシュの状態）

- **効く条件**: 同じ URL でも利用者ごと・状態ごとに重さが違う。N+1 と、別のリクエストからの同じ SQL を区別したい。キャッシュヘッダの効き方を見たい。
- **やり方**:
  - アプリがレスポンスヘッダに利用者 ID を入れ、nginx が `$sent_http_x_user_id` のようにログへ出す（個人情報は外へ出さない）。
  - リクエスト ID をアクセスログと SQL コメントの両方に出す。
  - 条件付き GET と圧縮の要求・応答ヘッダを専用のログに出す。
- **確かめ方**: 利用者 ID ごとに並べて、1 人の操作の列として読める（アクセスログをシナリオとして読む）。
- **危険**: SQL の正規化やコメント除去でリクエスト ID が消えないか確かめる。ログに認証トークンを出さない。

### nginx のログのバッファと条件付き出力

- **効く条件**: ログの書き込み自体が I/O を使っている。ベンチ中だけ詳細ログを出し、他はまとめたい。
- **やり方**:
  ```nginx
  access_log /var/log/nginx/access.log ltsv buffer=64k flush=1s;   # バッファして 1 秒ごとに書く
  access_log off;                                                  # 止める
  map $status $loggable { ~^[23] 0; default 1; }
  access_log /var/log/nginx/access.log ltsv if=$loggable;          # 2xx・3xx を出さない
  ```
  - `buffer=` はバッファ書き込み。サイズはディスクのアトミック書き込みの大きさ以下。
  - `gzip` を付けると圧縮して書く。`zcat` でいつでも読める。
  - バッファの中身は、次のログが入らないとき・`flush=` の時間を過ぎたとき・ワーカーがログを開き直すとき・終了するときに書かれる。
  - ログのパスに変数を使うと、バッファ書き込みは効かない。
- **確かめ方**: ベンチ中の `iostat -xz 1` の書き込みと、alp の集計が欠けていないか見る。
- **危険**: バッファしたログは、ベンチ直後に `tail` しても最後の数秒が無いことがある。ローテートの前に `nginx -s reopen` でバッファを書かせる。`if=` で 2xx を捨てると、alp の `count` と `sum` が実際より小さくなる。計測用のログと、終盤の軽いログを混ぜない。
- **出典**: [nginx ngx_http_log_module](https://nginx.org/en/docs/http/ngx_http_log_module.html)。ISUCON10 予選の講評に「高負荷時にはログ出力を止めること」がある（[講評](https://isucon.net/archives/55025156.html)）。

---

## 2. スロークエリ

### MySQL の slow query log を入れる

- **効く条件**: DB の CPU が高い。どの SQL が重いか分からない。
- **やり方**: 起動時の設定ファイルか、実行中の `SET GLOBAL`。
  ```ini
  # my.cnf の [mysqld]
  slow_query_log      = 1
  slow_query_log_file = /var/log/mysql/mysql-slow.log
  long_query_time     = 0        # 全部。まずは 0 で 1 本流して集計する
  log_queries_not_using_indexes = 0
  ```
  ```sql
  SET GLOBAL slow_query_log = 'ON';
  SET GLOBAL long_query_time = 0;       -- 秒。マイクロ秒の精度
  SHOW VARIABLES LIKE 'slow_query%';
  ```
  - `long_query_time` の既定は 10 秒、最小は 0。0 にすると、時間の条件が常に満たされる。
  - `log_queries_not_using_indexes` はインデックスを使わない文を出す。`log_throttle_queries_not_using_indexes` で 1 分あたりの上限を決められる。
  - `min_examined_row_limit` で、走査行数が少ない文を除く。
  - `log_slow_extra`（8.0.14 以降）で行数などの追加情報を出す。
  - 実行中に `slow_query_log_file` を変えると、今のファイルを閉じて新しいファイルを開く。
- **確かめ方**: `SHOW VARIABLES LIKE 'slow_query%'` と `long_query_time`。ベンチを流してログが増えているか見る。
- **危険**:
  - `long_query_time = 0` は全部の文を書く。ログが急に大きくなり、書き込みの I/O と CPU を使う。ディスクの残量を見る（[15 章](15-endgame.md)）。
  - **MySQL を再起動すると、`SET GLOBAL` の値は消える**（ISUCON13 向けの道具のリポジトリにも同じ注意がある。[isucon-tool の例](https://github.com/toshikingggg/isucon-tool)）。my.cnf にも書く。
  - 速い SQL の回数が多い場合（N+1）は、`long_query_time = 0` でないと見えない。ISUCON9 本選の参加者は 0 にして、速い SQL の回数を数えた。
  - 終盤は止める（[15 章](15-endgame.md)）。
- **出典**: [MySQL 8.0 リファレンス: The Slow Query Log](https://dev.mysql.com/doc/refman/8.0/en/slow-query-log.html)。`long_query_time = 0` の副作用の説明（ディスク・オーバーヘッド）は、この文書に書かれておらず出典未確認。

### MySQL のスロークエリログをローテートする

- **効く条件**: 前のベンチのクエリが混ざり、今の走行だけを集計できない。
- **やり方**: ファイルを退避してから、ログを開き直させる。
  ```sh
  sudo mv /var/log/mysql/mysql-slow.log /var/tmp/slow-$(date +%H%M%S).log
  mysql -e 'FLUSH SLOW LOGS'          # または mysqladmin flush-logs slow
  ```
- **確かめ方**: 新しいファイルが作られ、ベンチ中に大きくなるか見る。
- **危険**: `FLUSH SLOW LOGS` の手順は、MySQL の slow log のページには書かれていない。MySQL の「Server Log Maintenance」で確かめる。本章は確認していない。ベンチ中にローテートしない。
- **出典**: 出典未確認（手元の道具は  の `rotate`）。

### pt-query-digest で集計する

- **効く条件**: slow log が数十万行で、手で読めない。
- **やり方**:
  ```sh
  pt-query-digest /var/log/mysql/mysql-slow.log > digest.txt
  pt-query-digest --order-by Query_time:sum --limit 20 slow.log
  pt-query-digest --since '2026-10-31 10:30:00' --until '2026-10-31 10:32:00' slow.log
  pt-query-digest --filter '$event->{arg} =~ m/^select/i' slow.log
  ```
  - 既定は、リテラルを取り除いた形（フィンガープリント）で束ね、合計時間の大きい順に並べる。
  - `--order-by` は `属性:集計`（既定 `Query_time:sum`。集計は `sum`・`min`・`max`・`cnt`）。
  - `--limit` の既定は `95%:20`。
  - `--since` は厳密（新しい行が見つかるまで読み飛ばす）。`--until` は厳密ではない。
  - 入力は slow log のほか、general log、binlog、tcpdump の出力、`--processlist` も読める。
- **確かめ方**: 出力の最初の `Profile` 表で、上位の「Response time」の割合と「Calls」を見る。`V/M`（分散と平均の比）が大きい文は、遅い日と速い日がある。個別の `Query_time distribution` で、桁ごとの分布を見る。`Rows examined` が `Rows sent` よりずっと大きい文は、インデックスが足りない候補。
- **危険**: 合計時間の上位が、DB の待ちか、回数の多さ（N+1）かは分けて読む。`Calls` が大きく 1 回が速い文は、SQL を直すより、アプリで束ねる（[06 章](06-sql-and-indexes.md)）。
- **出典**: [Percona Toolkit: pt-query-digest](https://docs.percona.com/percona-toolkit/pt-query-digest.html)。ISUCON12 予選の講評は、slow log と pt-query-digest で `visit_history` の集約クエリを特定し、カバリングインデックスで DB の CPU が下がったと書く（[講評](https://isucon.net/archives/56850281.html)）。

### MySQL の performance_schema と sys スキーマで集計する

- **効く条件**: slow log を出せない（再起動できない・ディスクが足りない）。ベンチ中の文の統計を、ログなしで見たい。
- **やり方**: sys スキーマの `statement_analysis` は、正規化した文ごとの実行回数・総時間・走査行数・一時テーブルをまとめる。既定は総時間の降順。
  ```sql
  SELECT query, db, exec_count, total_latency, rows_examined_avg
  FROM sys.statement_analysis LIMIT 10;
  SELECT query, total_latency FROM sys.x$statement_analysis ORDER BY total_latency DESC LIMIT 5;
  ```
  - ほかに、`statements_with_full_table_scans`（全表走査）、`statements_with_temp_tables`（一時テーブル）、`innodb_lock_waits`（今のロック待ち）、`schema_unused_indexes`（使われていないインデックス）、`schema_redundant_indexes`（重複するインデックス）がある。
- **確かめ方**: ベンチの前後で値の差を取る（累積値なので、ベンチごとの値は差で出す）。
- **危険**: 累積値なので、MySQL の再起動で消える。`statement_analysis` 以外のビューの説明と、performance_schema が既定で有効という点は、取得した文書に無く出典未確認。performance_schema を有効にするとメモリを使う（出典未確認）。
- **出典**: [MySQL 8.0: sys.statement_analysis](https://dev.mysql.com/doc/refman/8.0/en/sys-statement-analysis.html)。

### PostgreSQL の pg_stat_statements

- **効く条件**: PostgreSQL の DB の CPU が高い。どの文が合計時間を食うか分からない。
- **やり方**: `postgresql.conf` に書いて再起動し、DB ごとに拡張を作る。
  ```ini
  shared_preload_libraries = 'pg_stat_statements'
  compute_query_id = on
  pg_stat_statements.max   = 10000
  pg_stat_statements.track = all
  ```
  ```sql
  CREATE EXTENSION pg_stat_statements;
  SELECT query, calls, total_exec_time, mean_exec_time, rows
         100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0) AS hit_percent
  FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
  SELECT pg_stat_statements_reset;
  ```
  - 定数は `$1` に置き換わった形で束ねられる。
  - `pg_stat_statements.max` の既定は 5000。超えると、実行の少ない順に捨てる。
  - `pg_stat_statements.track` の既定は `top`（関数の中の文は数えない）。`all` で入れ子も数える。
  - `track_planning` は既定で無効。有効にするとオーバーヘッドが出る（同じ形の文を多数の接続が流すとき）。
  - `pg_stat_statements_info` の `dealloc` が増えたら、追い出しが起きている。
- **確かめ方**: ベンチの前に `pg_stat_statements_reset`、後に上の SELECT。リセットしない場合は、前後の累積値の差を取る。
- **危険**:
  - 拡張を入れるには**再起動が要る**（`shared_preload_libraries`）。競技サーバーの DB の再起動は、反映の手順に入れる。
  - 共有メモリを `pg_stat_statements.max` に比例して使う。
  - `queryid` は PostgreSQL のメジャーバージョンやアーキテクチャをまたいで保証されない。
  - スタンバイを分けた構成では、台ごとに別に取る（読みはスタンバイで処理されるため）。
- **出典**: [PostgreSQL: pg_stat_statements](https://www.postgresql.org/docs/current/pgstatstatements.html)。

### PostgreSQL の log_min_duration_statement とサンプリング

- **効く条件**: `pg_stat_statements` の集計ではなく、遅い文 1 回ずつのログが欲しい。文のパラメータの値で遅さが変わる。
- **やり方**:
  ```ini
  log_min_duration_statement = 250ms     # これ以上は必ず出す。0 は全部、-1 は無効
  log_min_duration_sample    = 100ms     # これ以上の文から一部だけ出す
  log_statement_sample_rate  = 0.1       # 100ms 以上のうち 10%
  log_line_prefix = '%m [%p] %q%u@%d/%a '
  log_lock_waits  = on                   # deadlock_timeout を超えた待ちを出す
  log_temp_files  = 0                    # 一時ファイル（work_mem 不足）を全部出す
  ```
  - 単位を付けないとミリ秒。
  - 拡張問い合わせプロトコル（多くのドライバ）では、Parse・Bind・Execute の時間が別々に出る。`log_statement` は Execute の時点で、Bind のパラメータ値も出す。
  - `logging_collector` は再起動が要る。
- **確かめ方**: ログに `duration: 312.4 ms  execute ...` が出るか見る。`log_lock_waits` はロック待ちがあれば `still waiting for ... lock` が出る。
- **危険**: `0` は全部の文を書く。サンプリングの閾値を使うと、閾値以上の文は必ず出る（サンプルされない）。パラメータ値はログに出る。個人情報に注意する。
- **出典**: [PostgreSQL: Error Reporting and Logging](https://www.postgresql.org/docs/current/runtime-config-logging.html)。

### PostgreSQL の auto_explain

- **効く条件**: 遅い文の実行計画を、手で `EXPLAIN` せずに残したい。計画が変わる文（パラメータ次第）の遅い回だけ見たい。
- **やり方**:
  ```ini
  session_preload_libraries = 'auto_explain'
  auto_explain.log_min_duration = '300ms'
  auto_explain.log_analyze = on
  auto_explain.log_timing  = off       # オーバーヘッドを下げる。行数は残る
  auto_explain.log_buffers = on
  auto_explain.sample_rate = 0.1
  auto_explain.log_format  = json
  ```
  1 つの接続で試すだけなら `LOAD 'auto_explain'; SET auto_explain.log_min_duration = 0; SET auto_explain.log_analyze = true;`。
- **確かめ方**: ログに計画（`Seq Scan`・`Index Scan` と実行行数）が出るか見る。
- **危険**: `log_analyze` は、遅くない文も含めて**全部の文を計測する**。公式の文書は「性能への影響が極めて大きくなりうる」と警告する。`log_timing = off` で下がる。時計の読み取りが遅い環境もある。ベンチの点を測るときは切る。
- **出典**: [PostgreSQL: auto_explain](https://www.postgresql.org/docs/current/auto-explain.html)。

### EXPLAIN でインデックスが使われているか確かめる

- **効く条件**: スロークエリの上位の文が、インデックスを作っても速くならない。
- **やり方**: ログで見つけた文に `EXPLAIN`（MySQL は `EXPLAIN ANALYZE` も、PostgreSQL は `EXPLAIN (ANALYZE, BUFFERS)`）をかける。MySQL では `SHOW INDEX FROM <表>` でインデックスの一覧を見る。
- **確かめ方**: 走査行数・使ったインデックス・ファイルソート・一時テーブルを見る。
- **危険**: `EXPLAIN ANALYZE` は文を実際に実行する。UPDATE・DELETE にかけるときはトランザクションに入れて ROLLBACK する。競技サーバーで書き込みの文を試さない（手元で試す）。
- **出典**: [ISUCON11 本選の講評](https://isucon.net/archives/56163308.html)（EXPLAIN ANALYZE と SHOW INDEX の紹介）。詳しくは [06 章](06-sql-and-indexes.md)。

---

## 3. プロファイラ

### Go: net/http/pprof で CPU とヒープを採る

- **効く条件**: アプリの CPU が高い。DB やネットワークの待ちではなく、アプリの中で時間を使っている。
- **やり方**: ブランク import すると、`DefaultServeMux` にハンドラが付く。専用のポートで待ち受ける。
  ```go
  import _ "net/http/pprof"
  // main の中:
  go func { log.Println(http.ListenAndServe("localhost:6060", nil)) }
  ```
  ```sh
  go tool pprof "http://localhost:6060/debug/pprof/profile?seconds=30"   # CPU。既定 30 秒
  go tool pprof http://localhost:6060/debug/pprof/heap
  curl -o trace.out "http://localhost:6060/debug/pprof/trace?seconds=5" && go tool trace trace.out
  ```
  - エンドポイントは `profile`・`heap`・`goroutine`・`allocs`・`threadcreate`・`block`・`mutex`・`trace`。
  - `DefaultServeMux` を使っていないアプリは、自分の mux にハンドラを登録する。
- **確かめ方**: **ベンチ中の 30 秒**に採る（ベンチの山に合わせる）。`top`・`list <関数>`・`web` で見る。上位の関数が自分のコードか、ライブラリ（JSON・暗号・正規表現・GC）かを見る。
- **危険**:
  - 公開のポートで待ち受けない（`localhost` に限る）。手元に取るときは SSH のポート転送か `scp`。
  - 子関数と親関数の時間を取り違えない。自己時間（flat）と累積時間（cum）を区別する。ISUCON7 本選のチームは、`Big.Int.String` の先の `Itoa` が重いと読み違え、無意味な最適化をした。
  - プロセスが残っていると、古いプロセスのプロファイルを見る。取得元ポートを listen している PID と、サービスの MainPID を照らす。
- **出典**: [Go: net/http/pprof](https://pkg.go.dev/net/http/pprof)。

### Go: block と mutex のプロファイル

- **効く条件**: CPU は空いているのに遅い。ロック待ち・チャネル待ちが疑わしい。
- **やり方**: block と mutex は、コードで有効にしないと空。
  ```go
  runtime.SetBlockProfileRate(1)          // 1 は全イベント。重いので間引くなら大きい値
  runtime.SetMutexProfileFraction(5)      // 5 回に 1 回
  ```
  ```sh
  go tool pprof http://localhost:6060/debug/pprof/block
  go tool pprof http://localhost:6060/debug/pprof/mutex
  ```
- **確かめ方**: 待ち時間の合計が大きいロックの呼び出し元を見る。
- **危険**: 間引き率の値の選び方（1 や 5 の目安）は、取得した文書にない。小さい値ほど重い。ベンチの点を測るときは切る。
- **出典**: [Go: net/http/pprof](https://pkg.go.dev/net/http/pprof)（`SetBlockProfileRate`・`SetMutexProfileFraction` が必要という記述）。値の目安は出典未確認。

### Go: fgprof で待ちも含む時間（wall-clock）を見る

- **効く条件**: pprof の CPU プロファイルでは小さいのに、リクエストが遅い。I/O 待ち（DB・外部 API・ディスク）の中身を知りたい。
- **やり方**: `fgprof.Handler` を `/debug/fgprof` に登録して、`go tool pprof` で採る。約 99Hz で全 goroutine のスタックを取り、動いているものも待っているものも数える。
  ```go
  http.DefaultServeMux.Handle("/debug/fgprof", fgprof.Handler)
  ```
  ```sh
  go tool pprof "http://localhost:6060/debug/fgprof?seconds=10"
  curl "http://localhost:6060/debug/fgprof?seconds=10&format=folded" > folded.txt   # FlameGraph 用
  ```
- **確かめ方**: CPU のプロファイルと並べて、DB 呼び出しの関数が大きく出るか見る。
- **危険**: goroutine が増えるとオーバーヘッドが増える（README によると 1,000 未満なら通常は無視できる、10,000 以上では目立つ）。Go 1.19 以降を使う。古い版は goroutine が多いと停止時間が長くなる。
- **出典**: [felixge/fgprof README](https://github.com/felixge/fgprof)。実例: 自動収集のプロファイルを fgprof にした。

### Rust: perf でプロセスをサンプリングする

- **効く条件**: Rust のアプリの CPU が高い。言語内蔵のプロファイラは無いので、perf を使う。
- **やり方**:
  ```sh
  sudo perf record -F 99 -p $(pidof -s <アプリ>) -g -- sleep 30
  sudo perf report --stdio -n | head -80
  sudo perf record -F 99 -p <PID> --call-graph dwarf -- sleep 10   # frame pointer が無いとき
  ```
  - `-F 99` は 99Hz のサンプリング。
  - スタックを取るには、フレームポインタ付きでビルドするのが望ましい（出典の主張は「常にフレームポインタ付きでコンパイルせよ」）。無ければ `--call-graph dwarf`。
  - Cargo.toml の release にデバッグ情報を足すと、関数名と行が出る。
    ```toml
    [profile.release]
    debug = true
    ```
  - 権限: `kernel.perf_event_paranoid` が -1 だと制限なし、0 でシステム全体の計測も可、1 以上は自プロセスのみ。`CAP_PERFMON` を持てば制限を越えられる（Linux 5.9 以降は `CAP_SYS_PTRACE` なしで足りる）。
- **危険**:
  - `perf_event_paranoid` を下げるのは、競技サーバーの設定変更。セキュリティ上の影響は小さい競技環境でも、変更は反映の手順に入れる。
  - LBR（`--call-graph lbr`）は、クラウドでは無効なことが多い。
  - デバッグ情報を付けた release バイナリはサイズが大きい。デプロイの時間とディスクを見る。計測用と最終用で、ビルドの設定を分けない（点が変わるため）。
  - 長い `perf record`（数分）は、ファイルが大きくなる。
- **出典**: [Brendan Gregg: perf Examples](https://www.brendangregg.com/perf.html)、[Linux kernel: Perf events and tool security](https://www.kernel.org/doc/html/latest/admin-guide/perf-security.html)、[cargo flamegraph README](https://github.com/flamegraph-rs/flamegraph)（`debug = true`）。`RUSTFLAGS="-C force-frame-pointers=yes"` でフレームポインタを付ける書き方は出典未確認。

### Rust: cargo flamegraph（手元で再現した負荷を見る）

- **効く条件**: 手元（ローカルの再現環境）でアプリの CPU の中身を見たい。
- **やり方**:
  ```sh
  cargo install flamegraph
  cargo flamegraph --bin <bin名>           # release が既定。出力は flamegraph.svg
  cargo flamegraph -F 499 --bin <bin名>    # サンプリング頻度（既定 997Hz）
  cargo flamegraph --root --bin <bin名>    # perf の権限が無いとき
  ```
  - 権限は `echo -1 | sudo tee /proc/sys/kernel/perf_event_paranoid` か `--root`。
  - `--no-inline` はインラインの計算を省くが、README によると遅くなりうる。
- **確かめ方**: SVG をブラウザで開き、幅が広い関数を見る。
- **危険**: `cargo flamegraph` は `cargo run` を実行する。競技サーバー上のサービス（systemd が起動しているアプリ）には直接使えない。サービスには perf を直接使う（前項）。手元の負荷は本番のベンチと違うので、手元で見つけた上位が本番でも上位とは限らない。
- **出典**: [flamegraph-rs/flamegraph README](https://github.com/flamegraph-rs/flamegraph)。

### Rust: pprof-rs をアプリに埋め込む

- **効く条件**: perf が使えない（権限・コンテナ）。ベンチの山の時間だけ、アプリの中から取りたい。
- **やり方**: `ProfilerGuard` を作っている間だけ、SIGPROF でサンプルする。
  ```rust
  let guard = pprof::ProfilerGuardBuilder::default
      .frequency(1000)
      .blocklist(&["libc", "libgcc", "pthread", "vdso"])
      .build.unwrap;
  // ... 負荷の間待つ ...
  let report = guard.report.build.unwrap;
  report.flamegraph(std::fs::File::create("flamegraph.svg").unwrap).unwrap;   // feature "flamegraph"
  ```
  - `protobuf-codec`（または `prost-codec`）で pprof 形式に出せる。`pprof -svg profile.pb` で描く。
  - `frame-pointer` の feature は nightly 限定。標準ライブラリのフレームポインタが正しくないため `-Z build-std` が要る。
- **確かめ方**: SVG の幅の広い関数。HTTP で取得するエンドポイントを自分で用意する。
- **危険**: libgcc のアンワインダはシグナルハンドラの中で安全ではない。README は blocklist で緩和している。シグナルのハンドラは、ロックを取れないとそのサンプルを諦める。止めたあと元の SIGPROF ハンドラを戻さない（README の TODO）。ベンチの点を測る版には入れない（feature で切る。[15 章](15-endgame.md)）。
- **出典**: [tikv/pprof-rs README](https://github.com/tikv/pprof-rs)。

### Rust: samply と tokio-console（補助）

- **効く条件**: samply は、フレームグラフの代わりに Firefox Profiler で時間軸を見たい。tokio-console は、async のタスクが待ちっぱなし・自分で起こし続けるなど、非同期特有の問題が疑わしい。
- **やり方**:
  ```sh
  samply record ./my-application args           # Linux は perf_event_paranoid が要る。終わるとブラウザで開く
  # tokio-console: console-subscriber を足し、RUSTFLAGS="--cfg tokio_unstable" でビルド
  #   console_subscriber::init;   → tokio-console を実行（既定 127.0.0.1:6669）
  ```
  - samply は、release に `debug = true` を足した `profiling` プロファイルを勧める。`perf_event_paranoid` を 1 にするか -1 にし、`mmap failed` なら `perf_event_mlock_kb` を上げる。
  - tokio-console は、self-wake（自分を起こす）、lost waker、never yielded、auto-boxed future・large future の警告を出す。
- **確かめ方**: samply はブラウザの呼び出しツリー。tokio-console は タスク一覧と詳細。
- **危険**: tokio-console は `tokio_unstable` の cfg が要る。README にオーバーヘッドの記述は無い。**ベンチの点に影響するので、計測用のビルドだけで使い、点を測る版に入れない**。
- **出典**: [mstange/samply](https://github.com/mstange/samply)、[tokio-rs/console](https://github.com/tokio-rs/console)。

### perf top でシステム全体の CPU を見る

- **効く条件**: アプリ・DB・nginx のどれが CPU を使っているか、プロセスをまたいで知りたい。カーネル（システムコール・ネットワーク）の割合が大きいか知りたい。
- **やり方**:
  ```sh
  sudo perf top -F 49                  # 今の CPU 消費者を関数単位で
  sudo perf record -F 99 -a -g -- sleep 10 && sudo perf report --stdio
  ```
- **確かめ方**: カーネルの関数（`_raw_spin_lock`、`tcp_*`、`copy_user`）が上位なら、アプリではなく OS・ネットワークの設定を見る（[02 章](02-os-kernel.md)）。
- **危険**: 重い `perf record`（長時間・全 CPU）は競技サーバーで避ける。短く（10 秒）。HOLD 中は触らない（チームの運用）。
- **出典**: [Brendan Gregg: perf Examples](https://www.brendangregg.com/perf.html)。

### フレームグラフの読み方

- **効く条件**: 上位の関数が複数あり、どれを直すべきか分からない。
- **やり方**:
  - 幅 = その関数（とその子）がサンプルに含まれた割合。高さ = 呼び出しの深さ。
  - 自己時間（その関数だけ）と累積時間（子を含む）を区別する。
  - 上位の関数が、自分のコードの子として呼ばれているか、ライブラリの内部かを見る。
  - 2 つのプロファイルを比べるときは、サンプル数（負荷）を揃える。
- **確かめ方**: 直した後に、同じ関数の幅が減ったかを、同条件のプロファイルで比べる。
- **危険**: 累積の大きい親関数を直そうとして、実は子が重い。逆も起きる。DB 待ちが大きいアプリでは CPU のフレームグラフに現れない。DB が軽くなったら、調査を CPU のプロファイルに移す。

### 走行ごとにプロファイルを保存して比べる

- **効く条件**: 変更の前後でプロファイルを比べたい。ベンチごとに結果がばらつく。
- **やり方**: ベンチ 1 本ごとに、日時・commit・走行 ID の付いたディレクトリへ保存する。pprof の `.pb.gz`・perf の `perf.data` または折りたたみ形式・alp の出力・pgss の差分を同じディレクトリに入れる。
- **確かめ方**: 同じ関数の割合を、走行をまたいで比べる。
- **危険**: 累積型の指標（pgss・pprof の一部）は、リセットされない限り前の走行の値を含む。負荷の条件が違う走行（人数が違う）は区別して比べる。
- **出典**: 実例 毎回保存して過去のフレームグラフと比較、初期化フックからベンチごとの計測を開始、ベンチ結果をグラフの注釈へ結びつける。

---

## 4. トレース（OpenTelemetry）

### トレースを入れる価値と、採取率

- **効く条件**: 1 つのリクエストの中で、DB・外部 API・待ち（接続プール・同時数の上限）のどこに時間が使われたか、ログと pprof では割れない。
- **やり方**:
  - 入口のスパンに、DB 呼び出し・接続プールの待ち・重い処理の枠待ちを子のスパンとして付ける。
  - 採取率は最初から絞る。環境変数の設定:
    ```sh
    OTEL_TRACES_SAMPLER=parentbased_traceidratio
    OTEL_TRACES_SAMPLER_ARG=0.1        # 10%。0〜1
    OTEL_TRACES_EXPORTER=none          # 出力先を付けない（切るとき）
    ```
  - サンプラーは `always_on`・`always_off`・`traceidratio`・`parentbased_*`。
  - ヘッドサンプリングは、トレースの始まりに決める（割合で一貫して残す）。テイルサンプリングは、トレースがそろってから決める（遅い・エラーのトレースだけ残せるが、運用が重い）。

### トレースの収集サーバーが先に落ちる

- **効く条件**: トレースを増やしたら、分析する前に収集サーバー（Tempo・Jaeger など）が落ちた。
- **やり方**: 採取率を下げる。属性の数・保持期間を絞る。収集サーバーを競技サーバーと別の台に置く。
- **確かめ方**: 収集サーバーの CPU・メモリ・ディスクを、ベンチ中に見る。
- **危険**: 外部の収集サービスに送るときは、競技ルール（外部資源の利用は計測・開発の目的なら可、スコアを上げる効果は不可）に合うか確かめる。計測用サーバーを競技サーバーと同じ VPC に置いたまま終わると、環境確認で失格になった可能性がある（ISUCON14 の参加者の推測。[15 章](15-endgame.md)）。
- **出典**: 実例 ISUCON14、トレース量で収集サーバーが落ちた、手元のプロファイラへ閉じた経路でデータを送る、生ログではなく集計 JSON を収集する。

---

## 5. OS の計測

### 最初の 1 分: top・vmstat・mpstat・pidstat

- **効く条件**: どの層（nginx・アプリ・DB）の CPU が張り付くか、CPU 以外（I/O・メモリ・ロック）が詰まっていないかを、まず知りたい。
- **やり方**:
  ```sh
  top                        # %CPU の上位プロセス。1 キーで CPU ごとに分ける
  vmstat 1                   # r（実行待ち）、b（I/O 待ち）、si/so（swap）、bi/bo、cs、us/sy/id/wa/st
  mpstat -P ALL 1            # CPU ごと。1 コアだけ 100% を見つける
  pidstat -u 1               # プロセスごとの CPU（%usr・%system・%wait）
  pidstat -w 1               # コンテキストスイッチ（cswch/s 自発、nvcswch/s 非自発）
  pidstat -d 1               # プロセスごとのディスク I/O
  ```
  - `vmstat` の最初の行は、起動からの平均。2 行目以降が 1 秒ごと。
  - `mpstat` は、平均では見えない「1 コアだけ張り付く」を見る。単一スレッドの処理（Go の 1 つの goroutine・Rust の 1 つのタスク・PostgreSQL の 1 バックエンド）で起きる。
  - `pidstat -t` はスレッドごと。`-p PID`・`-C <名前>` で絞る。
  - `htop` は top の対話版（出典未確認）。
- **確かめ方**:
  - `us` が高い → アプリ・DB の計算。`sy` が高い → システムコール・ネットワーク・ロック。`wa` が高い → ディスク待ち。`st` が高い → VM を他の VM に取られている。
  - `r` が CPU 数を大きく超える → 実行待ちが溜まっている。

### ディスク: iostat

- **効く条件**: `vmstat` の `wa` が高い。DB のコミットやログの書き込みが遅いと疑う。
- **やり方**:
  ```sh
  iostat -xz 1        # 拡張統計。動きの無いデバイスは出さない
  iostat -xzm 1       # MB/s で表示
  ```
  - `r_await`・`w_await`: 要求が待ち行列に入ってから終わるまでの平均 ms（待ちを含む）。
  - `aqu-sz`: デバイスへ出された要求の平均の待ち行列の長さ。
  - `%util`: デバイスが要求を処理していた時間の割合。
- **確かめ方**: `w_await` が数十 ms を超えたら、書き込みの待ち。`aqu-sz` が大きいと、要求が溜まっている。
- **危険**: **`%util` が 100% に近くても、並列に処理するデバイス（RAID・SSD・EBS）では、限界とは限らない**（man の注意）。`await` と `aqu-sz` を見る。
- **出典**: [iostat(1)](https://manpages.debian.org/bookworm/sysstat/iostat.1.en.html)。

### sar で CPU・メモリ・ネットワーク・TCP を時系列で見る

- **効く条件**: ベンチの最中に見逃した。過去のベンチの時間帯を後から見たい。
- **やり方**:
  ```sh
  sar -u 1             # CPU（%user %system %iowait %steal %idle）
  sar -q 1             # 実行待ち行列と load average
  sar -r 1             # メモリ
  sar -n DEV 1         # NIC ごとの rx/tx pck/s・kB/s・%ifutil
  sar -n TCP,ETCP 1    # TCP の接続数と再送（retrans/s）・エラー
  sar -n SOCK 1        # ソケット数（tcp-tw が TIME_WAIT）
  sar -d 1             # ブロックデバイス
  sar -u -f /var/log/sysstat/saDD -s 10:30:00 -e 10:35:00   # 保存済みの履歴を読む
  ```
  - 履歴は `sadc` が `saDD` に保存する。`-f` で読み、`-s`・`-e` で時刻を絞る（既定は 08:00〜18:00）。
  - `-n TCP` は `sadc` を `-S SNMP` 付きで収集していないと出ない。`-d` は `-S DISK` または `-S XDISK`。
- **確かめ方**: `retrans/s` が増える → 通信の詰まり。`%ifutil` が 100% に近い → 帯域の上限。`tcp-tw` が数万 → TIME_WAIT の溜まり（[02 章](02-os-kernel.md)）。
- **危険**: sysstat のタイマーが動いていないと、履歴は空。競技の開始時に有効にする。sysstat の収集間隔は、既定では 10 分で、ベンチの山（数秒〜数十秒）は見えない（出典未確認）。ベンチ中は 1 秒間隔で、自分で採って保存する。
- **出典**: [sar(1)](https://manpages.debian.org/bookworm/sysstat/sar.1.en.html)。

### ソケットの状態: ss（netstat）

- **効く条件**: 499・502・接続エラーが出る。TIME_WAIT・CLOSE_WAIT が多いと疑う。ポート・FD の枯渇が疑わしい。
- **やり方**:
  ```sh
  ss -s                              # 状態ごとの数の要約（速い）
  ss -tnp                            # TCP、数値、プロセス付き
  ss -t state time-wait | wc -l
  ss -t state established '( sport = :80 )'
  ss -ti                             # 輻輳制御・RTT・RTO・cwnd など TCP の内部情報
  ss -tlnp                           # listen 中のソケット（既定では隠れる）
  ```
  - `netstat` は `ss` の古い代替（出典未確認）。
- **確かめ方**: TIME_WAIT が数万 → [02 章](02-os-kernel.md)・[03 章](03-nginx.md)の keepalive。CLOSE_WAIT が増え続ける → アプリが接続を閉じていない。
- **危険**: 特になし。ただし大量のソケットがあるホストで `ss`（要約でない形）を連打すると遅い。`-s` を使う。
- **出典**: [ss(8)](https://manpages.debian.org/bookworm/iproute2/ss.8.en.html)。

### ネットワークの帯域とプロセス別: iftop・nethogs・dstat

- **効く条件**: 台どうしの通信量が多い。帯域に上限のある環境で、誰が帯域を使っているか知りたい。
- **やり方**:
  ```sh
  sudo iftop -i eth0 -nNP           # ホスト対ごとの帯域（直近 2・10・40 秒）。-t でテキスト出力
  sudo nethogs eth0                 # プロセスごとの帯域
  ```
  - iftop の `-n` は名前解決しない、`-N` はポート名にしない、`-P` はポートを出す、`-f` で絞る。
  - nethogs は、プロセスに結び付かない通信を「unknown TCP」に出す。root が要る。
  - `dstat`（後継は `dool`）は CPU・ディスク・ネットワークを 1 行に並べる（出典未確認）。
- **危険**: iftop・nethogs はパケットを捕まえるので CPU を使う。ベンチ中の常時実行は避ける。平文の通信の中身（認証情報）が見えうる。出力を外へ出さない。
- **出典**: [iftop(8)](https://manpages.debian.org/bookworm/iftop/iftop.8.en.html)、[raboof/nethogs](https://github.com/raboof/nethogs)。

### netdata など、常時の監視を入れる

- **効く条件**: ベンチのたびに全台で `top` を開くのが追いつかない。全台の CPU・メモリ・ネットワークを 1 画面で見たい。
- **やり方**: netdata は各台にエージェントを入れ、1 秒ごとに集計する。ダッシュボードは `http://<台>:19999`。他の選択肢は Grafana + Prometheus の exporter など。実例では、全台の負荷を共通のモニターに表示して、局所的な CPU の飽和を見つけた。
- **確かめ方**: ベンチ中に、全台の CPU を同じ時間軸に並べる。
- **危険**:
  - **計測ツールが CPU を使う。netdata の README は、既定で約 5% の CPU と 150MiB のメモリ、機械学習とアラートを切って揮発性の保存にすると 1% 未満と書く。**台のスペックが小さい（2 vCPU）競技サーバーでは、無視できない。
  - ダッシュボードのポートを公開しない。競技中に外部のサービスへ送るときは、ルールを確かめる。
- **出典**: [netdata/netdata](https://github.com/netdata/netdata)、全台の負荷を共通モニターに表示。

---

## 6. DB 内部の計測

### MySQL: SHOW ENGINE INNODB STATUS

- **効く条件**: ロック待ち・デッドロックが疑わしい。バッファプールが足りているか知りたい。
- **やり方**: `SHOW ENGINE INNODB STATUS\G`。セクションごとに見る。
  | セクション | 見るもの |
  | --- | --- |
  | `SEMAPHORES` | 待っているスレッドが多いと、I/O か内部の競合 |
  | `LATEST DETECTED DEADLOCK` | 直近のデッドロックの両方の文・保持と待ちのロック・ロールバックされた側 |
  | `TRANSACTIONS` | `LOCK WAIT`、`TRX HAS BEEN WAITING n SEC FOR THIS LOCK TO BE GRANTED`、`History list length` |
  | `BUFFER POOL AND MEMORY` | `Buffer pool hit rate`（1000/1000 が全部メモリから） |
  | `LOG` | LSN・フラッシュ・チェックポイントの位置 |
  | `ROW OPERATIONS` | 行操作の数と速度 |
- **確かめ方**: ロック待ちの秒数と、待たれているテーブル・インデックス・ロックのモード。`History list length` が増え続けるなら、長いトランザクションが purge を止めている。
- **危険**: 出力は 1MB で切れる。直近 1 回のデッドロックだけが出る。デッドロックが連続するなら、ログに出す設定（`innodb_print_all_deadlocks`）を使う（この設定は取得した文書に無く出典未確認）。
- **出典**: [MySQL 8.0: InnoDB Standard Monitor](https://dev.mysql.com/doc/refman/8.0/en/innodb-standard-monitor.html)。

### PostgreSQL: pg_stat_activity と pg_locks

- **効く条件**: DB の接続が詰まる。長いトランザクション・ロック待ちが疑わしい。`idle in transaction` が溜まっている。
- **やり方**:
  ```sql
  SELECT pid, usename, state, now - xact_start AS xact_age
         wait_event_type, wait_event, left(query, 80) AS query
  FROM pg_stat_activity
  WHERE state <> 'idle' AND pid <> pg_backend_pid
  ORDER BY xact_age DESC NULLS LAST;

  SELECT pid, pg_blocking_pids(pid) AS blocked_by, wait_event_type, wait_event, query
  FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;
  ```
  - `pg_locks` は `granted = false` が待っている行。ブロックしている側は `pg_blocking_pids(pid)` で求める（自己結合より推奨）。
- **確かめ方**:
  - `state = 'idle in transaction'` で `state_change` が古い → ロックを持ったまま止まっている（アプリがトランザクションを閉じていない）。
  - 同じ `wait_event_type = 'Lock'` が並ぶ → ブロックの連鎖。`'LWLock'` → 内部の競合。
- **危険**: `pg_locks` を頻繁に問い合わせない（ロックマネージャを短く止める）。行ロックは `pg_locks` にほとんど出ない（待っている側は、持ち主のトランザクション ID を待つ形で出る）。他人のセッションの文を見るには、スーパーユーザーか `pg_read_all_stats` が要る。
- **出典**: [PostgreSQL: Monitoring Database Activity](https://www.postgresql.org/docs/current/monitoring-stats.html)、[PostgreSQL: pg_locks](https://www.postgresql.org/docs/current/view-pg-locks.html)。

### PostgreSQL: 表とインデックスの統計

- **効く条件**: インデックスが使われているか、全表走査が多いか、vacuum が追いついているか知りたい。
- **やり方**:
  ```sql
  SELECT relname, seq_scan, seq_tup_read, idx_scan, n_live_tup
  FROM pg_stat_user_tables WHERE seq_scan > 0 ORDER BY seq_tup_read DESC LIMIT 10;
  SELECT relname, n_live_tup, n_dead_tup, last_autovacuum FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 10;
  SELECT datname, round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) AS hit_pct
         deadlocks, temp_files FROM pg_stat_database WHERE datname IS NOT NULL;
  SELECT schemaname, relname, indexrelname, idx_scan FROM pg_stat_user_indexes WHERE idx_scan = 0;
  ```
- **確かめ方**: 大きい表で `seq_scan` と `seq_tup_read` が大きく `idx_scan` が少ない → インデックスの候補（ただし全表走査が正しい計画のこともある）。`hit_pct` が低い → `shared_buffers` かワーキングセットの問題（OS のキャッシュは数えない）。`deadlocks` が増える → ロックの順序。`temp_files` が多い → `work_mem` 不足。
- **危険**:
  - 統計の値は約 1 秒遅れて反映される。トランザクションの中では同じ値が見える（`pg_stat_clear_snapshot` で更新）。
  - **使われていないインデックス（`idx_scan = 0`）を消す前に**、統計の期間（`stats_reset`）が十分か、スタンバイが使っていないか、主キー・一意制約のインデックスでないかを確かめる。
  - `track_io_timing` は既定で無効。有効にすると `*_time` の列が出るが、時計を読むオーバーヘッドがある（オーバーヘッドの大きさは出典未確認）。
- **出典**: [PostgreSQL: Monitoring Database Activity](https://www.postgresql.org/docs/current/monitoring-stats.html)。

---

## 7. 計測ツール自体のオーバーヘッド

### 計測ツールの重さ一覧（入れる前に知る）

- **効く条件**: 計測を入れたら点が下がった。計測を入れた版と切った版のどちらが本当の性能か分からない。
- **やり方**: 次の表で、重いものを特定する。同じ commit で、計測あり・なしを 2 本ずつ流して比べる（[14 章](14-benchmarker-and-scoring.md)・`tools/noise`）。
  | ツール | 重さ | 出典 |
  | --- | --- | --- |
  | nginx のアクセスログ | 毎リクエストの書き込み。`buffer=`・`if=`・`off` で下げる | nginx 文書 |
  | MySQL の `long_query_time = 0` | 全文を書く。ログが急に大きくなる（副作用の説明は出典未確認） | MySQL 文書 |
  | PostgreSQL の `auto_explain.log_analyze` | 全部の文を計測。「性能への影響が極めて大きくなりうる」。`log_timing = off` で下がる | PostgreSQL 文書 |
  | PostgreSQL の `pg_stat_statements.track_planning` | 同じ文を多数の接続が流すと目立つ | PostgreSQL 文書 |
  | PostgreSQL の `log_transaction_sample_rate` | 「相当なオーバーヘッド」と明記 | PostgreSQL 文書 |
  | Go の fgprof | goroutine が 1 万を超えると目立つ。Go 1.19 未満は停止時間が長い | fgprof README |
  | netdata | 既定で約 5% の CPU・150MiB。ML とアラートを切ると 1% 未満 | netdata README |
  | tokio-console | README にオーバーヘッドの記述なし（要計測） | tokio-console README |
  | `perf record`（全 CPU・長時間） | ファイルが大きい。CPU を使う | 出典未確認 |
- **確かめ方**: 計測あり・なしを同じ commit で 2 本ずつ（交互に）流して、差がぶれの幅（±10〜15%）を超えるか見る。
- **危険**: 計測が重いと、ボトルネックの姿が変わる。計測を終盤に一括で切れるようにしておく（[15 章](15-endgame.md)）。計測を切った後も、1 本流して確かめる。

---

## 8. ベンチ 1 本ごとの回収

### ベンチ前にログを退避して空にし、ベンチ後に集計する

- **効く条件**: 前のベンチのログが混ざる。集計を手でやるたびに抜けや間違いが出る。複数人が同じ台で計測して取り合う。
  1. ベンチ前（`rotate`）: nginx のアクセスログと DB のスロークエリログを、サーバー上の別の場所へ退避して空にする。PostgreSQL は `pg_stat_statements` の開始値を保存する。
  2. ベンチ。
  3. ベンチ後（`collect`）: ログを手元へ集める（gzip で運ぶ）。alp と pt-query-digest（PostgreSQL は pgbadger）の結果と、pgss の差分の上位を、日時付きのディレクトリに置く。
  ```sh
  # nginx の例（rotate の中身）
  sudo mv /var/log/nginx/access.log /var/tmp/isu/access-$(date +%H%M%S).log
  sudo nginx -s reopen                  # USR1 を送ってログを開き直す
  ```
  - nginx のログローテーションは、ファイルを改名して、マスタープロセスに `USR1`（`nginx -s reopen`）を送る。マスターがログを開き直し、ワーカーも開き直す。古いファイルはすぐ後処理（圧縮など）できる。
  - 初期化のフックから計測を始める方法（ISUCON13 のチームは、pprotein を初期化の末尾から起動した。実例）、ログの増分からベンチの終了を検出する方法（ISUCON14）、ベンチ前後のビルド・再起動・ログ退避を Ansible にまとめる方法（ISUCON14）がある。
- **確かめ方**: 退避後の新しいログが空から始まる。集計に前のベンチの時刻が入らない。
- **危険**:
  - **ベンチ中に rotate しない**（チームの運用では HOLD。ベンチ中は競技サーバーを触らない）。
  - 計測の道具を片付ける前に、ログと実装の状態を保存する。ISUCON14 の参加者は、計測用のインスタンスをバックアップなしで消し、点数とメトリクスを失った。
  - 無通信とベンチの終了を混同しない。途中で失敗した走行のログも保存する。ログのローテーションを扱う。
  - **初期化の API は、ベンチの度に呼ばれる**。初期化のフックに重い計測の開始を入れると、初期化の制限時間（[14 章](14-benchmarker-and-scoring.md)）に響く。
  - 失格が出た走行のログは、間引かずに保存する。
- **出典**: [nginx: Controlling nginx](https://nginx.org/en/docs/control.html)。手元の道具 （`rotate`・`collect`）。

### 集約ツール（pprotein など）で、走行ごとの計測を 1 か所に集める

- **効く条件**: 複数人が同じ結果を見たい。台が複数で、ログとプロファイルを手で集めるのが面倒。
- **やり方**: pprotein は、pprof・fgprof・alp・slp（スロークエリ）を集めて表示する Go 向けの道具（リポジトリの説明）。ISUCON14 の参加記（kawaemon）は alp・slp・pprotein を必須ツールに挙げ、ISUCON13 のとーふとふは pprotein を初期化の末尾から起動した。フレームグラフを毎回生成して共有、共通のモニターも同じ目的。
- **確かめ方**: ベンチの後に、台ごとの結果が 1 画面で比べられる。
- **危険**:
  - 計測用のサーバーを、競技サーバーと同じ VPC・同じアカウントに置いたまま終わらない。ISUCON14 の参加者は、環境確認で失格になった原因を、pprotein を入れたサーバーを同じ VPC に立てっぱなしにしたことだと推測している（運営の説明ではない）。競技の最後に、計測用のものを止めて片付ける（[15 章](15-endgame.md)）。
  - 競技サーバーから外部の収集サーバーへ送るデータには、出題内容・認証情報が含まれうる。競技中に出題内容を外部へ公開しない。
  - 導入に時間を使いすぎない。導入して分析に使わなかった例もある。
- **出典**: [kaz/pprotein](https://github.com/kaz/pprotein)（リポジトリの説明のみ確認。設定方法は未確認）、[kawaemon の ISUCON14 参加記](https://zenn.dev/kawaemon/articles/f40529e0c447f5)、[ponyo877 の ISUCON14 参加記](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)。

---

## 9. 山の窓に揃えた集計

### 平均ではなく、負荷の山の時間だけを切り出す

- **効く条件**: 走行全体の平均では CPU が空いているのに、点が伸びない。ベンチの終盤の数十秒だけ遅い。ベンチの負荷が時間とともに増える。
- **やり方**: 見る時間帯（窓）を先に決めて、窓の中だけを集計する。`tools/bench-window` は、窓の中の次の 3 つを出す。
  1. 秒単位のタイムライン（台 × 秒の件数・p50・p99・5xx・499。CPU も並べられる）。
  2. エンドポイント別の件数・合計時間・p99。
  3. 窓の中と外の比較（同じエンドポイントの p99 が何倍か）。
  ```sh
  # ベンチの受付開始が 10:03:20。最後の 30 秒を見る
    --anchor 10:03:20 --from +30s --to +60s --group alp-groups.txt
  # 受付開始が分からないときは、ログの最後から
  # CPU も同じ秒に並べる
  ```
  - 入力は LTSV と JSON Lines の両方（1 行ごとに自動判別）。`.gz` も読める。サーバーには接続しない。標準ライブラリだけの Python で、`uv` で動く。
  - CPU は `mpstat`・`pidstat` の出力か、`ts,host,cpu` の CSV。
  - 窓の外は「窓に入らなかった行すべて」。比較の基準なので、走行の前後だけを集めたログを渡す。
- **確かめ方**: 読み方の例。負荷が 2.4 倍になっただけで、あるエンドポイントの p99 が 8.8 倍になっていれば、負荷に比例しない伸びなので、そこがボトルネック（`tools/bench-window/README.md` の出力例）。
- **危険**:
  - 窓の基準時刻の取り方は、アプリと回による。ベンチのポータルの開始時刻か、アクセスログの `POST /initialize` の時刻を使う案がある（後者は出典未確認）。ベンチの時計とサーバーの時計がずれていると、窓がずれる。台ごとの時刻をそろえる。
  - 窓の中が少件数だと p99 は不安定。`--min-count` で比べる最小の件数を決める。
- **出典**: `isucon-kit/tools/bench-window/README.md`。

### 台ごと・日ごとの内訳も見る

- **効く条件**: 台が複数。地域や日ごとに負荷の型が違う。
- **やり方**: 台の名前をつけて `bench-window` に渡す。ベンチの出力に、日ごと・地域ごとの人数が出るなら、それも並べる。
- **確かめ方**: ある台だけ張り付き、他の台が空いていれば、構成の問題（[11 章](11-multi-server.md)）。
- **危険**: 同じ走行内でも、地域ごと・日ごとにぶれる。1 本の数字で結論しない（`tools/noise`）。
- **出典**: `isucon-kit/tools/noise/README.md`。

---

## よくある失敗

- **計測を入れる作業が目的になる。** 導入して、分析に使わない。分析の担当を決める。
- **トレース・ログを重いまま点を測る。** OTel を切ると +10.5%。計測を入れた版の点で、改善の効果を評価しない。
- **子関数と親関数の時間を取り違える。** フレームグラフの自己時間と累積時間を区別する。
- **手動のアクセスで測った時間を、ベンチ中の時間と思い込む。** ISUCON9 予選の配送 API は、ベンチ中だけ約 0.8 秒の遅延が入る。手元の手動アクセスでは速く見えた（[ISUCON9 予選の講評](https://isucon.net/archives/53789931.html)）。
- **古いプロセスのプロファイルを見る。** プロファイルがリセットされないとき、旧プロセスが残っている。
- **ログが混ざる。** 前のベンチのログを消さずに集計する。rotate を忘れる。
- **MySQL を再起動して、`SET GLOBAL` の計測の設定が消える。** my.cnf に書く。
- **PostgreSQL の拡張を入れるのに再起動が要るのを忘れる。** `shared_preload_libraries` は再起動後に効く。
- **ベンチ中に重い読み取り（長い `perf record`・大きい表の全件 SELECT・`pg_dump`）を流す。** 点が下がり、計測も歪む。
- **計測用のサーバー・ツールを片付けずに終わる。** 環境確認や追試に響きうる（[15 章](15-endgame.md)）。
- **ログに認証情報・出題内容を出して、外へ出す。** キャプチャも同じ。コミットも外への持ち出しもしない。

## 出典一覧

ISUCON 公式の講評・マニュアル

- [ISUCON9 予選の講評](https://isucon.net/archives/53789931.html)
- [ISUCON10 予選の講評](https://isucon.net/archives/55025156.html)
- [ISUCON11 予選の講評](https://isucon.net/archives/56044867.html)
- [ISUCON11 本選の講評](https://isucon.net/archives/56163308.html)
- [ISUCON12 予選の講評](https://isucon.net/archives/56850281.html)
- [ISUCON13 の講評](https://isucon.net/archives/58001272.html)
- [ISUCON14 の講評](https://isucon.net/archives/58869617.html)

参加記

- [kawaemon の ISUCON14 参加記](https://zenn.dev/kawaemon/articles/f40529e0c447f5)
- [ponyo877 の ISUCON14 参加記](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)
- [toshikingggg/isucon-tool（ISUCON13 向けの道具）](https://github.com/toshikingggg/isucon-tool)

道具・ソフトウェアの公式文書

- [tkuchiki/alp](https://github.com/tkuchiki/alp)、[matsuu/kataribe](https://github.com/matsuu/kataribe)、[kaz/pprotein](https://github.com/kaz/pprotein)
- [nginx: ngx_http_log_module](https://nginx.org/en/docs/http/ngx_http_log_module.html)、[nginx: Controlling nginx](https://nginx.org/en/docs/control.html)
- [MySQL 8.0: The Slow Query Log](https://dev.mysql.com/doc/refman/8.0/en/slow-query-log.html)、[MySQL 8.0: InnoDB Standard Monitor](https://dev.mysql.com/doc/refman/8.0/en/innodb-standard-monitor.html)、[MySQL 8.0: sys.statement_analysis](https://dev.mysql.com/doc/refman/8.0/en/sys-statement-analysis.html)
- [Percona Toolkit: pt-query-digest](https://docs.percona.com/percona-toolkit/pt-query-digest.html)
- [PostgreSQL: pg_stat_statements](https://www.postgresql.org/docs/current/pgstatstatements.html)、[auto_explain](https://www.postgresql.org/docs/current/auto-explain.html)、[ログの設定](https://www.postgresql.org/docs/current/runtime-config-logging.html)、[Monitoring Database Activity](https://www.postgresql.org/docs/current/monitoring-stats.html)、[pg_locks](https://www.postgresql.org/docs/current/view-pg-locks.html)
- [Go: net/http/pprof](https://pkg.go.dev/net/http/pprof)、[felixge/fgprof](https://github.com/felixge/fgprof)
- [flamegraph-rs/flamegraph](https://github.com/flamegraph-rs/flamegraph)、[tikv/pprof-rs](https://github.com/tikv/pprof-rs)、[mstange/samply](https://github.com/mstange/samply)、[tokio-rs/console](https://github.com/tokio-rs/console)
- [Brendan Gregg: perf Examples](https://www.brendangregg.com/perf.html)、[Linux kernel: Perf events and tool security](https://www.kernel.org/doc/html/latest/admin-guide/perf-security.html)
- [OpenTelemetry: Sampling](https://opentelemetry.io/docs/concepts/sampling/)、[OpenTelemetry: SDK の環境変数](https://opentelemetry.io/docs/languages/sdk-configuration/general/)
- [vmstat(8)](https://manpages.debian.org/bookworm/procps/vmstat.8.en.html)、[mpstat(1)](https://manpages.debian.org/bookworm/sysstat/mpstat.1.en.html)、[pidstat(1)](https://manpages.debian.org/bookworm/sysstat/pidstat.1.en.html)、[iostat(1)](https://manpages.debian.org/bookworm/sysstat/iostat.1.en.html)、[sar(1)](https://manpages.debian.org/bookworm/sysstat/sar.1.en.html)、[ss(8)](https://manpages.debian.org/bookworm/iproute2/ss.8.en.html)、[iftop(8)](https://manpages.debian.org/bookworm/iftop/iftop.8.en.html)
- [raboof/nethogs](https://github.com/raboof/nethogs)、[netdata/netdata](https://github.com/netdata/netdata)

手元の資料

- `isucon-kit/tools/bench-window/README.md`、`isucon-kit/tools/noise/README.md`、## 出典未確認の項目（後で人間が確かめる）

- alp の `-m` に複数の正規表現をまとめて渡す書き方、`kataribe.toml` の設定項目。
- MySQL の `FLUSH SLOW LOGS` によるローテートの手順（slow log のページに無い）。
- `long_query_time = 0` のオーバーヘッドの大きさ、performance_schema の既定と費用、`statement_analysis` 以外の sys ビューの説明。
- `innodb_print_all_deadlocks`、`sysstat` の既定の収集間隔、`netstat`・`htop`・`dstat`・`dool` の位置づけ。
- Rust で `RUSTFLAGS="-C force-frame-pointers=yes"` を使う書き方。Go の block・mutex の間引き率の目安。
- アクセスログの `POST /initialize` の時刻を窓の基準にする案。`perf record` の長時間実行の負荷。
