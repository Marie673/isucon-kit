# 05 PostgreSQL

**競技のルールは毎回違う。当日のマニュアルが優先する。** この章の「危険」は、ルールが再起動後のデータ保持・追試・初期化の時間を求める前提で書いた。

## この章で扱う範囲

- PostgreSQLの「サーバー設定」「計測」「インデックスの種類」「パーティション」「複製（レプリケーション）と読み取り専用のスタンバイ」「一括の読み込み」「ロック」。
- 扱わないもの: SQL の書き方・インデックスの列順・N+1（`06-sql-and-indexes.md`）、計測の入れ方の詳細（[01 計測](01-measurement.md)）、MySQL（`04-mysql.md`）。
- 数字は出典の値を書いた。出典の無い主張は「出典未確認」と書いた。

## 最初に見るもの（当日の 5 分チェックリスト）

1. 版: `SELECT version;`（PostgreSQL の主版で文書が変わる）。
2. 計測が入っているか: `SHOW shared_preload_libraries;` に `pg_stat_statements` があるか。無ければ **再起動が要る**（反映の予定に入れる）。`SELECT * FROM pg_stat_statements LIMIT 1;` が通るか。
3. メモリ・接続: `SHOW shared_buffers; SHOW work_mem; SHOW effective_cache_size; SHOW max_connections;`、`free -m`。アプリの台数 × プロセス数 × プール上限との掛け算。
4. 永続化の現状: `SHOW fsync; SHOW synchronous_commit; SHOW full_page_writes; SHOW wal_level; SHOW synchronous_standby_names;`
5. 待ちの正体: `SELECT state, wait_event_type, wait_event, count(*) FROM pg_stat_activity GROUP BY 1,2,3 ORDER BY 4 DESC;`（Lock・IO・Client・IPC のどれが多いか）。
6. 全件走査: `SELECT relname, seq_scan, seq_tup_read, idx_scan FROM pg_stat_user_tables ORDER BY seq_tup_read DESC LIMIT 10;`
7. スタンバイがあるなら: primary で `SELECT application_name, state, sync_state, write_lag, flush_lag, replay_lag FROM pg_stat_replication;`
8. **再起動試験の方法をマニュアルで確かめる**（OS の再起動か、サービスの再起動か）。永続化に触る前に必ず。

---

## 1. 設定の反映のしかた（reload と再起動）

### reload で効くものと再起動が要るもの

- **効く条件**: 設定を変えたのに効かない。設定の反映でベンチ中に DB が止まった。
- **やり方**:
  - 再起動が要る（サーバー起動時のみ）と文書が示すもの: `shared_buffers`、`max_connections`、`wal_buffers`、`wal_level`、`shared_preload_libraries`（`pg_stat_statements` の追加）、`pg_stat_statements.max`、`listen_addresses`、`max_wal_senders`・`max_replication_slots`。
  - reload（`SELECT pg_reload_conf;` か `systemctl reload`）で効く: `work_mem`、`maintenance_work_mem`、`effective_cache_size`、`random_page_cost`、`jit`、`max_wal_size`、`checkpoint_timeout`、`wal_compression`、`synchronous_commit`、`bgwriter_*`、`log_*`、`idle_in_transaction_session_timeout`、`synchronous_standby_names`。
  - 設定は `conf.d/*.conf` の drop-in に分けると、変更の単位と戻し方が明確になる。後ろのファイルが勝つので、**同じキーを 2 か所に書かない**。`ALTER SYSTEM` の書く `postgresql.auto.conf` が最後に勝つので、上書きされていないかも見る。
- **危険**:
  - **値が不正だと起動しない**。
  - **スタンバイの `max_connections` は primary と同じかそれ以上**にする。そうでないとスタンバイで問い合わせができない（文書）。primary を上げるときは、先にスタンバイを上げて再起動する。
  - 再起動すると接続が切れる。アプリが再接続できるか確かめる（`04-mysql.md` の節 7 と）。
  - https://www.postgresql.org/docs/16/runtime-config-connection.html
  - https://www.postgresql.org/docs/16/pgstatstatements.html
  - （段階 0 の再起動、ISU-343 の不変条件）

---

## 2. メモリ

### shared_buffers・effective_cache_size

- **効く条件**: 読み込みでディスクに行く（`EXPLAIN (ANALYZE, BUFFERS)` の `shared read` が多い、`pg_stat_database` の `blks_read` が `blks_hit` に比べて多い。この 2 列は出典未確認）。計画がインデックスを避ける（`effective_cache_size` が小さい）。
- **やり方**:
  - `shared_buffers`: 既定 128MB。文書の出発点は「専用サーバーでメモリの 25%」。「40% を超えて良くなる可能性は低い」。大きくしたら `max_wal_size` も増やす。**再起動が要る**。
  - `effective_cache_size`: 既定 4GB。**メモリを確保しない**。OS のキャッシュを含めた「使えるキャッシュの大きさ」の見積もりで、大きいとインデックススキャンが選ばれやすくなる。
- **確かめ方**: `EXPLAIN (ANALYZE, BUFFERS)` の `shared hit` / `read`。pg_stat_statements の `shared_blks_hit` / `shared_blks_read`（ヒット率 = hit ÷ (hit + read)）。
- **危険**: 大きくしすぎると同居するプロセス（アプリ・nginx）と OS のキャッシュが足りなくなる。再起動でキャッシュが冷える（`pg_prewarm` は出典未確認）。特になし（永続化には関係しない）。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-resource.html、https://www.postgresql.org/docs/16/runtime-config-query.html

### work_mem・hash_mem_multiplier・maintenance_work_mem

- **効く条件**: `EXPLAIN (ANALYZE)` に `Sort Method: external merge  Disk: ...kB` や `Batches: 2`（ハッシュがディスクに溢れる）が出る。`log_temp_files = 0` で一時ファイルがログに出る。
- **やり方**:
  - `work_mem`: 既定 4MB。**1 つの並べ替え・ハッシュ操作ごとに**この量まで使える。複雑な問い合わせは複数の操作を同時に行い、複数のセッションが同時に走るので、**合計は work_mem の何倍にもなる**（文書）。
  - `hash_mem_multiplier`: 既定 2.0。ハッシュ操作は `work_mem × この値` まで使える。`work_mem` を 40MB 以上に上げた環境で 2.0〜8.0 以上が有効なことがある（文書）。
  - `maintenance_work_mem`: 既定 64MB。`VACUUM`・`CREATE INDEX`・外部キーの追加に使う。`work_mem` よりずっと大きくしてよい（文書）。autovacuum では最大 `autovacuum_max_workers` 倍まで確保されうる。
- **確かめ方**: `EXPLAIN (ANALYZE, BUFFERS)` の `Sort Method`・`Batches`、`log_temp_files`。インデックス作成の時間（`maintenance_work_mem`）。
- **危険**: `work_mem` を接続数ぶん掛けて見積もる。大きくして OOM で PostgreSQL が落ちると再起動になる。特になし（永続化には関係しない）。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-resource.html、https://www.postgresql.org/docs/16/runtime-config-logging.html（`log_temp_files`）

### random_page_cost・effective_io_concurrency（SSD）

- **効く条件**: SSD（EBS gp3 など）なのに、インデックスがあっても計画が `Seq Scan` を選ぶ。
- **確かめ方**: `EXPLAIN` の計画が変わるか。変わったあとの実行時間。
- **危険**: 下げすぎるとインデックススキャンを選びすぎて遅くなる計画が出る。reload で戻せる。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-query.html、https://www.postgresql.org/docs/16/runtime-config-resource.html

---

## 3. 永続化に関わる設定（危険が大きい。最初に読む）

- ISUCON13 では、再起動後のデータチェックの不整合で fail したチームが 4、再起動後のスコアが 75% 以下で 1、再起動後スコア 0 で 51 チームだった（公式の結果発表）。ISUCON14 では「データ保持違反」で失格になったチームの記録がある。出典: https://isucon.net/archives/57993937.html、https://zenn.dev/kawaemon/articles/f40529e0c447f5
- 失格の判定は**再起動の方法とデータの確かめ方で決まる**。当日のマニュアルで確かめ、**その方法で自分たちで試して**から採用する。

### synchronous_commit

- **効く条件**: コミットの待ちが長い（`pg_stat_activity` の `wait_event` が `WALSync`・`SyncRep`、書き込み API の p50 がコミットの下限に張り付く）。
- **やり方**: 値は `on`（既定）・`off`・`local`・`remote_write`・`remote_apply`。**トランザクション単位で変えられる**（`SET LOCAL synchronous_commit = off;`）。
  - `off`: コミットが WAL のディスクへの書き込みを待たない（非同期コミット）。障害時に失う可能性のある窓は **`wal_writer_delay`（既定 200ms）の最大 3 倍**。文書は「データベースの不整合を起こさない」と明記する（直近のコミットが無かったことになるだけ。`fsync = off` とは違う）。
  - `local`: ローカルの WAL のディスクだけ待つ。同期レプリケーションを使わないなら `on` と同じ。
  - `remote_apply`: スタンバイが適用して読めるようになるまで待つ。コミットの遅延が大きい。
- **確かめ方**: 書き込み API の時間、`pg_stat_activity` の `wait_event`、`pg_stat_replication` の遅れ。
- **危険**:
  - `off` は **電源断・OS のクラッシュ・`kill -9`・immediate shutdown で直近のコミットが消える**（文書: immediate shutdown はクラッシュと同じ）。ベンチが「ベンチ中に書いたデータが再起動後に残っていること」を確かめるなら、直近の分の欠けとして見つかりうる。**正常停止（fast shutdown）なら残るかは文書に明記が無い**（出典未確認。実機で試す）。
  - 文書は「クライアントが外部の動作（現金の払い出しなど）をコミットの耐久性を前提にして行う用途には使わない」と書く。**課金・予約など、外部に副作用のあるコミットには使わない**。
  - 同期レプリケーションを使う構成では、`local` や `off` にすると、**スタンバイから読むとき書いた直後の内容が見えない**。
  - `SET LOCAL` は**トランザクション全体**に効く。同じトランザクションの他の変更も安全な場合だけ使う。
  - https://www.postgresql.org/docs/16/runtime-config-wal.html
  - https://www.postgresql.org/docs/16/wal-async-commit.html
  - https://www.postgresql.org/docs/16/server-shutdown.html

### fsync・full_page_writes

- **効く条件**: 書き込みが WAL の fsync・ページの全体書き込みで詰まる（ほかの手でも解けない場合の最後の手とされるが、**手元の方針としては使わない**）。
- **やり方**: どちらも `postgresql.conf` だけ（reload で効く。`fsync` は起動時か reload）。文書は `fsync = off` を、バックアップからの初回ロード、捨てる前提のバッチ処理、フェイルオーバーに使わない読み取り専用のクローンに限って許す。`full_page_writes = off` も同じ条件。
- **確かめ方**: （使わない）。
- **危険**: **`fsync = off` は電源断・システムクラッシュで回復不能なデータ破壊を起こしうる**。`full_page_writes = off` は「回復不能または**気づかない**データ破壊」を起こしうる（文書）。高品質のハードウェアだけでは正当化にならない（文書）。**ISUCON の再起動の試験・追試が強制停止を含むなら失格に直結する**。使わない。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-wal.html

### wal_level

- **効く条件**: 一括の読み込み（`COPY`・`CREATE INDEX`・`TRUNCATE`）の WAL の量が多い。
- **やり方**: 既定は `replica`（アーカイブ・ストリーミング複製・スタンバイの読み取りに十分）。`minimal` にすると、同じトランザクションで作った・書き換えたテーブルの WAL を省く。`logical` は WAL が増える。**再起動が要る**。
- **確かめ方**: `pg_wal` の増え方、`pg_current_wal_lsn` の差分。
- **危険**: **`minimal` では `max_wal_senders` が 0 でないと起動しない**、アーカイブ・PITR・スタンバイが使えなくなる。ストリーミング複製（スタンバイ）を使う構成では使えない。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-wal.html、https://www.postgresql.org/docs/16/populate.html

---

## 4. チェックポイントと WAL

### checkpoint_timeout・max_wal_size・checkpoint_completion_target

- **効く条件**: 書き込みが多く、WAL の量で起きるチェックポイント（`pg_stat_bgwriter` の `checkpoints_req` が `checkpoints_timed` に比べて多い）が頻繁。ログに「checkpoints are occurring too frequently」が出る。チェックポイント直後に書き込みが遅くなる（全ページ書き込みで WAL が増える）。
- **危険**: **値を大きくするとクラッシュ回復に時間がかかる**（文書）。`pg_wal` のディスクの空きを使う。再起動の試験のあとの起動が遅くならないか確かめる。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-wal.html、https://www.postgresql.org/docs/16/monitoring-stats.html、・`95-wal-bgwriter.conf`

### wal_buffers・bgwriter

- **効く条件**: 同時に多くのコミットがあり、WAL バッファが満杯になる。接続のプロセスが自分でダーティページを書いている。
- **確かめ方**: `pg_stat_wal`・`pg_stat_bgwriter` の差分（走行の前後）。
- **危険**: 共有メモリが増える。特になし（永続化には関係しない）。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-wal.html

### wal_compression

- **効く条件**: WAL の量が多い。特にチェックポイント直後の最初の更新でページ全体（FPI）が WAL に載り、スタンバイへの送信量・適用量が大きい。
- **やり方**: `wal_compression = lz4`（reload で効く。`pglz`・`zstd` もある。lz4 は pglz より CPU が軽い。lz4 はビルド時に対応していること）。
- **確かめ方**: WAL の生成量（`pg_current_wal_lsn` の差分）、スタンバイの適用の遅れ、CPU。
- **危険**: 文書は「WAL の量を減らし、**破損の危険は増えない**。ログと回復時の CPU が増える」と書く。値が不正だと起動しない。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-wal.html

---

## 5. 接続とプール

### max_connections とアプリ側のプール

- **効く条件**: `FATAL: sorry, too many clients already`。DB が空いているのにアプリのリクエストが待たされる（プールの取得待ち）。`pg_stat_activity` に `idle` の接続が大量にある。
- **やり方**:
  - `max_connections` の既定は通常 100（**再起動が要る**）。PostgreSQL は接続ごとに 1 プロセスを持つ（出典未確認）ので、CPU が少ない台では多すぎると逆効果になりうる（根拠の数字は文書に無い。出典未確認）。
  - Go: `database/sql` の `SetMaxOpenConns`・`SetMaxIdleConns`（既定 2）・`SetConnMaxLifetime`（`04-mysql.md` の節 7）。
- **危険**: **上限を上げて逆に遅くなる**。DB 側の CPU・ロックがボトルネックなら詰まりが増えるだけ。**スタンバイの `max_connections` は primary 以上**。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-connection.html、https://docs.rs/sqlx/0.8.6/sqlx/pool/struct.PoolOptions.html、https://go.dev/doc/database/manage-connections

### PgBouncer

- **効く条件**: 接続の数が多すぎて PostgreSQL のプロセスが重い、アプリの台数が多く接続を集約したい。
- **やり方**: `pool_mode` は `session`（既定）・`transaction`・`statement`。`transaction` では、トランザクションが終わるとサーバー接続がプールに戻る。`default_pool_size` の既定 20、`max_client_conn` の既定 100、`server_idle_timeout` の既定 600 秒。
  - **transaction モードではセッション状態（`SET`）に頼れない**（トランザクションごとに別のサーバー接続になる）。`max_prepared_statements`（既定 200）が 0 でなければ、プロトコルレベルの名前付き prepared statement は動く。`PREPARE`・`EXECUTE` の SQL 文は追跡されない。
- **確かめ方**: PgBouncer の統計、`pg_stat_activity` の接続数。アプリのエラー（`prepared statement "..." does not exist` など）。
- **出典**: https://www.pgbouncer.org/config.html

### タイムアウトの設定（idle_in_transaction_session_timeout・lock_timeout・statement_timeout）

- **効く条件**: 開きっぱなしのトランザクションが行ロックを握り続け、接続とロックを占有する（`pg_stat_activity` に `idle in transaction` が長く残る）。
- **確かめ方**: `SHOW idle_in_transaction_session_timeout;`、`pg_stat_activity` の `state = 'idle in transaction'`。
- **危険**: **`lock_timeout`・`statement_timeout` は入れなかった**。進行中の正常な要求をエラーにして、5xx を返す経路を作るため。タイムアウトによる成功件数の減少と FAIL の危険がある。60 秒 idle の接続が実際にあれば、その要求は失敗する。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-logging.html（`log_lock_waits`）

---

## 6. プランと計測の設定

### prepared statement の generic plan と custom plan（plan_cache_mode）

- **効く条件**: 同じ SQL でも引数によって実行時間が極端に違う（偏った値で `Seq Scan` や不適切な結合順が選ばれる）。`EXPLAIN EXECUTE` で `$1` の入った計画が出ている。ORM・sqlx（既定でプリペアドステートメントを使う）で、最初の数回は速いのに後で遅くなる。
- **やり方**: prepared statement は、**最初の 5 回は引数の値を使った custom plan** で、その推定コストの平均を取る。その後、引数に依存しない generic plan を作ってコストを比べ、**generic plan のコストが平均より「大きく高くない」なら generic plan を使い続ける**（`plan_cache_mode = auto`）。`force_custom_plan` / `force_generic_plan` で上書きできる（reload で効く。`SET plan_cache_mode = ...` でセッション単位でも）。
  - 確かめ: `PREPARE` した文を `EXPLAIN EXECUTE name(...)` する。generic plan は `$1`・`$2` が計画に出る。custom plan は値が埋まる。
- **確かめ方**: `auto_explain`（次項）で遅い文の実際の計画、`pg_stat_statements` の `mean_exec_time` と `max_exec_time` の開き（文書にある列）。
- **危険**: **文書は `force_*` を「generic plan のコスト見積もりが大きく外れる場合に」使うものとする**。`force_custom_plan` は毎回計画するので CPU（2 vCPU の台では重い）が増える。`force_generic_plan` は偏った値で遅くなる。DDL・統計の更新があると再計画される。
- **出典**: https://www.postgresql.org/docs/16/sql-prepare.html、https://www.postgresql.org/docs/16/runtime-config-query.html、https://docs.rs/sqlx/latest/sqlx/postgres/struct.PgConnectOptions.html

### JIT

- **効く条件**: `EXPLAIN (ANALYZE)` に `JIT:` の節があり、`Timing: Generation ... Total` が実行時間に比べて大きい。短い問い合わせが多い（OLTP）のに、計画の時間・CPU が高い。
  ```ini
  jit = off
  ```
- **確かめ方**: `EXPLAIN (ANALYZE)` の `JIT:` の節の `Timing: Total`。ベンチの CPU。
- **危険**: 特になし（分析的な重いクエリが主の問題なら逆効果。実測で決める）。
- **出典**: https://www.postgresql.org/docs/16/jit-decision.html、https://www.postgresql.org/docs/16/runtime-config-query.html

### 並列クエリ（max_parallel_workers_per_gather）

- **効く条件**: CPU が少ない台（2 vCPU）で、短い問い合わせに並列ワーカーの起動のコストが載る。`EXPLAIN` に `Gather` がある。
- **確かめ方**: `EXPLAIN` の `Gather`、ベンチの CPU。
- **危険**: 重い集計が少数ある問題では、無効にすると遅くなる。実測で決める。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-resource.html

### pg_stat_statements と auto_explain

- **効く条件**: どの SQL が時間を使っているか分からない。ログを集める前に、累計で上位を見たい。
- **やり方**:
  - `pg_stat_statements`: `shared_preload_libraries = 'pg_stat_statements'` を設定して**再起動**し、`CREATE EXTENSION pg_stat_statements;`（使う DB で 1 回。スタンバイには複製で届く）。上位の取り方:
    ```sql
    SELECT query, calls, total_exec_time, mean_exec_time, rows
           shared_blks_hit, shared_blks_read
           round(100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0), 2) AS hit_percent
    FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
    ```
    `blk_read_time` を見るには `track_io_timing = on`。`pg_stat_statements.max` の既定は 5000（再起動）。`pg_stat_statements_reset` で全部リセット。
  - `auto_explain`: `LOAD 'auto_explain'`（セッション）か `session_preload_libraries`・`shared_preload_libraries`。`auto_explain.log_min_duration`（既定 -1 = 無効。`'250ms'` など）、`auto_explain.sample_rate`。
- **確かめ方**: 走行の前後で `pg_stat_statements` の累計の差を取る。
- **危険**:
  - **`auto_explain.log_analyze = on` は、実行する全ての文に対してノードごとの時間を測るので、性能に「極めて悪い影響」を与えうる**（文書）。使うなら `log_timing = off` か `sample_rate` を小さくする。
  - `pg_stat_statements` は共有メモリを追加で使う。再起動が要るので、**最初の反映の時点で入れておく**。
  - ログの量: `log_min_duration_statement` も全ての文のログと同様に大きな負荷になりうる（文書）。ログに機密・出題内容が入る。外に出さない。
- **出典**: https://www.postgresql.org/docs/16/pgstatstatements.html、https://www.postgresql.org/docs/16/auto-explain.html、https://www.postgresql.org/docs/16/runtime-config-logging.html

### EXPLAIN (ANALYZE, BUFFERS) の読み方

- **効く条件**: 1 つの SQL が遅い理由を知りたい。
- **やり方**:
  ```sql
  BEGIN READ ONLY;
  SET LOCAL statement_timeout = '3s';
  EXPLAIN (ANALYZE, BUFFERS) SELECT ...;
  ROLLBACK;
  ```
  読み方: ① **推定行数（`rows`）と実際の行数（`actual rows`）の差**。大きな差は統計の古さ・不足（`ANALYZE`）。② `loops` を掛けて合計を出す（`actual time` と `rows` は 1 回あたりの平均）。③ `Buffers: shared hit=... read=...`。`read` が多ければディスクから読んでいる。④ `Seq Scan` / `Index Scan` / `Bitmap Heap Scan` / `Index Only Scan`（`Heap Fetches` が多いと可視性マップが効いていない）。⑤ `Rows Removed by Filter` が多ければ、フィルタで捨てている（インデックスで絞れていない）。
- **確かめ方**: 変更の前後で `Execution Time`、`Buffers`。**2 回目の実行は暖まった値**。cold と warm を区別する。
- **危険**: **`EXPLAIN ANALYZE` は文を実行する**。`INSERT`・`UPDATE`・`DELETE` は `BEGIN` … `ROLLBACK` で囲む（文書）。タイミングの測定自体にも負荷がある。小さなテーブルの計画を大きなテーブルに当てはめない。
- **出典**: https://www.postgresql.org/docs/16/using-explain.html

### log_min_duration_statement・log_lock_waits・log_checkpoints

- **効く条件**: ピンポイントで遅い文・ロック待ち・チェックポイント・autovacuum の影響を、ログから時刻つきで追いたい。
- **やり方**: `log_min_duration_statement`（既定 -1 = 無効。ミリ秒）、`log_lock_waits = on`（`deadlock_timeout` を超えた待ちを記録）、`log_checkpoints`（既定 on）、`log_autovacuum_min_duration`、`log_temp_files`（既定 -1）。`log_line_prefix = '%m [%p] '` で文と時間を結びつける。
- **確かめ方**: PostgreSQL のログ（`pgbadger` で集計。使い方は[01 計測](01-measurement.md)）。
- **危険**: **文のログはどれも大きな負荷になりうる**。ロギングの収集がログを失わない設計なので、極端な負荷ではログの書き込みでプロセスが待たされることがある（文書）。ログには機密が入りうる。計測が終わったら戻す。
- **出典**: https://www.postgresql.org/docs/16/runtime-config-logging.html

---

## 7. autovacuum・可視性マップ・index-only scan

### autovacuum と visibility map

- **効く条件**: `EXPLAIN (ANALYZE)` の `Index Only Scan` に `Heap Fetches` が多い。`pg_stat_user_tables` の `n_dead_tup` が多い・`last_autovacuum` が古い。大量の `UPDATE`・`DELETE`・初期化のあとで、計画が悪い（統計が古い）。
- **やり方**:
  - `UPDATE`・`DELETE` は古い行バージョンを残す。`VACUUM` が回収し、**可視性マップ**（全トランザクションに見える行だけのページの印）を更新する。index-only scan は可視性マップの印が立っているページだけヒープを読まずに済む（文書）。
  - autovacuum の発動: `autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × 行数`（既定は 50 + 0.2 × 行数。既定値は文書のこのページに無く、一般の既定。出典未確認）。表ごとに `ALTER TABLE ... SET (autovacuum_vacuum_scale_factor = 0.01)` で変えられる。
  - 初期化で大量に書き換えたあとは、手動の `VACUUM (ANALYZE)` を明示するか（文書は一括読み込みのあとの `ANALYZE` を勧める。autovacuum を当てにしない）。
- **確かめ方**: `SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables;`、`EXPLAIN (ANALYZE)` の `Heap Fetches`。
- **危険**: `VACUUM FULL` は `ACCESS EXCLUSIVE` ロックを取り、追加のディスクが要る（文書）。ベンチ中に使わない。`log_autovacuum_min_duration = 0` で走行中の autovacuum の影響を追える。
- **出典**: https://www.postgresql.org/docs/16/routine-vacuuming.html、https://www.postgresql.org/docs/16/indexes-index-only-scans.html、https://www.postgresql.org/docs/16/populate.html

---

## 8. インデックスの種類

### INCLUDE つきのカバリングインデックス（index-only scan）

- **効く条件**: 検索で必要な列がごく少数で、`Index Scan` のあとにヒープの読み込みが多い。`EXPLAIN` で `Index Scan` のコストの大半がヒープの読み込み。
- **やり方**: `CREATE INDEX tab_x_y ON tab(x) INCLUDE (y);`。`y` は検索のキーでなく、インデックスに載るだけ（型にインデックスが対応していなくてもよい）。B-tree・GiST・SP-GiST が `INCLUDE` に対応（B-tree・GiST・SP-GiST のみ。文書）。UNIQUE の一意性はキーの列だけに掛かる。式は `INCLUDE` に使えない。
- **確かめ方**: `EXPLAIN (ANALYZE, BUFFERS)` に `Index Only Scan`、`Heap Fetches` が小さい。
- **危険**: **文書は「幅の広い列を足すのは控えめに」「テーブルの変更が遅く、index-only scan がヒープを読まずに済むときでなければ意味が薄い」と書く**。書き込みが重くなり、インデックスが大きくなる。スタンバイでは可視性マップの更新が競合しうる（`VACUUM` が全可視にする操作は index-only scan と衝突しうる。文書）。
- **出典**: https://www.postgresql.org/docs/16/indexes-index-only-scans.html、https://www.postgresql.org/docs/16/hot-standby.html

### 部分インデックス（partial index）

- **効く条件**: 検索が「一部の行」（`WHERE status = 'pending'`、`deleted_at IS NULL`）に偏っていて、残りが大多数でインデックスを大きくしているだけ。部分集合にだけ一意制約を掛けたい。
- **やり方**: `CREATE INDEX ... ON t (col) WHERE predicate;`、`CREATE UNIQUE INDEX ... WHERE success;`。
- **確かめ方**: `EXPLAIN` で、その部分インデックスが使われている。`pg_relation_size`。
- **危険**: **クエリの WHERE がインデックスの述語を「含意する」と計画時に分かる場合だけ使われる**。汎用の定理証明は無い。**引数つきのクエリ（`x < $1`）は述語（`x < 2`）を含意しないので使われない**（文書）。同じ列の値ごとに多数の部分インデックスを作ると、計画のたびに全部を調べる（パーティションを使う）。
- **出典**: https://www.postgresql.org/docs/16/indexes-partial.html、（インデックスの節）

### 式インデックス

- **効く条件**: `WHERE lower(name) = $1`、`WHERE (data->>'k') = $1` のように、列に関数・演算をかけた条件。
- **やり方**: `CREATE INDEX ON t (lower(name));`、`CREATE INDEX ON t ((data->>'k'));`。**クエリの式がインデックスの式と同じ形でないと使われない**。式インデックスのキー列だけが必要でも、planner は元の列が要ると判断して index-only scan にならないことがある（`CREATE INDEX tab_f_x ON tab (f(x)) INCLUDE (x);` で回避。文書）。
- **確かめ方**: `EXPLAIN`。
- **危険**: 式の計算コストが書き込みごとに載る。式が `IMMUTABLE` な関数でないと作れない（出典未確認）。
- **出典**: https://www.postgresql.org/docs/16/indexes-index-only-scans.html、https://www.postgresql.org/docs/16/datatype-json.html

### BRIN

- **効く条件**: 非常に大きな追記専用に近い表で、列の値が物理的な位置と相関している（作成時刻・単調増加の ID）。範囲検索が主。B-tree が大きすぎてメモリに乗らない。
- **やり方**: `CREATE INDEX ON events USING brin (created_at) WITH (pages_per_range = 32, autosummarize = on);`。範囲ごとの要約（最小・最大）だけを持つので、B-tree よりずっと小さい。ビットマップスキャンで、候補の範囲を読んで再検査する（非可逆）。新しいページは、`VACUUM`・`autosummarize`（既定 off）・`brin_summarize_new_values` で要約するまで使われない。
- **確かめ方**: `SELECT attname, correlation FROM pg_stats WHERE tablename = '...';`（±1 に近いほど BRIN 向き）、インデックスの大きさ、`EXPLAIN` の `Bitmap Index Scan`。
- **危険**: 値が物理的にばらけていると、ほとんどの範囲を読んで効かない。点引きには B-tree。行を更新して物理順が崩れると効かなくなる。ISUCON の規模（数十万〜百万行）では B-tree で足りることが多い（出典未確認）。
- **出典**: https://www.postgresql.org/docs/16/brin-intro.html

### GIN と pg_trgm（部分一致・LIKE '%語%'・jsonb）

- **効く条件**: `LIKE '%語%'`・`ILIKE`・正規表現が遅い（`Seq Scan`）。jsonb の `@>`（包含）が遅い。
- **やり方**:
  - `CREATE EXTENSION pg_trgm; CREATE INDEX ON t USING gin (col gin_trgm_ops);`。**前方一致でない `LIKE '%foo%bar'` や正規表現にもインデックスが効く**（3 文字の組（トライグラム）を引く）。トライグラムが取り出せないパターンは「インデックス全体の走査」になる（文書）。GiST は距離検索（`<->`）の `ORDER BY ... LIMIT` に使える（GIN は不可）。
  - jsonb: `CREATE INDEX ON api USING GIN (jdoc jsonb_path_ops);`（`@>` だけ。小さく速い。キーの存在 `?` は引けない）、既定の `jsonb_ops` は `?`・`@>` ともに使える。サブドキュメントなら式インデックス。
  - **日本語（非 ASCII）: データベースのロケールが `C` だと、トライグラムが空になり効かない**（個人の検証ノートの記述。日本語での検証と 3 文字未満の検索語の挙動は出典未確認。実機の `SELECT show_trgm('日本語');` で確かめる）。
- **確かめ方**: `EXPLAIN` の `Bitmap Index Scan on ... gin`。検索結果が旧実装と一致するか。
- **危険**:
  - GIN の更新は遅い（出典未確認）。書き込みの多い列には慎重に。
  - 検索結果の同一性（大文字小文字・Unicode 正規化・LIKE のエスケープ）を、旧実装との突き合わせで全パターン確かめる。
- **出典**: https://www.postgresql.org/docs/16/pgtrgm.html、https://www.postgresql.org/docs/16/datatype-json.html、https://blackglory.me/notes/postgresql/PostgreSQL(v15)/Full_Text_Search_(FTS)/N-Gram_Index?variant=en（C ロケールの注意。個人のノート）

---

## 9. パーティション

- **効く条件**: 非常に大きな表で、古いデータの一括削除・期間ごとの検索が重い。
- **やり方**: `PARTITION BY RANGE (col)`（範囲）・`LIST`・`HASH`。**パーティションの絞り込み（pruning）は、WHERE にパーティションキーの条件があるときだけ働く**（インデックスではなく制約で絞る）。主キー・一意制約は**パーティションキーの列を含めなければならない**。`DETACH PARTITION` は `ACCESS EXCLUSIVE` ロックを取る（`CONCURRENTLY` なら弱いロック）。
- **確かめ方**: `EXPLAIN` の `Append` の下のノードの数と、`Subplans Removed`。
- **危険**:
  - **文書は「パーティションが多すぎると、計画の時間とメモリが増える」と警告する**（数千までは普通に扱える。条件で大半を絞れる前提）。
  - パーティションをまたぐ UNIQUE を表現できない。外部キーの制約にも影響する。
  - 初期化・再起動の試験・スタンバイとの整合（パーティションの `TRUNCATE` はスタンバイの読みと衝突する。節 12）。
- **出典**: https://www.postgresql.org/docs/16/ddl-partitioning.html

---

## 10. UNLOGGED テーブル（再起動で消える = 失格の危険）

- **効く条件**: 書き込みの WAL がボトルネックで、**再起動後に空でもよい**データ（キャッシュ・セッション・一時的な集計）。
- **やり方**: `CREATE UNLOGGED TABLE ...`。WAL に書かないので書き込みがずっと速い。**インデックスも UNLOGGED になる**。同時に作られるシーケンス（`serial`・`identity`）も UNLOGGED。
- **確かめ方**: 書き込みの時間、`pg_wal` の増え方。
- **出典**: https://www.postgresql.org/docs/16/sql-createtable.html

---

## 11. COPY・一括 INSERT・ON CONFLICT・initialize

- **効く条件**: `/initialize` が遅い・上限に近い。大量の INSERT の連続（CSV の入稿）がタイムアウトする。
- **やり方**（文書の一括読み込みの手引き）:
  1. **`COPY` を使う**（`INSERT` より負荷が少ない。1 つのコマンドなので自動コミットを切る必要が無い）。使えなければ `PREPARE` を 1 回して `EXECUTE` を繰り返す。
  2. 複数の `INSERT` は **1 つのトランザクションにまとめる**。
  3. **新しいテーブルに `COPY` してからインデックスを作る**。既存のテーブルは、インデックスを落として読み込み、作り直す（一意インデックスの重複検出を失う）。
  4. 外部キーは読み込みの間は外し、あとで追加する（一括で検査される）。
  5. `maintenance_work_mem` を上げる（`CREATE INDEX` と外部キーの追加が速くなる）。`max_wal_size` を上げる（強制のチェックポイントが減る）。
  6. **終わったら `ANALYZE`**（autovacuum を当てにしない）。
  7. `wal_level = minimal` にして、**同じトランザクションでテーブルを作る/`TRUNCATE` して `COPY` する**と WAL が省かれる（再起動が要る。複製・アーカイブを使う構成では不可）。
  - アプリの一括: `INSERT ... VALUES (...), (...)`、`INSERT ... ON CONFLICT (col) DO UPDATE SET x = EXCLUDED.x`（**原子的な UPSERT**。1 つの文が同じ行を 2 回変えるとエラー）、Rust の sqlx の `QueryBuilder::push_values`（**バインド変数の上限は PostgreSQL で 65535**。行数 = 65535 ÷ 列数 を超えない。空の iterator は不正な SQL）。
- **危険**:
  - 大きな WAL は複製の負荷になる。
  - `ON CONFLICT DO UPDATE` は、衝突しなくても対象の行をロックする（文書）。
  - 全保存先（DB・Redis・ファイル）を initialize で戻す。再起動後にも残ること。
- **出典**: https://www.postgresql.org/docs/16/populate.html、https://www.postgresql.org/docs/16/sql-insert.html、https://docs.rs/sqlx/0.8.6/sqlx/struct.QueryBuilder.html

---

## 12. レプリケーションと読み取り専用のスタンバイ

### ストリーミング複製・同期の設定（synchronous_standby_names・remote_apply）

- **効く条件**: 1 台の DB に読み取りが集中して CPU が飽和している。読み取りを別の台に逃がしたい。
- **やり方**:
  - primary: `max_wal_senders`・`max_replication_slots`・`wal_level = replica`（既定）・複製用ロール（`REPLICATION LOGIN`）と `pg_hba.conf` の `replication` 行。スタンバイ: `pg_basebackup`、`standby.signal`、`primary_conninfo`、`primary_slot_name`。
  - 同期: `synchronous_standby_names = 'ANY 2 (s1, s2, s3)'`（**quorum**: どれか 2 台の確認）または `'FIRST 2 (s1, s2, s3)'`（優先順位）。`synchronous_commit` の段階: `on`（スタンバイが WAL を耐久的に書いた）・`remote_write`・`remote_apply`（**スタンバイが適用して問い合わせで読める**）。`remote_apply` は「単純な場合に因果整合の負荷分散ができる」（文書）。
  - 遅れの見方: primary の `pg_stat_replication` の `write_lag`・`flush_lag`・`replay_lag`、`sent_lsn` と `replay_lsn` の差。
  - WAL の保持: 複製スロット（`pg_create_physical_replication_slot`）と `max_slot_wal_keep_size`（スロットが保持する WAL の上限。無いと `pg_wal` が溢れる）。
- **確かめ方**: `pg_stat_replication` の `state`（`streaming`）・`sync_state`（`quorum`・`sync`）、`replay_lag`。スタンバイで `pg_last_wal_replay_lsn`。
- **危険**:
  - 新しいスタンバイは `catchup` から始まり、`streaming` になってから同期になる。
  - スタンバイを作る間はベンチを止める。
  - 書き込みの応答の「前」の待ち（アプリが `replay_lsn` を監視）は、**全ての書き込みに一律に払うと書き込みの時間の 90% になった**。どの書き込みが別の台から読まれるかで分ける。
  - **スタンバイの遅れは、リージョン間の往復が床**。
- **出典**: https://www.postgresql.org/docs/16/warm-standby.html、https://www.postgresql.org/docs/16/runtime-config-wal.html

### ホットスタンバイの読み取り（GET で書かない）

- **効く条件**: 読み取りをスタンバイで処理する構成。
- **やり方**: スタンバイで許されるのは `SELECT`・`COPY TO`・カーソル・`SHOW`・`SET`・`PREPARE`/`EXECUTE`（読み取りのみ）。**許されないもの**: `INSERT`・`UPDATE`・`DELETE`・`TRUNCATE`・DDL・**`SELECT ... FOR SHARE/UPDATE`**・**`nextval`**・一時テーブルへの書き込み・`LISTEN/NOTIFY`・`ANALYZE`・`VACUUM`・SERIALIZABLE（文書）。**GET のハンドラとミドルウェアで DB に書かない**（書くとスタンバイでだけ失敗する）。セッションの最終アクセス時刻の更新などを GET に入れない。
- **確かめ方**: スタンバイに向けた GET で 5xx が 0 件。`SHOW in_hot_standby;`。
- **危険**:
  - **書いた直後の読みが古い**（スタンバイは結果整合）。**`sleep` で待っても保証にならない**（ISUCON10 予選: レプリカの読み取りが CSV 投入・在庫更新の直後に先行し、0.5 秒待っても検証に落ちた）。`remote_apply` か、書いた本人の直後の読みを primary に向ける。
  - スタンバイの `max_connections` は primary 以上。
- **出典**: https://www.postgresql.org/docs/16/hot-standby.html、（ISU-297 の節、初期化直後の確認）

### レプリケーションを入れる前に知ること

- **同じ構成では 1 プロセスのメモリの状態をもう一方から見えない**。メモリに持つなら、DB の正から作り直せる形にするか、全プロセスに配る仕組みとセットで入れる。
- **単純に「書き込みを primary に、読みを複数のスタンバイに」したくなるが**、友達申請の 63% が別リージョン宛てだったように、データの局所性を測らずに前提を置くと誤る。
- 同一リージョン内の読み取り用スタンバイにすると、**順番を間違えると書き込みが止まる**。

---

## 13. ロックとデッドロック（PostgreSQL）

### 行ロックの強さを必要最小限に（FOR NO KEY UPDATE・FOR SHARE）

- **効く条件**: 同じ行を取り合う API どうしで待ちが長い（`pg_stat_activity` で `wait_event_type = 'Lock'`・`wait_event = 'transactionid'`/`tuple'`、`log_lock_waits` に出る）。
- **やり方**:
  - 行ロックの強さ（弱い順）: `FOR KEY SHARE` < `FOR SHARE` < `FOR NO KEY UPDATE` < `FOR UPDATE`。`UPDATE`（キーを変えない）は `FOR NO KEY UPDATE`、`DELETE` は `FOR UPDATE` を取る。**`FOR NO KEY UPDATE` は `FOR KEY SHARE` を止めない**（外部キーの参照のロックと両立する）。
  - **ロックを持ったまま重い処理（画像の INSERT・権限の SQL）をしない**。
- **確かめ方**: `pg_locks`、`pg_stat_activity` の `wait_event`、`pg_stat_statements` で `FOR UPDATE` 系の SQL の平均時間。
- **出典**: https://www.postgresql.org/docs/16/explicit-locking.html

### デッドロックの避け方

- **効く条件**: ログに `deadlock detected`。同じ複数の行を別の順序で更新する経路がある。
- **やり方**: 文書の指針: ① **ロックを取る順序をそろえる**（複数の行は id の昇順など）、② 最も強いロックを最初に取る、③ デッドロックで中断されたトランザクションは再試行する（どのトランザクションが中断されるかは予測できない）、④ トランザクションを短く保つ。アドバイザリロック（`pg_advisory_xact_lock`）は組・名前など「行にならないもの」の排他に使える（トランザクションが終わると自動解放。セッション単位は明示的に解放が要る）。**`LIMIT` と一緒にアドバイザリロックの関数を呼ぶと、意図しない行までロックされうる**（副問い合わせで行を絞ってから呼ぶ）。
- **確かめ方**: PostgreSQL のログ、`pg_locks`。
- **危険**: 再試行は冪等でないと二重に実行される。**前回の練習（ISUCON9 本戦）では、デッドロックを 500 のまま返して再試行しなかったため、負荷を上げたら整合性の失格につながった**。
- **出典**: https://www.postgresql.org/docs/16/explicit-locking.html

---

## よくある失敗

| 失敗 | 何が起きるか | 避け方 |
| --- | --- | --- |
| `fsync = off`・`full_page_writes = off`・`UNLOGGED` を、再起動の試験の方法を読まずに入れる | 強制停止・再起動でデータ破壊・消失（失格） | マニュアルで再起動の方法を確かめ、自分たちで同じ方法で試す。迷ったら入れない |
| `synchronous_commit = off` を、課金・予約などの外部副作用のある経路に入れる | 直近のコミットが消えるのに、外には完了が伝わっている | 経路ごとに判断する。外部に副作用がある経路・読みの反映が必要な経路には使わない |
| `pg_stat_statements` を入れ忘れて、再起動が要る | スロークエリが見えないまま昼になる | 最初の反映で `shared_preload_libraries` に入れる |
| 同じキーを複数の conf.d に書く・`ALTER SYSTEM` で上書きされる | 設定が効かない | 反映後に `SHOW`・`pg_file_settings`・`pending_restart` を見る |
| プールの上限を上げ続ける | ロック待ち・CPU の取り合いで逆に遅い | 取得待ちの実測から決めて、段階的に比べる |
| 部分インデックスの述語がクエリの引数に合わない | インデックスが使われない | 引数でなく述語を含意する条件をクエリに書く |
| パーティションのキーを条件に与えない | 全パーティションを探す | `EXPLAIN` の `Append` の下を数える |
| GIN・大きなインデックスを initialize で作る | WAL が大きくなり、複製の負荷と initialize の時間が増える | initialize の時間と WAL を測ってから入れる |
| スタンバイの GET で書く（`FOR SHARE`・`nextval`・更新） | スタンバイでだけ失敗する | GET では書かない。書く必要があれば primary へ |
| 書いた直後の読みをスタンバイに向けて `sleep` で待つ | 古い値が返り、検証に落ちる | `remote_apply` か primary 読み。固定待機を保証にしない |
| `EXPLAIN ANALYZE` を書き込みの文にそのまま使う | データが変わる | `BEGIN` … `ROLLBACK` で囲む |
| 同期スタンバイが落ちたときの戻し方を用意せずに `ANY 2` にする | commit が止まり、全体が止まる | 戻し方（`synchronous_standby_names` を戻す手順）を先に書く |

---

## 出典一覧

公式ドキュメント（PostgreSQL 16）

- https://www.postgresql.org/docs/16/runtime-config-resource.html
- https://www.postgresql.org/docs/16/runtime-config-wal.html
- https://www.postgresql.org/docs/16/runtime-config-query.html
- https://www.postgresql.org/docs/16/runtime-config-connection.html
- https://www.postgresql.org/docs/16/runtime-config-logging.html
- https://www.postgresql.org/docs/16/wal-async-commit.html
- https://www.postgresql.org/docs/16/server-shutdown.html
- https://www.postgresql.org/docs/16/sql-prepare.html
- https://www.postgresql.org/docs/16/jit-decision.html
- https://www.postgresql.org/docs/16/pgstatstatements.html
- https://www.postgresql.org/docs/16/auto-explain.html
- https://www.postgresql.org/docs/16/using-explain.html
- https://www.postgresql.org/docs/16/monitoring-stats.html
- https://www.postgresql.org/docs/16/routine-vacuuming.html
- https://www.postgresql.org/docs/16/indexes-index-only-scans.html
- https://www.postgresql.org/docs/16/indexes-partial.html
- https://www.postgresql.org/docs/16/brin-intro.html
- https://www.postgresql.org/docs/16/pgtrgm.html
- https://www.postgresql.org/docs/16/datatype-json.html
- https://www.postgresql.org/docs/16/ddl-partitioning.html
- https://www.postgresql.org/docs/16/sql-createtable.html
- https://www.postgresql.org/docs/16/populate.html
- https://www.postgresql.org/docs/16/sql-insert.html
- https://www.postgresql.org/docs/16/warm-standby.html
- https://www.postgresql.org/docs/16/hot-standby.html
- https://www.postgresql.org/docs/16/explicit-locking.html
- https://www.pgbouncer.org/config.html
- https://docs.rs/sqlx/0.8.6/sqlx/pool/struct.PoolOptions.html
- https://docs.rs/sqlx/latest/sqlx/postgres/struct.PgConnectOptions.html
- https://docs.rs/sqlx/0.8.6/sqlx/struct.QueryBuilder.html
- https://go.dev/doc/database/manage-connections
- https://blackglory.me/notes/postgresql/PostgreSQL(v15)/Full_Text_Search_(FTS)/N-Gram_Index?variant=en（個人のノート。検索結果の要約のみ確認）

ISUCON の記録

- https://isucon.net/archives/57993937.html（ISUCON13 結果発表。fail の理由）
- https://zenn.dev/kawaemon/articles/f40529e0c447f5（ISUCON14。データ保持違反）
- 手元の実例 （ISUCON10 予選。出典 https://qiita.com/lostfind/items/40f77453f8c91d49014e）

