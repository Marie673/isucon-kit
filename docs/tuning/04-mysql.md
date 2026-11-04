# 04 MySQL / MariaDB

**競技のルールは毎回違う。当日のマニュアルが優先する。** この章の「危険」は、ルールが再起動後のデータ保持・追試・初期化の時間を求める前提で書いた。

## この章で扱う範囲

- MySQL 8.x と MariaDB の「サーバー設定」「アプリ（Go・Rust）側の接続」「スキーマ」「DB の別台への移動」。
- 扱わないもの: 計測の入れ方（slow log の解析ツールなど。[01 計測](01-measurement.md)）、PostgreSQL（`05-postgresql.md`）、SQL そのものの書き方・インデックス設計（`06-sql-and-indexes.md`）。
- 版の前提: 公式ドキュメントは MySQL 8.4 の版を読んだ。**8.0 と 8.4 で既定値が違うもの**（`innodb_adaptive_hash_index` など）は項目の中で書く。実機では必ず `SELECT VERSION` と `SHOW VARIABLES LIKE '...'` で確かめる。
- 数字（推奨値）は出典の値を書いた。問題・サーバー（メモリ・ディスク・台数）で変わる。そのまま持ち込まない。

## 最初に見るもの（当日の 5 分チェックリスト）

1. 版と実装: `SELECT VERSION;`（MySQL か MariaDB か、8.0 か 8.4 か 5.7 か）。
2. メモリの配分: `free -m`、同じ台で動くアプリ・nginx の RSS。`SHOW VARIABLES LIKE 'innodb_buffer_pool_size';`
3. 永続化まわりの現状: `SHOW VARIABLES WHERE Variable_name IN ('innodb_flush_log_at_trx_commit','sync_binlog','log_bin','innodb_doublewrite','innodb_flush_method');`
4. 接続の上限と実使用: `SHOW VARIABLES LIKE 'max_connections';`、`SHOW GLOBAL STATUS LIKE 'Max_used_connections';`、アプリの台数 × プロセス数 × プール上限との掛け算。
5. スロークエリを取る（`long_query_time` を 0 か小さい値にして 1 走行）。取り方は[01 計測](01-measurement.md)。取れたら `EXPLAIN` を読む（`06-sql-and-indexes.md`）。
6. アプリの接続文字列（Go なら DSN の `interpolateParams`・`parseTime`・`collation`）とプールの設定（`SetMaxOpenConns` など）。
7. スキーマの文字コード・照合順序: `SHOW CREATE TABLE`。列ごとの文字コードが揃っているか。
8. **再起動試験の方法をマニュアルで確かめる**（OS 再起動か、サービスの再起動か。どの時点でデータを確かめるか）。永続化に触る前に必ず。

---

## 1. 設定の反映のしかた（動的・再起動）

### 設定が「動的」か「再起動」かを先に知る

- **効く条件**: 設定を変えたのに効いていない。`SET GLOBAL` で試した値が再起動で戻った。
- **やり方**:
  - 動的（`SET GLOBAL` で再起動なしに変えられる）と、MySQL 8.4 の文書が示すもの: `innodb_flush_log_at_trx_commit`、`innodb_doublewrite`（ON 系どうしの切替だけ）、`innodb_redo_log_capacity`、`innodb_io_capacity`、`innodb_io_capacity_max`、`innodb_log_buffer_size`、`innodb_flush_neighbors`、`innodb_adaptive_hash_index`、`innodb_buffer_pool_size`（チャンク単位で変更）、`sync_binlog`、`slow_query_log`、`long_query_time`。
  - 起動時だけ（再起動が要る）: `innodb_flush_method`、`ngram_token_size`、`performance_schema`、`innodb_buffer_pool_chunk_size`、`innodb_buffer_pool_instances`、`skip-log-bin`（binlog の無効化）、`innodb_doublewrite` の ON と OFF の切替。
  - 動的に変えた値は **my.cnf に書かないと再起動で元に戻る**（MySQL 8.0 以降の `SET PERSIST` で mysqld-auto.cnf に残す手もある。`SET PERSIST` は出典未確認）。
  - 設定は `/etc/mysql/mysql.conf.d/` など、読まれる場所に置く。`mysqld --verbose --help` の先頭に読むファイルの一覧が出る（出典未確認）。同じキーが複数のファイルにあると後のものが勝つ。
- **確かめ方**: 再起動後に `SHOW VARIABLES LIKE '...'`。デプロイ後にも同じコマンドで確かめる。
- **危険**: 再起動が要る設定を変えると、ベンチ中に DB が止まる。競技サーバーの反映はベンチを走らせていない時に行う。再起動後にアプリが DB へ再接続できるか（Go の `database/sql` は壊れた接続を捨てて張り直す。Rust の sqlx は `test_before_acquire` などの設定次第）も確かめる。
  - https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html（各変数の Dynamic の欄）
  - https://dev.mysql.com/doc/refman/8.4/en/innodb-redo-log.html
  - https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool-resize.html
  - https://dev.mysql.com/doc/refman/8.4/en/fulltext-search-ngram.html
  - https://dev.mysql.com/doc/refman/8.4/en/performance-schema-startup-configuration.html
  - 手元の例: （DB 再起動後の切断済み接続）

---

## 2. 永続化に関わる設定（危険が大きい。最初に読む）

この節の設定は、点を大きく上げる定番であると同時に、**再起動後にデータが消えて失格になりうる**。次の事実を先に押さえる。

- ISUCON13 では、再起動後のデータチェックの不整合で fail したチームが 4、再起動後のスコアが 75% 以下で 1、再起動後スコア 0 で 51 チームだった（公式の結果発表）。
- ISUCON14 では「データ保持違反」で失格になったチームの参加記がある（原因はそのチームも不明としている）。ベンチ後の画面に「データ保持確認」ボタンがあり、自分で対象サーバーを再起動したあとに押して確かめられる形だった。
- 失格の判定は **再起動の方法とデータの確かめ方で決まる**。OS の正常な再起動なら通る設定でも、プロセスの kill や電源断では消える。当日のマニュアルで再起動の方法を読み、**その方法で自分たちが試して**から採用する。
- 出典: https://isucon.net/archives/57993937.html（ISUCON13 の結果発表。fail の理由の一覧）、https://zenn.dev/kawaemon/articles/f40529e0c447f5（ISUCON14 参加記）

### innodb_flush_log_at_trx_commit

- **効く条件**: 書き込みの多い問題。コミットのたびに redo log を fsync している（`Innodb_os_log_fsyncs`（出典未確認）の増え方が速い、`iostat` の書き込みの待ちが長い、スロークエリが INSERT/UPDATE/COMMIT 系）。
- **やり方**: 既定は `1`（コミットごとに書いて fsync）。`2` は「コミットごとに書き、fsync は 1 秒に 1 回」、`0` は「1 秒に 1 回、書いて fsync」。
  ```ini
  [mysqld]
  innodb_flush_log_at_trx_commit = 2
  ```
  ISUCON の記録では `= 2`（ISUCON12 予選の作問者 acidlemon の解説。dice801 の参加記には「MySQL の書き込みがきつそうなので `= 2` にした（21,579 点）」とあるが、アプリ 2 台化などと同じ時期で単独の効果ではない）か `= 0`（ISUCON4 の kazeburo の事後練習）が出てくる。MariaDB には `3`（prepare と commit の両方で fsync）もある。
- **確かめ方**: 同じ負荷で書き込み系 API の平均時間、`iostat -x 1` の `w_await`・`%util`、`Innodb_os_log_fsyncs`（出典未確認）の増分を比べる。**単独の効果は参加記では分離されていない**（ほかの変更と同時に入れている）ので、自分たちで前後を取る。
- **危険**:
  - `0` と `2` では、**予期しない停止で最大 N 秒（`innodb_flush_log_at_timeout`、既定 1 秒）分のコミット済みトランザクションが消えうる**。文書は「`0` はプロセスが落ちただけでも、`2` は OS の停止・電源断で」という違いを書いている（「`2` は mysqld だけが落ちたときは OS のキャッシュに書かれているので残る」という区別は広く言われるが、MySQL 8.4 の文書には書かれていない。出典未確認。8.4 の文書は「予期しない mysqld の終了で最大 N 秒分が消えうる」と書く）。1 秒ごとの fsync も「100% 保証ではない」。
  - InnoDB のクラッシュ回復は、この設定にかかわらず働く。トランザクションは「全部適用」か「全部消える」で、**壊れはしない**（文書）。消えるのは直近の分だけ。
  - **再起動の試験が「正常な停止（`systemctl stop` や `reboot`）」なら通ることが多いが、出典は無い**（正常停止での redo log の扱いは `innodb_fast_shutdown` の文書で要確認。出典未確認）。電源断・`kill -9`・インスタンスの強制停止に当たる試験なら、消える。
  - 複製（レプリケーション）の構成では、文書が `sync_binlog = 1` と `innodb_flush_log_at_trx_commit = 1` を勧める。
  - ベンチの追試が「直前のベンチで作ったデータが再起動後に全部残っていること」を確かめるなら、1 秒分の消失は直近のデータの欠けとして見つかる可能性がある。ベンチ終了から再起動までの間に 1 秒以上あれば、普通は flush 済み。ただし保証ではない。
  - OS・ディスクの側が fsync を偽る場合は、`1` でも保証されない（文書の警告）。
  - https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_flush_log_at_trx_commit
  - https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables
  - https://isucon.net/archives/56842718.html（ISUCON12 作問者の解説。binlog 無効と `= 2`）
  - https://zenn.dev/dice801/articles/ebc027207257b1（ISUCON12、`= 2` で 21,579 点。検索結果の抜粋のみ確認）
  - https://kazeburo.hatenablog.com/entry/2014/10/14/170129（ISUCON4 の事後練習。`= 0`、`innodb_flush_method=O_DIRECT`、`innodb_buffer_pool_size = 1G`）

### sync_binlog と binlog の無効化（skip-log-bin / disable_log_bin）

- **効く条件**: MySQL 8.0 以降で書き込みが重く、複製を使わない。`SHOW VARIABLES LIKE 'log_bin';` が `ON`、データディレクトリに `binlog.0000xx` が増えている、`Binlog_cache_use`・`Binlog_cache_disk_use`（出典未確認）が増えている。
- **やり方**: MySQL 8.0 以降は binlog が**既定で有効**（`--initialize` で初期化する場合を除く）。無効化は次のとおり（起動時のみ。再起動が要る）。
  ```ini
  [mysqld]
  skip-log-bin          # または disable-log_bin（`--disable-log-bin` と同じ意味）
  ```
  無効にすると、`--log-replica-updates` や `--replica-preserve-commit-order` も外す必要がある。`sync_binlog` は binlog を使い続けるときの設定で、既定は `1`（コミット前に fsync）、`0` は OS 任せ、`N` は N 回のコミットグループごと。
- **確かめ方**: `SHOW VARIABLES LIKE 'log_bin';` が `OFF`、データディレクトリで binlog が増えない。書き込み API の時間と `iostat` を前後で比べる。
- **危険**:
  - binlog を止めると、**binlog からの復旧・レプリカの作成・PITR ができなくなる**。複製を使う構成（MySQL を別台に複製する、読み取りレプリカを置くなど）では無効にできない。
  - GTID を使っている場合、**異常終了のあとで binlog を無効にすると GTID を失って複製が壊れうる**（正常停止のあとだけ安全）。
  - `sync_binlog = 0` や `N > 1` は、電源断・OS クラッシュのときに「コミット済みだが binlog に無い」トランザクションが出うる。`sync_binlog = 1` のままなら、binlog に無いトランザクションは prepared 状態で残り、回復時に rollback される（文書）。
  - binlog を無効にしても、データ本体の永続化（redo log）は変わらない。再起動後のデータ保持には直接は響かない（出典: 文書の記述から読んだ範囲。実機で確かめる）。
  - 5.7 以前や MariaDB は既定が違う場合がある（MariaDB の比較表は「両方とも最近の版では既定 ON」としている）。`SHOW VARIABLES` で確かめる。
  - https://dev.mysql.com/doc/refman/8.4/en/replication-options-binary-log.html（enabling and disabling binary logging、`sync_binlog`）
  - https://isucon.net/archives/56842718.html（ISUCON12 の解説に「binlog を無効にする」とある）
  - https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/migrating-to-mariadb/moving-from-mysql/mysql-to-mariadb-compatibility-matrix

### innodb_doublewrite

- **効く条件**: ディスクの書き込みがボトルネックで、バッファプールのフラッシュが多い（`Innodb_dblwr_writes`・`Innodb_buffer_pool_pages_flushed`（どちらも出典未確認）の増え方、`iostat` の書き込み）。
- **やり方**: 既定は `ON`。MySQL の 8.0.30 以降は `ON`（= `DETECT_AND_RECOVER`）・`OFF`・`DETECT_ONLY` を取る。`DETECT_ONLY` は中身を書かずメタデータだけで、**不完全なページの書き込みを検出できるが、直せない**。`OFF` にする場合:
  ```ini
  [mysqld]
  innodb_doublewrite = OFF
  ```
  ON 系と OFF の動的な切替はできない。再起動が要る。MariaDB 11.0.6 以降は `OFF`・`ON`・`fast`（ON と同じだがデータファイルの同期をしない）。
- **確かめ方**: 書き込み系 API の時間と `iostat`。ベンチの結果が変わらないなら入れない。
- **危険**: 文書は「データの整合性より性能を優先する場合（ベンチマークなど）だけ検討する」としている。ダブルライトバッファは **OS・ストレージ・mysqld が書き込みの途中で落ちたときに壊れたページを直すための保険**。`OFF` にして電源断・強制停止が起きると、**壊れたページが直せず、起動できない・データが壊れる**可能性がある（文書の説明からの帰結）。再起動の試験が強制停止を含むなら使わない。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-doublewrite-buffer.html、https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html（`innodb_doublewrite`）、https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables

### innodb_flush_method

- **効く条件**: OS のページキャッシュとバッファプールが二重にメモリを食っていて（`free -m` の `buff/cache` が大きいのに InnoDB のバッファプールも大きい）、メモリが足りない。
- **やり方**: `O_DIRECT` はデータファイルの書き込みで OS のキャッシュを通さない。MySQL 8.4 の文書は Unix の既定を「`O_DIRECT`（対応していれば）、なければ `fsync`」と書く。MariaDB は 10.6.0 以降 `O_DIRECT` が既定（11.0 で非推奨）。**起動時だけ**変更できる。
  ```ini
  [mysqld]
  innodb_flush_method = O_DIRECT
  ```
- **確かめ方**: `SHOW VARIABLES LIKE 'innodb_flush_method';`、`free -m` の `buff/cache`、`iostat`。既定のままで `O_DIRECT` になっているなら変更は不要。
- **危険**:
  - `O_DIRECT_NO_FSYNC` は、redo log とデータファイルが別のストレージにあり、バッテリー保護のないキャッシュの上にあるとき、**予期しない終了でデータを失いうる**（文書）。そういう構成なら `O_DIRECT` を使う。
  - `littlesync`・`nosync` は内部試験用でサポート外（文書）。使わない。
  - `O_DIRECT` にすると OS のキャッシュが効かなくなるので、バッファプールを小さくしすぎると読み込みが遅くなる。
  - ISUCON4 の事後練習の例（`innodb_flush_method=O_DIRECT`、`innodb_flush_log_at_trx_commit=0`、`innodb_buffer_pool_size=1G` を合わせて入れて 40,812 点）は、**個別の寄与も永続化の検証も書かれていない**。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_flush_method、https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables、https://kazeburo.hatenablog.com/entry/2014/10/14/170129

---

## 3. メモリと I/O

### innodb_buffer_pool_size

- **効く条件**: ディスクからの読み込みが多い。`SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_reads';`（ディスクまで読みに行った数）が `Innodb_buffer_pool_read_requests` に比べて多い。`iostat` の読み込みが多い。データ（`SELECT SUM(data_length+index_length) FROM information_schema.tables WHERE table_schema='...'`）がバッファプールより大きい。
- **やり方**:
  - 専用の DB サーバーなら物理メモリの 80% が目安（MySQL 8.4 の文書）。ただし InnoDB は指定量の約 10% 多くメモリを確保する。
  - **アプリや nginx と同居している台は 50〜70%**。同居するプロセスの余地を残す。
  - 動的に変更できる（`SET GLOBAL innodb_buffer_pool_size = ...`）。チャンク（既定 128MB）×インスタンス数の倍数に丸められる。
  ```ini
  [mysqld]
  innodb_buffer_pool_size = 2G
  ```
- **確かめ方**: `Innodb_buffer_pool_reads` と `Innodb_buffer_pool_read_requests` の比（ヒット率）。`Innodb_buffer_pool_wait_free` が増え続けるならフラッシュが追いついていない（この 2 つの指標の意味は MySQL の文書の別ページ。出典未確認）。`free -m` でスワップが出ていないか。
- **危険**:
  - 大きくしすぎると OS がスワップする、または OOM で mysqld が落ちる。**ISUCON8 予選では MySQLTuner の推奨をそのまま入れて DB が落ちた**（メモリ不足と推測。最終値は記載なし）。接続ごとのバッファ（`sort_buffer_size`・`join_buffer_size` など）の分も残す。
  - ISUCON12 の解説は、3.7GB のメモリのインスタンスで「バッファプールを慎重に大きくする（スワップさせない）」としている。
  - 再起動後は冷えた状態から始まる。`innodb_buffer_pool_dump_at_shutdown`・`innodb_buffer_pool_load_at_startup`（どちらも既定 ON）で暖機される。再起動試験の直後にベンチが走るなら、暖機の分だけ最初が遅い。**ISUCON8 予選の事例では、暖機を設定したが採点の条件の読み違いで時間を費やしたと振り返っている**。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_buffer_pool_size、https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool-resize.html、https://dev.mysql.com/doc/refman/8.4/en/innodb-preload-buffer-pool.html、https://hikalium.hatenablog.jp/entry/2018/09/16/235250、https://isucon.net/archives/56842718.html

### innodb_log_file_size と innodb_redo_log_capacity

- **効く条件**: 書き込みが多く、チェックポイントの頻繁なフラッシュで書き込みが詰まる（redo log の使用量が容量に近い）。MySQL 8.0.30 以降は `Innodb_redo_log_current_lsn` と `Innodb_redo_log_checkpoint_lsn` の差（チェックポイントの遅れ）が容量に近づく。
- **やり方**:
  - MySQL 8.0.30 以降: `innodb_redo_log_capacity`（既定 100MB。動的。範囲 8MB〜512GB）。`innodb_log_file_size` と `innodb_log_files_in_group` を置き換える（`innodb_redo_log_capacity` を指定すると前の 2 つは無視される）。
    ```sql
    SET GLOBAL innodb_redo_log_capacity = 8589934592;  -- 8GB
    ```
  - それ以前の MySQL: `innodb_log_file_size`（再起動が要る。8.0.30 より前の挙動は出典未確認）。
  - MariaDB: `innodb_log_file_size`（既定 96MB。10.9 以降は動的）。MariaDB には `innodb_redo_log_capacity` が無い。
  ```ini
  [mysqld]
  innodb_redo_log_capacity = 4G     # MySQL 8.0.30+
  # innodb_log_file_size = 1G       # MariaDB など
  ```
- **確かめ方**: `SHOW STATUS LIKE 'Innodb_redo_log%';` の `Innodb_redo_log_current_lsn - Innodb_redo_log_checkpoint_lsn`（チェックポイントの遅れ。文書は直接は載せず、この差で求める）、`performance_schema.innodb_redo_log_files`。書き込み API の時間の揺れ。
- **危険**: redo log が大きいほどクラッシュ回復に時間がかかる。ベンチ・再起動試験で回復が長くなるが、ISUCON の規模では通常は秒単位（実測値は出典未確認）。ディスクの空きも使う。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-redo-log.html、https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_redo_log_capacity、https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables

### innodb_log_buffer_size

- **効く条件**: 1 トランザクションで多くの行を更新・挿入する（一括 INSERT・大きな UPDATE）。コミットの前にログバッファが溢れてディスクに書かれる（`SHOW GLOBAL STATUS LIKE 'Innodb_log_waits';` が増える。この指標は出典未確認）。
- **やり方**: 8.4 の既定は 64MB（動的）。大きなトランザクションを書くなら大きくする（文書）。
- **確かめ方**: 一括 INSERT や初期化の時間。
- **危険**: 特になし（メモリの消費が増えるだけ）。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_log_buffer_size

### innodb_io_capacity と innodb_io_capacity_max、innodb_flush_neighbors

- **効く条件**: ベンチ中にダーティページのフラッシュが追いつかず書き込みが詰まる。SSD（EBS gp3 など）で既定が低い・高いと感じる場面。
- **やり方**: `innodb_io_capacity` は InnoDB のバックグラウンド処理に割り当てる IOPS（8.4 の既定は 10000。動的）。`innodb_io_capacity_max` はフラッシュが遅れたときの上限（既定は 2 倍）。`innodb_flush_neighbors` は 8.4 の既定が `0`（SSD では 0 で書き込みを散らす）。**8.0 以前は既定値が違う**（出典未確認）。
- **確かめ方**: `Innodb_buffer_pool_pages_dirty` の推移、`iostat`。ストレージの IOPS（EBS なら gp3 の既定 3000）と比べる（EBS の数値は出典未確認）。
- **危険**: 実際のストレージの IOPS を超える値にしても性能は上がらない。ほかのプロセスの I/O を圧迫しうる。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html（`innodb_io_capacity`、`innodb_io_capacity_max`、`innodb_flush_neighbors`）

---

## 4. 接続・サーバーの上限

### max_connections

- **効く条件**: アプリが `Too many connections`（エラー 1040）で落ちる。`Max_used_connections` が `max_connections` に近い。アプリの台数 × プロセス数 × プール上限が `max_connections` を超えている。
- **やり方**: 既定は 151（ISUCON12 の解説）。`SET GLOBAL max_connections = 500;` で動的に変えられる。**アプリ側のプール上限の合計から決める**（アプリ 3 台 × プロセス 2 × プール 60 = 360 など）。接続は管理用に +1（`CONNECTION_ADMIN` か `SUPER` の権限を持つ人）が残る。
  ```ini
  [mysqld]
  max_connections = 1000
  ```
- **確かめ方**: `SHOW GLOBAL STATUS LIKE 'Max_used_connections';`、`SHOW PROCESSLIST;`。アプリのログに 1040 が出ないこと。
- **危険**:
  - 接続ごとにスレッドとバッファ（`sort_buffer_size` など）が要る。大きくしすぎるとメモリが足りなくなる。
  - 接続の数を増やしても、DB の CPU・ロックがボトルネックなら速くならない。**接続を増やして遅くなることもある**（ISUCON11 本選の fujiwara 組はプールを 10 → 20 → 40 → 60 と段階的に比べた）。
  - `table_open_cache`・`open_files_limit`・`performance_schema` の自動サイズは `max_connections` に連動する。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/too-many-connections.html、https://isucon.net/archives/56842718.html、https://sfujiwara.hatenablog.com/entry/isucon11-final

### skip_name_resolve

- **効く条件**: アプリが別の台から接続する構成で、新規接続の確立が遅い（接続のたびに逆引き DNS が走る）。`performance_schema.host_cache` に失敗が出る。
- **やり方**:
  ```ini
  [mysqld]
  skip_name_resolve = ON
  ```
  有効にすると、**ホスト名で作ったアカウント（`'isucon'@'host.example.com'`）が使えなくなり、IP アドレスのアカウントだけが使える**（文書）。`'isucon'@'%'` や IP のアカウントを作っておく。
- **確かめ方**: 新規接続の時間（`mysql -h <db> -e 'select 1'` の時間）。接続が拒否されないこと。
- **危険**: 有効にしたあとで、権限がホスト名で書かれたアカウントにログインできなくなる。競技の管理用アカウントを消さない・壊さない（マニュアルの追試・運営用アカウントの規定を読む）。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/host-cache.html

### table_open_cache

- **効く条件**: `Opened_tables` が急に増え続けている（キャッシュが小さい）。テーブルの数が多い・接続が多い構成。
- **やり方**: 8.4 では起動時に自動でサイズが決まる。大きくするなら起動時に設定（`table_open_cache = 4000` など）。目安は「同時接続数 × 1 つのクエリで JOIN する最大のテーブル数」（文書）。ファイル記述子の上限（`open_files_limit`）に当たる。
- **確かめ方**: `SHOW GLOBAL STATUS LIKE 'Opened_tables';` の増え方。
- **危険**: OS のファイル記述子の上限を超えると、接続の拒否・クエリの失敗が起きる（文書）。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/table-cache.html

### performance_schema

- **効く条件**: CPU・メモリに余裕が無く、計測を使っていない。逆に、`performance_schema` の情報を使って計測しているときは止めない。
- **やり方**: 既定で有効。無効にするには起動時に指定する（再起動が要る）。
  ```ini
  [mysqld]
  performance_schema = OFF
  ```
  有効のときはメモリが増える（文書。インスタンス数に比例）。**CPU への影響の数字は文書に無い**。ISUCON の参加記でも単独の効果を分離したものは見つからなかった（出典未確認）。
- **確かめ方**: 切る前後で `top` の mysqld の CPU とベンチの点を比べる。
- **危険**: 切ると `performance_schema` を使う計測（`events_statements_summary_by_digest`、ロック待ちの調査など）ができなくなる。**計測を先に済ませてから切る**。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/performance-schema-startup-configuration.html

### adaptive hash index（innodb_adaptive_hash_index）

- **効く条件**: 点引きが多い読み取りで CPU が高く、`SHOW ENGINE INNODB STATUS` の SEMAPHORES に adaptive hash index のラッチ待ちが出る（この見方は出典未確認）。
- **やり方**: **MySQL 8.4 の既定は `OFF`。それより前は有効が既定**（文書）。MariaDB は 10.5 以降の既定が `OFF`。動的（`SET GLOBAL`）。文書は「実際の負荷で有効・無効を比べる。効かない負荷もある」と書く。
- **確かめ方**: 無効・有効で同じ負荷のベンチを比べる。
- **危険**: 無効にするとハッシュ表がすぐ空になる（文書）。再度有効にすると通常運転の中で再構築される。永続化には関係しない。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_adaptive_hash_index、https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables

### transaction_isolation

- **効く条件**: ロック待ち・デッドロックが多い。`SELECT ... FOR UPDATE` や範囲の `UPDATE`・`DELETE` でギャップロックが多くの行を止めている。
- **やり方**: 既定は REPEATABLE READ。READ COMMITTED にすると、**検索・インデックススキャンのギャップロックが無効になり**、`UPDATE`・`DELETE` が実際に変えた行にだけロックを持つ（WHERE の判定後に外れた行のロックは解放）。
  ```sql
  SET GLOBAL transaction_isolation = 'READ-COMMITTED';
  -- アプリの接続ごとに SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;
  ```
- **確かめ方**: デッドロックの回数（`SHOW ENGINE INNODB STATUS` の LATEST DETECTED DEADLOCK、`innodb_print_all_deadlocks`）、ロック待ちの時間（`performance_schema.data_lock_waits` は出典未確認）。
- **危険**:
  - READ COMMITTED では**ファントムリードが起きる**（同じトランザクション内で同じ条件の SELECT が別の結果を返す）。範囲の整合性（「空きがあるか数えてから挿入」など）をトランザクション内で保っているコードは壊れる。
  - 外部キーと重複キーのチェックではギャップロックを使い続ける（文書）。
  - READ COMMITTED では binlog は row 形式のみ（`binlog_format = MIXED` なら自動で row になる）。
  - 整合性チェックで失格になるおそれがあるので、変更前後で整合性のテスト（skill `critical-tests`）を通す。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html、https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html

---

## 5. 文字コード・照合順序（インデックスが効かなくなる）

- **効く条件**: JOIN や WHERE のインデックスがあるのに `EXPLAIN` の `type` が `ALL`。JOIN する 2 つの列の文字コードが違う（`utf8mb3` と `utf8mb4`、`latin1` と `utf8mb4`）。MySQL 5.7 → 8.0 の入れ替えで急に遅くなった。数値の列を文字列と比べている、文字列の列を数値と比べている。
- **やり方**:
  - 文書の規則: 「文字列の列どうしの比較では、同じ文字コードにする。`utf8mb4` の列と `latin1` の列の比較ではインデックスが使えない」「文字列の列を数値や日時と比べると、インデックスが使えなくなることがある」「JOIN する列は同じ型・同じ大きさにする（`VARCHAR(10)` と `CHAR(15)` は別扱い）」。
  - `SHOW CREATE TABLE` で列と表の `CHARACTER SET`・`COLLATE` を見る。違う列があれば揃える（`ALTER TABLE ... MODIFY ... CHARACTER SET utf8mb4`。時間がかかる。節 8 の「スキーマ変更」を読む）。
  - ASCII だけの識別子の列（ULID の `CHAR(26)` など）は `latin1` にしてインデックスを小さくした例がある（ISUCON11 本選。日本語列には使わない）。
  - Go のドライバでは DSN の `collation`（既定 `utf8mb4_general_ci`）が接続の照合順序を決める。サーバー側の既定は MySQL 8.0 が `utf8mb4_0900_ai_ci`、MariaDB 11.4 以降が `utf8mb4_uca1400_ai_ci`。接続の照合順序とサーバーの既定が食い違ったときの挙動は出典未確認（`SHOW VARIABLES LIKE 'collation%'` で確かめる）。
- **確かめ方**: `EXPLAIN` の `type`・`key`・`key_len`、`EXPLAIN` の `Extra` に変換の痕跡（`Using where` で `rows` が多い）が出る。
- **危険**: 列の文字コードを変えると、**ソート順・比較の結果（大文字小文字・アクセントの区別）が変わる**。ベンチの整合性チェックや、ユニークキーの重複判定が変わりうる。静的データを書き換えない（マニュアルで禁止されていないか確認）。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html、https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/migrating-to-mariadb/moving-from-mysql/mysql-to-mariadb-compatibility-matrix、https://github.com/go-sql-driver/mysql/blob/master/README.md

---

## 6. MySQL 8 と MariaDB の違い

| 項目 | MySQL 8.x | MariaDB | 出典 |
| --- | --- | --- | --- |
| クエリキャッシュ | 8.0 で削除（無い） | ある | https://mariadb.com/kb/en/system-variable-differences-between-mariadb-109-and-mysql-80 |
| redo log の容量 | 8.0.30 以降 `innodb_redo_log_capacity`（動的） | `innodb_log_file_size`（既定 96MB、10.9 から動的）。`innodb_log_files_in_group` は 10.6 で削除 | MySQL 8.4 の文書、MariaDB の InnoDB 変数一覧 |
| adaptive hash index の既定 | 8.4 で `OFF`（それ以前は `ON`） | 10.5 以降 `OFF` | 同上 |
| `innodb_flush_method` の既定（Unix） | 8.4 の文書は `O_DIRECT`（対応していれば） | 10.6.0 以降 `O_DIRECT`（11.0 で非推奨） | 同上 |
| `innodb_flush_log_at_trx_commit` | 0 / 1 / 2 | 0 / 1 / 2 / 3 | 同上 |
| `innodb_doublewrite` | `ON` / `OFF` / `DETECT_AND_RECOVER` / `DETECT_ONLY`（8.0.30 以降） | 11.0.6 以降 `OFF` / `ON` / `fast` | 同上 |
| JSON 型 | 内部はバイナリ形式 | `LONGTEXT` の別名（CHECK 制約つき）。速度を試す | MariaDB の互換表 |
| 既定の照合順序 | `utf8mb4_0900_ai_ci`（8.0） | 11.4 以降 `utf8mb4_uca1400_ai_ci` | 同上 |
| スレッド処理 | 接続ごとのスレッド（既定） | `thread_handling=pool-of-threads` で、少ない CPU に多数の接続がある短いクエリの負荷に効く。Unix の既定は接続ごと | https://mariadb.com/kb/en/thread-pool-in-mariadb/ |
| `explicit_defaults_for_timestamp` | ON | OFF | 互換表 |

- **効く条件**: 「MariaDB から MySQL 8 へ」「MySQL 5.7 から 8 へ」の入れ替えを考えるとき、または設定の名前が通らないとき。
- **やり方**: 入れ替えは、点が上がる根拠（計測）があるときだけ。同じ設定名が通らない場合は、上の表で名前を確かめる。
- **確かめ方**: 入れ替えの前後で、`EXPLAIN` の実行計画と既定値の差を比べる。
- **危険**: **ISUCON8 予選の復習で MariaDB 5.5 から MySQL 8.0 に替えると点が下がった**（クエリキャッシュの有無・既定値・互換性）。入れ替えはデータの再作成・初期化の時間・再起動後の動作にも響く。8.0 での照合順序の既定の違いで、比較結果・ソート順が変わりうる。
- **出典**: https://kujira16.hateblo.jp/entry/2018/10/22/013611（ISUCON8 予選の復習）、上の表の URL

---

## 7. アプリ側（Go・Rust）の接続

### interpolateParams と プリペアドステートメントの往復

- **効く条件**: スロークエリや MySQL のステータスに `Com_stmt_prepare`・`Com_stmt_execute`・`Com_stmt_close` が大量に出る。MySQL の general log・OTel のトレースで、1 つのクエリが「prepare → execute → close」の 3 往復になっている。DB を別台にしたあとに遅い（往復の遅延が増えた）。
- **やり方**: Go の `go-sql-driver/mysql` は、引数のある `db.Query` / `db.Exec` を既定でサーバー側の prepared statement にして、prepare・execute・close の 3 往復をする。DSN に `interpolateParams=true` を付けると、ドライバがクライアント側で `?` を値に展開し、1 回の通常のクエリとして送る。
  ```go
  dsn := "isucon:isucon@tcp(127.0.0.1:3306)/isucon?parseTime=true&loc=Asia%2FTokyo&charset=utf8mb4&interpolateParams=true"
  ```
  - 明示的な `db.Prepare` と `Stmt.Exec` には影響しない（メンテナの回答）。
  - ISUCON14 の LayerX チーム（4 位）は `interpolateParams=true` を入れた（23,894 点の時点）。
- **確かめ方**: MySQL の `SHOW GLOBAL STATUS LIKE 'Com_stmt%';` の増え方が止まる。往復数（OTel のスパン数）が減る。
- **危険**:
  - **`BIG5`・`CP932`・`GB2312`・`GBK`・`SJIS` の多バイト文字コードとは併用できない**（ドライバの README）。`utf8mb4` なら問題ない。
  - 値の埋め込みはドライバが行う。SQL を文字列連結で組み立てて渡すのとは違い、引数はバインドのまま渡す。**文字列連結に置き換えない**（SQL インジェクションになる）。
  - `multiStatements=true` では、`?` を最初の文にしか使えない（`interpolateParams` を使えば回避）。
  - Rust の sqlx（MySQL）は別。プリペアドステートメントをキャッシュして再利用する作りで、`statement_cache_capacity` で調整する（PostgreSQL 版の説明。MySQL 版の確認は出典未確認）。
- **出典**: https://github.com/go-sql-driver/mysql/blob/master/README.md、https://github.com/go-sql-driver/mysql/issues/413、https://zenn.dev/layerx/articles/3bd55b77e047c4

### コネクションプール（SetMaxOpenConns・SetMaxIdleConns・ConnMaxLifetime）

- **効く条件**: DB の接続の数が多すぎて `Too many connections`、または DB が空いているのにアプリのリクエストが待たされる（プールの取得待ち）。`db.Stats` の `WaitCount`・`WaitDuration` が大きい。ベンチ中に接続の開閉が多い（`Connections` の増え方が速い）。
- **やり方**:
  - `SetMaxOpenConns(n)`: 同時に開く接続の上限（既定は無制限）。上限は DB の `max_connections` から逆算する。上限に達すると取得待ちになる。
  - `SetMaxIdleConns(n)`: プールに残すアイドルの接続（**既定は 2**）。ドライバの README は `SetMaxOpenConns` と同じ値にするよう勧める。小さいと接続を何度も開閉する。
  - `SetConnMaxLifetime(d)`: 接続の最大の寿命（既定は無制限）。ドライバの README は 5 分より短くするよう勧める（間に入るミドルウェアが切るため）。
  - `SetConnMaxIdleTime(d)`: アイドルの接続を早く閉じる。
  ```go
  db.SetMaxOpenConns(50)
  db.SetMaxIdleConns(50)
  db.SetConnMaxLifetime(3 * time.Minute)
  ```
  - Alex Edwards の記事の目安は `SetMaxOpenConns(25)`・`SetMaxIdleConns(25)`・`SetConnMaxLifetime(5*time.Minute)`（小〜中規模の Web アプリの出発点。負荷試験で調整する）。
  - Rust の sqlx: `PoolOptions` の `max_connections`・`min_connections`・`acquire_timeout`・`idle_timeout`・`max_lifetime`・`test_before_acquire`（既定 `true`）。`test_before_acquire` は取得のたびに ping する。切ると往復が減るが、DB の再起動で切れた接続を掴むことがある。
- **危険**:
  - **上限を上げて逆に遅くなることがある**。ロック待ち・DB の CPU がボトルネックなら、接続を増やしても詰まりが増えるだけ。
  - 上限が無いと DB の `max_connections` を超える。`SetMaxOpenConns` の上限は、**全アプリの台数 × プロセス数 × 上限 ≤ `max_connections`**。
  - 上限はセマフォなので、取得待ちのままデッドロックしうる（`database/sql` の文書）。トランザクションを持ったまま別の接続を取る処理は危険。
  - アプリの再起動順と DB の再起動で、切れた接続を使ってしまい 500 になる（ISUCON5 本選の fujiwara）。再起動後に最初のリクエストが通ることを確かめる。
- **出典**: https://go.dev/doc/database/manage-connections、https://www.alexedwards.net/blog/configuring-sqldb、https://github.com/go-sql-driver/mysql/blob/master/README.md、https://docs.rs/sqlx/0.8.6/sqlx/pool/struct.PoolOptions.html

### デッドロックと再試行

- **効く条件**: ベンチのログに `Deadlock found when trying to get lock; try restarting transaction`（エラー 1213）が出る。`Lock wait timeout exceeded`（エラー 1205）が出る。同じ行・同じ範囲を複数の API が更新する。（エラー番号は出典未確認）
- **やり方**: 文書は「デッドロックは危険ではない。**もう一度やる**」と書く。
  1. **アプリで再試行を実装する**（デッドロックで rollback されたトランザクションを最初からやり直す。回数に上限を付ける）。
  2. 原因を調べる: `SHOW ENGINE INNODB STATUS` の LATEST DETECTED DEADLOCK、`innodb_print_all_deadlocks = ON`（頻発するとき、調べ終わったら戻す）。
  3. 減らす: トランザクションを短くする、**ロックを取る順序をそろえる**（複数の表・複数の行は常に同じ順）、適切なインデックスでロックする行を減らす、`FOR UPDATE` を外す、READ COMMITTED にする（節 4）。
  4. どうしても直列にしたいなら、1 行の「セマフォ表」を先に更新する、またはテーブルロック（文書の紹介）。
- **確かめ方**: エラーのログの件数、`SHOW ENGINE INNODB STATUS`。
- **危険**:
  - 再試行は**冪等でないと二重に実行される**（二重課金・二重予約）。外部 API の呼び出しや採番をトランザクションの外に出すと意味が変わる（ISUCON11 予選の）。
  - ロックの範囲だけを狭めても点が変わらないことがある（ISUCON13 の）。待ち時間の内訳を測ってから直す。
  - 再試行の上限を超えたら 500 を返す。競技によってはエラーの数が減点になる。
  - 前回の練習（ISUCON9 本戦の isutrain）では、デッドロック（MySQL のエラー 1213 / 40001）を 500 のまま返して再試行しなかったため、負荷を上げたらキャンセルが失敗して整合性の失格につながった。**初期実装が再試行している箇所は、置き換えた実装でも同じ回数・同じ条件で再試行する**。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html、https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html

### 一括 INSERT・IN 句の取得（アプリ側の書き方）

- **効く条件**: スロークエリの上位が同じ形の `INSERT` の連続、同じ形の `SELECT ... WHERE id = ?` の連続（`06-sql-and-indexes.md` の N+1）。
- **やり方**:
  - Go: `sqlx.In` で `IN (?)` を展開して `db.Rebind` で書き換える。`NamedExec` に構造体の slice を渡すと一括 INSERT になる。
  - Rust（sqlx）: `QueryBuilder::push_values` で一括 INSERT。**バインド変数の上限は MySQL も PostgreSQL も 65535**。行数 = 65535 ÷ 列数 を超えないよう分割する。空の iterator は不正な SQL になる。
  - MySQL の一括 INSERT: 複数行の `INSERT ... VALUES (...), (...)`、`innodb_autoinc_lock_mode = 2`（連続挿入）、主キー順に挿入する、必要なら `unique_checks = 0`・`foreign_key_checks = 0`（重複・参照の不整合が無いと分かっているときだけ）。
- **確かめ方**: クエリの回数と合計時間（slow log・OTel）。
- **危険**: 空のリストで SQL エラー、ID の対応が崩れる（ISUCON12 予選の）。`unique_checks = 0` は重複を検出できなくなる。
- **出典**: https://jmoiron.github.io/sqlx/、https://docs.rs/sqlx/0.8.6/sqlx/struct.QueryBuilder.html、https://dev.mysql.com/doc/refman/8.4/en/optimizing-innodb-bulk-data-loading.html

---

## 8. スキーマと全文検索・JSON

### generated column と関数インデックス（式のインデックス）

- **効く条件**: `WHERE DATE(created_at) = ?`、`WHERE LOWER(name) = ?`、`WHERE JSON_EXTRACT(...) = ?` のように、列に関数をかけた条件が遅い（`EXPLAIN` が `type: ALL`）。
- **やり方**:
  - 関数をかけた列には、そのままではインデックスが効かない（文書の一般論）。まず **列に関数をかけない形に書き直せるか**（`WHERE created_at >= ? AND created_at < ?`）を考える。
  - 書き直せないなら、MySQL 8.0.13 以降の**関数インデックス（式のインデックス）**。式を二重の括弧で囲む。
    ```sql
    CREATE INDEX idx_lower_name ON users ((LOWER(name)));
    ```
    **クエリの式がインデックスの式と同じでないとオプティマイザは使わない**（`SUBSTRING(col1,1,10)` のインデックスは `SUBSTRING(col1,1,9)` の条件では使われない）。
  - generated column（`GENERATED ALWAYS AS (...)`）に二次インデックスを張る。InnoDB は VIRTUAL な列にも二次インデックスを張れ、値がインデックスに実体化される（STORED よりテーブルが小さい）。JSON の列は直接インデックスを張れない。生成列を介して張る。
    ```sql
    ALTER TABLE jemp ADD COLUMN g INT GENERATED ALWAYS AS (c->"$.id"), ADD INDEX i (g);
    ```
  - JSON の `->>` を式のインデックスにすると、`LONGTEXT` が返るのでエラーになる。`CAST` を使うと、照合順序が違ってインデックスが使われないことがある（文書は `COLLATE utf8mb4_bin` を付ける、またはクエリに同じ `CAST` を書く方法を示す）。
  - MariaDB には MySQL 8.0 の「関数インデックス」が無い。生成列（`VIRTUAL`・`PERSISTENT`）＋インデックスで代用する（出典未確認）。
- **確かめ方**: `EXPLAIN` の `key`、`type`。`SHOW WARNINGS` で式が書き換えられた形を見る。
- **危険**:
  - 列の追加・インデックスの追加は書き込みの時間と初期化の時間を増やす（節「スキーマ変更」）。ISUCON10 予選では、インデックス・生成列を足したあと CSV 入稿がタイムアウトし始めて一括 INSERT に変更した。
  - 空間インデックス（GEOMETRY の生成列 + SPATIAL INDEX）は、`EXPLAIN` で使われているか確かめないと逆に下がる（ISUCON10 予選、1232 → 1175 点）。
  - 生成列の値は SQL 側の関数で決まる。アプリの計算と食い違うと整合性が崩れる。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/create-index.html（Functional Key Parts）、https://dev.mysql.com/doc/refman/8.4/en/create-table-secondary-indexes.html

### 全文検索・ngram

- **効く条件**: `LIKE '%語%'`（前方一致でない部分一致）が遅く、検索対象が日本語・中国語・韓国語（CJK）。
- **やり方**: 組み込みの全文検索パーサーは空白で単語を切るので、CJK では使えない。**ngram パーサー**は n 文字ずつに切る。`ngram_token_size`（既定 2、1〜10、起動時だけ）。
  ```sql
  CREATE FULLTEXT INDEX ft_body ON articles (body) WITH PARSER ngram;
  SELECT * FROM articles WHERE MATCH(body) AGAINST('東京' IN BOOLEAN MODE);
  ```
  - 自然言語モードでは検索語が ngram の和になる（"abc" は "ab bc"）。ブール・フレーズでは連続した ngram（"abc" は "ab bc" の連続）になる。
  - `innodb_ft_min_token_size` は ngram には効かない。
  - 文字コードは `utf8mb4`。日本語には MeCab パーサーのプラグインもある。
  - 大量のデータを入れたあとに FULLTEXT を作る（先に `FTS_DOC_ID` 列を作っておく。文書の一括ロードの節）。
- **確かめ方**: `EXPLAIN` の `type: fulltext`。検索結果が `LIKE` の元の結果と一致するか（ngram は語の切り方で一致の意味が変わる）。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/fulltext-search-ngram.html、https://dev.mysql.com/doc/refman/8.4/en/optimizing-innodb-bulk-data-loading.html

### JSON 列

- **効く条件**: JSON の列の中の値で検索・集計していて遅い。
- **やり方**: JSON の列は直接インデックスを張れない。値を取り出す生成列にインデックス（前項）。頻繁に使う値は、JSON の外の通常の列に出す。MariaDB の JSON は `LONGTEXT` の別名なので、速度を試す。
- **確かめ方**: `EXPLAIN`、更新のコスト（JSON の更新は行を書き換える）。
- **危険**: 初期データの JSON と、通常の列の値が食い違うと整合性が崩れる。静的データの書き換えの可否はマニュアルで確かめる。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/create-table-secondary-indexes.html、https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/migrating-to-mariadb/moving-from-mysql/mysql-to-mariadb-compatibility-matrix

### スキーマ変更と initialize の時間

- **効く条件**: インデックス・列を足したい。`/initialize` が競技の上限に近づいた。大きな表への `ALTER TABLE` に時間がかかる。
- **やり方**:
  - **スキーマ・インデックスの変更は initialize で毎回再現する**（`DROP TABLE` + スキーマの SQL を実行する。ISUCON13 の例）。初期データを別のファイル（SQLite など）に持つ問題では、その初期ファイルにもインデックスを入れる（ISUCON12 予選の）。
  - MySQL 8.4 のオンライン DDL: セカンダリインデックスの追加・削除は `INPLACE`・`LOCK=NONE`（読み書きを止めない。ただしテーブルの大きさに比例して時間とディスクがかかる）。列の追加は既定で `INSTANT`。**列の型の変更は `COPY` のみ**（全行コピー・書き込みを止める）。文字コードの変更はエンコードが変わると再構築。主キーの追加・変更は全行のコピー。
    ```sql
    ALTER TABLE t1 ADD INDEX idx_c (c), ALGORITHM=INPLACE, LOCK=NONE;  -- 速くできない方法なら失敗させる
    ```
  - 大量のデータを入れるなら、**表を作ってからデータを入れ、そのあとにインデックスを作る**（文書）。主キーは最初から付ける（あとからは全行のコピー）。
  - 初期データの再構築が重いなら、ダンプの復元・別の表からの `CREATE TABLE ... SELECT`（インデックス・制約が落ちるので DDL で補う）に替える。
  - `unique_checks = 0`・`foreign_key_checks = 0`・`autocommit = 0` は一括読み込みの定番（`INSERT` の連続のあとで戻す）。
- **確かめ方**: `/initialize` の応答時間を、変更のたびに 3 回測る（初回は遅いことがある）。SQL ごとの時間（slow log）。
- **危険**:
  - **initialize が上限を超えると失格になりうる**。
  - **インデックスの追加で書き込みと一括入稿が遅くなる**（ISUCON10 予選 、ISUCON12 でインデックスが逆に下げた）。
  - 全 SQLite を読み直す初期化は 33 秒でタイムアウトした。上限ぎりぎりの単発測定を採用の条件にしない。
  - 移行で主キーを落とすと 800 点 → 3,657 点に戻るほど遅くなる。DDL の移植漏れを、インデックス・一意制約・列型の比較で確かめる。
  - 初期化のあとのデータは再起動後も残ること。すべての保存先（MySQL と Redis など）を初期化で戻す。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-operations.html、https://dev.mysql.com/doc/refman/8.4/en/optimizing-innodb-bulk-data-loading.html

---

## 9. MySQL 自体を別の台へ移す

- **効く条件**: アプリと MySQL が同じ台で CPU とメモリを取り合っている（`top` で mysqld とアプリの CPU が合計で飽和、`free -m` でメモリが足りない）。台が複数あり、DB の台が遊んでいる。
- **やり方**:
  1. DB 用の台を決める（メモリの多い台。ほかの台の CPU が余っているなら、その台を DB にする）。
  2. `bind-address = 0.0.0.0`（または専用の IP）、`'isucon'@'%'` のユーザーを作り権限を付与（`skip_name_resolve` を使うなら IP か `%` で）。
  3. アプリの DB ホストの設定（環境変数ファイル）を書き換える。
  4. アプリ側が DB へ起動時に再接続できるようにする（起動順序が保証されない再起動の試験の対策）。
  5. `interpolateParams=true`（節 7）、プールの上限（節 7）、`max_connections`（節 4）を同時に見直す。
  - **ISUCON13 の p1ass（釜中の鯖）は、開始 10:51 に MySQL を別サーバーに分けた（5,333 点）。「アプリと MySQL を最初から分ける」がそのチームの定番**。ISUCON14 の LayerX は DB を別インスタンスに逃がして 10,221 点に到達した。
- **確かめ方**: DB の台と他の台の CPU・メモリ、アプリの応答時間、DB のスロークエリ。**往復が増えるので N+1 が目立つようになる**（ISUCON12 予選で SQLite を MySQL に替えようとして N+1 が顕在化した例）。
- **危険**:
  - **ISUCON14 では、DB を別サーバーに移したら notifications の負荷が上がり、スコアが 10,000 点台と 0 を行き来した例**（catatsuy。最終は速度を落として安定させ 9,884 点）。別の参加記でも、DB の分離後にエラーが出て一度元に戻している（kawaemon）。
  - ネットワークの遅延 × クエリの数が効く。クエリの数を減らす（N+1 の解消・`interpolateParams`・一括取得）のが先。
  - 競技のルールで、台どうしの通信・別のサーバーへの処理の委譲が禁じられていないかをマニュアルで確かめる。
  - 再起動したとき、DB の台が先に上がる保証が無い。アプリの再接続を確かめる。
  - 表単位で別の台に分ける案（ISUCON10 予選の）、アプリが 2 台の DB に並列に書いて読みを分散する案は、**表をまたぐ処理・初期化・採番・二重書き込みの整合性**が崩れやすい。
- **出典**: https://blog.p1ass.com/posts/isucon13/、https://zenn.dev/layerx/articles/3bd55b77e047c4、https://zenn.dev/catatsuy/articles/9c22150945c8dc、https://zenn.dev/kawaemon/articles/f40529e0c447f5、https://dev.mysql.com/doc/refman/8.4/en/host-cache.html、手元の定石集 

---

## 10. 計測（MySQL まわりの最小限）

### スロークエリログと EXPLAIN ANALYZE

- **効く条件**: どのクエリが遅いか分からない。
- **やり方**:
  ```sql
  SET GLOBAL slow_query_log = 1;
  SET GLOBAL long_query_time = 0;          -- 1 走行だけ全部取る
  SET GLOBAL log_queries_not_using_indexes = 1;  -- インデックスを使わないものも
  ```
  ログは `mysqldumpslow` で要約できる（`pt-query-digest` の使い方は[01 計測](01-measurement.md)）。個別のクエリは `EXPLAIN ANALYZE`（8.0.18 以降。TREE 形式。**文を実行する**ので `UPDATE`・`DELETE` は `BEGIN`〜`ROLLBACK` で囲む）。
- **確かめ方**: ログの上位のクエリの合計時間、`rows_examined`。
- **危険**: `long_query_time = 0` のログは大きく、ログを書くコストが測定に入る。計測が終わったら戻す。ログのファイルに個人情報・出題内容が入る。外に出さない。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/slow-query-log.html、https://dev.mysql.com/doc/refman/8.4/en/explain.html

---

## よくある失敗

| 失敗 | 何が起きるか | 避け方 |
| --- | --- | --- |
| `innodb_flush_log_at_trx_commit = 0/2` や `innodb_doublewrite = OFF` を、再起動の試験の方法を読まずに入れる | 強制停止・電源断で直近のデータが消える・ページが壊れる | マニュアルで再起動の方法を確かめ、同じ方法で自分たちで試す。迷ったら入れない |
| `skip-log-bin` を、複製を使う構成・GTID の異常終了の後で入れる | 複製が壊れる | 複製を使わないと決めてから。再起動して `log_bin` を確かめる |
| MySQLTuner などの推奨値を丸ごと入れる | メモリ不足で mysqld が落ちる（ISUCON8 予選） | 同居するプロセスのメモリを残す。1 つずつ入れ、`free -m` を見る |
| プール・`max_connections` を上げれば速くなると思う | ロック待ちが増え、逆に遅くなる | 取得待ちの実測（`db.Stats`・OTel）から決め、段階的に比べる |
| MariaDB から MySQL 8 に（またはその逆）替える | クエリキャッシュ・既定値・計画が変わって下がる（ISUCON8 の復習） | 根拠がないと替えない。替えるなら前後で `EXPLAIN` と既定値を比べる |
| 列の文字コード違いのまま JOIN する | インデックスが効かず全件走査 | `SHOW CREATE TABLE` で揃える |
| インデックス・生成列を足して、入稿・初期化が遅くなる | `/initialize` や CSV 入稿がタイムアウト | 追加のたびに `/initialize` の時間を測る |
| DB を別台に移して、N+1 の往復が効いて遅くなる | スコアが乱高下・0（ISUCON14） | 往復を減らしてから移す。移したあとで再起動を試す |
| デッドロックの再試行で二重に実行する | 二重課金・二重予約 | 冪等にする。外部 API はトランザクションの外で結果を使う設計にする（互換性の確認） |

---

## 出典一覧

公式ドキュメント

- https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-redo-log.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-doublewrite-buffer.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool-resize.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-preload-buffer-pool.html
- https://dev.mysql.com/doc/refman/8.4/en/replication-options-binary-log.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-operations.html
- https://dev.mysql.com/doc/refman/8.4/en/optimizing-innodb-bulk-data-loading.html
- https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html
- https://dev.mysql.com/doc/refman/8.4/en/create-index.html
- https://dev.mysql.com/doc/refman/8.4/en/create-table-secondary-indexes.html
- https://dev.mysql.com/doc/refman/8.4/en/fulltext-search-ngram.html
- https://dev.mysql.com/doc/refman/8.4/en/too-many-connections.html
- https://dev.mysql.com/doc/refman/8.4/en/host-cache.html
- https://dev.mysql.com/doc/refman/8.4/en/table-cache.html
- https://dev.mysql.com/doc/refman/8.4/en/performance-schema-startup-configuration.html
- https://dev.mysql.com/doc/refman/8.4/en/slow-query-log.html
- https://dev.mysql.com/doc/refman/8.4/en/explain.html
- https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables
- https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/migrating-to-mariadb/moving-from-mysql/mysql-to-mariadb-compatibility-matrix
- https://mariadb.com/kb/en/thread-pool-in-mariadb/
- https://mariadb.com/kb/en/system-variable-differences-between-mariadb-109-and-mysql-80（検索結果の要約のみ確認。本文は読んでいない）
- https://go.dev/doc/database/manage-connections
- https://github.com/go-sql-driver/mysql/blob/master/README.md
- https://github.com/go-sql-driver/mysql/issues/413
- https://jmoiron.github.io/sqlx/
- https://docs.rs/sqlx/0.8.6/sqlx/pool/struct.PoolOptions.html
- https://docs.rs/sqlx/0.8.6/sqlx/struct.QueryBuilder.html
- https://www.alexedwards.net/blog/configuring-sqldb

ISUCON の記録

- https://isucon.net/archives/57993937.html（ISUCON13 結果発表。fail の理由）
- https://isucon.net/archives/56842718.html（ISUCON12 予選 作問者の解説）
- https://blog.p1ass.com/posts/isucon13/
- https://diary.sorah.jp/2023/11/28/isucon13
- https://zenn.dev/catatsuy/articles/9c22150945c8dc（ISUCON14）
- https://zenn.dev/layerx/articles/3bd55b77e047c4（ISUCON14 4 位）
- https://zenn.dev/kawaemon/articles/f40529e0c447f5（ISUCON14）
- https://zenn.dev/dice801/articles/ebc027207257b1（ISUCON12、検索結果の要約のみ）
- https://kazeburo.hatenablog.com/entry/2014/10/14/170129（ISUCON4 の事後練習）
- https://hikalium.hatenablog.jp/entry/2018/09/16/235250（ISUCON8 予選）
- https://kujira16.hateblo.jp/entry/2018/10/22/013611（ISUCON8 予選の復習）
- https://sfujiwara.hatenablog.com/entry/isucon11-final（ISUCON11 本選）

手元の資料

- 書籍『達人が教えるWebパフォーマンスチューニング』第 5 章「データベースのチューニング」（目次のみ確認: 5-2 データベースの負荷を測る、5-3 インデックスでデータベースを速くする、5-4 N+1とは、5-5 データベースとリソースを効率的に利用する）。https://gihyo.jp/book/2022/978-4-297-12846-3
