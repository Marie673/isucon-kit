# 06 SQL とインデックス

**競技のルールは毎回違う。当日のマニュアルが優先する。** SQL を書き換える変更は、応答の中身・順序・件数が変わると整合性のチェックで失格になりうる。この章の「危険」はそこを中心に書いた。

## この章で扱う範囲

- DB の種類（MySQL・MariaDB・PostgreSQL・SQLite）に依らない SQL の書き方・インデックスの設計・ロックとトランザクションの使い方。
- 種類ごとの設定は `04-mysql.md`・`05-postgresql.md`。計測の入れ方（slow log・OTel・pprof）は[01 計測](01-measurement.md)。キャッシュ・メモリへの持ち上げは[08 キャッシュとインメモリ化](08-cache-and-inmemory.md)。

## 最初に見るもの（当日の 5 分チェックリスト）

1. **計測してから直す**。推測で SQL を書き換えない。スロークエリ（MySQL は slow log、PostgreSQL は `pg_stat_statements`）の**合計時間（回数 × 平均）の上位**から見る。1 回が遅いものより、**1 回は速いが回数が多いもの**が上位に出ることが多い。
2. 上位の SQL の**回数**を見る。1 リクエストで同じ形の SQL が何十回も出ていれば N+1（節 5）。
3. 上位の SQL を `EXPLAIN` する（節 1）。`type: ALL`（MySQL）・`Seq Scan`（PostgreSQL）、`Using filesort`・`Using temporary`、`rows` が大きい。
4. インデックスの有無・列順（節 2）、`SELECT *`（節 10）、`OFFSET` が大きい（節 9）、`COUNT(*)`（節 8）。
5. 書き込み: 1 件ずつの INSERT の連続（節 7）、`FOR UPDATE` の範囲（節 12）、トランザクションの長さ（節 13）、デッドロックのログ（節 14）。
6. **往復の費用を見積もる**: アプリと DB の往復時間（同じ台なら 0.1〜0.5ms、別の台なら数 ms、リージョンをまたぐと 100ms）。往復が安いなら、文の本数を減らしても効かない（節 5）。
7. 変更ごとに `EXPLAIN` の改善と**ベンチの点**の両方を見る。`EXPLAIN` が良くなっても点が上がらないことがある（節 0）。

---

## 0. 測り方の注意（EXPLAIN の改善 ≠ 点数）

- **効く条件**: SQL を速くしたのに点が上がらない・下がる。
- **やり方**: 変更ごとに、① その SQL の時間（`EXPLAIN ANALYZE`・pg_stat_statements・slow log）、② ベンチの点と、配点の大きい API の成功件数、③ 書き込みの時間、を並べる。
  - ISUCON11 本選・予選の acidlemon: 最新状態を集計して JOIN するクエリで `EXPLAIN` は良くなったが、trend 全体の点が増えず不採用。
  - ISUCON13 本番の saza: DNS 用の MySQL にインデックスを足すと DB の負荷は下がったが総得点も下がった（原因は著者の推測で未確定）。
  - ISUCON9 予選: N+1 を 4N+1 から 2N+1 に減らしても 3,500 → 3,400 点で伸びなかった。
  - ISUCON12 予選: `player_score(row_num DESC)` のインデックスを足したら約 1,000 点下がり不採用。
- **危険**: 速くしたことで並列数が増え、配点の高い API が回らなくなる可能性（ISUCON12 の作問者の説明は仮説）。
- **出典**: https://isucon.net/archives/56842718.html

---

## 1. EXPLAIN の読み方

### MySQL（`EXPLAIN`・`EXPLAIN ANALYZE`）

- **効く条件**: 1 つの SQL が遅い理由を知りたい。
- **やり方**: `EXPLAIN SELECT ...;`。見る列:
  - `type`（良い順）: `system` > `const`（主キー・UNIQUE を定数で）> `eq_ref`（JOIN で主キー・UNIQUE NOT NULL）> `ref`（非ユニークのインデックスで等値）> `range` > `index`（インデックス全体の走査）> `ALL`（表の全件走査）。
  - `key`: 実際に使ったインデックス（NULL なら使っていない）。`key_len`: 使った長さ（複合インデックスのどこまで使えたか。NULL 可の列は +1）。
  - `rows`: 読む行数の推定。`filtered`: 絞り込み後に残る割合の推定。
  - `Extra`: **`Using index`**（カバリング。表を読まない）、`Using index condition`（インデックスの条件プッシュダウン）、**`Using filesort`**（追加の並べ替え。調べる）、**`Using temporary`**（一時テーブル。調べる）、`Using where`、`Using join buffer`。
  - `EXPLAIN ANALYZE`（8.0.18 以降。TREE 形式のみ）は**文を実行**して、実際の時間・行数・ループ数を出す。`UPDATE`・`DELETE` は `BEGIN` … `ROLLBACK` で囲む。
- **確かめ方**: 変更の前後で `type`・`key`・`rows`・`Extra`。
- **危険**: `rows` は推定（InnoDB）。統計が古いと外れる。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/explain-output.html、https://dev.mysql.com/doc/refman/8.4/en/explain.html

### PostgreSQL（`EXPLAIN (ANALYZE, BUFFERS)`）

- **やり方**: `EXPLAIN (ANALYZE, BUFFERS) SELECT ...;`。読み方は `05-postgresql.md` の「EXPLAIN (ANALYZE, BUFFERS) の読み方」。要点: **推定行数と実際の行数の差**、`loops` を掛けて合計を出す、`shared hit` / `read`、`Seq Scan`・`Index Scan`・`Bitmap Heap Scan`・`Index Only Scan`（`Heap Fetches`）、`Rows Removed by Filter`。書き込みの文は `BEGIN` … `ROLLBACK`。
- **危険**: **2 回目は暖まっている**。cold と warm を区別する。
- **出典**: https://www.postgresql.org/docs/16/using-explain.html

---

## 2. 複合インデックスの列順

- **効く条件**: WHERE に複数の列があり、単独のインデックスがそれぞれ別に張ってある（`index_merge`）。`EXPLAIN` の `key_len` が短い（複合インデックスの先頭しか使えていない）。`WHERE a = ? ORDER BY b LIMIT n` が filesort。
- **やり方**:
  - **使えるのは先頭からの部分（左端の接頭辞）だけ**。`(col1, col2, col3)` なら `(col1)`・`(col1, col2)`・`(col1, col2, col3)` で引ける。`col2` だけの検索には使えない（電話帳の例: 姓・名の順に並んでいる）。
  - 順序の定石: **「等価条件の列 → 範囲条件・ORDER BY の列」**。例: `WHERE user_id = ? ORDER BY created_at DESC LIMIT 1` → `INDEX(user_id, created_at)`（手元の は ISUCON11 予選の同じ形で filesort を解消した）。
  - 実例: ISUCON5 予選で、友人関係の「one OR another」の両側に `created_at` を含むインデックスを足して約 120ms → 7ms 台。ISUCON10 予選の `(rent, id)`、ISUCON12 予選の `player_score(player_id, competition_id)`（片列だけの追加では点が下がった）。ランキングは点数とタイブレーク列まで含める `(competition_id, score DESC, row_num)`。
  - 1 つの順序の良いインデックスは、2 つ目のインデックスより良い（保存量と INSERT・UPDATE・DELETE の費用が減る）。
  - **主キーの列順も効く**: ISUCON11 本選で未読テーブルの主キーを `(user_id, announcement_id)` の順にし、重複する `user_id` のインデックスを削除した。InnoDB はクラスタインデックスなので、主キー順で物理的に並ぶ。価格順の範囲アクセスが多い表の主キーを価格先頭にした例もある。
  - 同じ SQL でも**アクセスの形が両方向**（`WHERE a = ? AND b = ?` と `WHERE b = ?`）なら、2 本を張るか、片方を許容するか決める。
- **確かめ方**: `EXPLAIN` の `key`・`key_len`・`rows`・`Extra`（`Using filesort` が消えるか）。ベンチの点と、**書き込み（INSERT・UPDATE）の時間**。
- **危険**:
  - **インデックスを足すと書き込みが遅くなり、初期化・一括入稿の時間が増える**（ISUCON10 予選: インデックスと生成列の追加後に CSV 入稿がタイムアウトし始め、一括 INSERT にした）。
  - 使われないインデックスを足すと下がる。**インデックスが存在することと使われることは別**。`EXPLAIN` で使用を確かめる。
  - **インデックスを足しても使われない型の不一致・関数・文字コードの違い**（節 16・`04-mysql.md` の文字コード）。
  - 初期データをコピーして使う問題（SQLite のファイルなど）では、初期ファイルの中にもインデックスを入れる。変更が許されるかをマニュアルで確かめる。
  - **インデックスの削除は慎重に**。短い観測窓で `idx_scan = 0` でも、一意性・外部キー・書き込み時の価値は否定できない。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html、https://use-the-index-luke.com/sql/where-clause/the-equals-operator/concatenated-keys

---

## 3. カバリングインデックス

- **効く条件**: `EXPLAIN` の `rows` は少ないのに遅い。インデックスで行を特定したあと、表（ヒープ・クラスタインデックス）を読む費用が大きい。選ぶ列が少数。
- **やり方**:
  - MySQL: クエリが使う列がすべてインデックスに入っていれば `Using index`（表を読まない）。InnoDB の二次インデックスは主キーを含むので、主キーの列は足さなくてよい。ISUCON12 予選の作問者の解説は、`visit_history (tenant_id, competition_id, player_id, created_at)` のように `created_at` を足してカバリングにし、クラスタインデックスの読みを避けた。
  - PostgreSQL: `CREATE INDEX ... ON t (x) INCLUDE (y);`（index-only scan）。**可視性マップが立っているページだけ**ヒープを読まずに済む（`VACUUM` が更新する。`05-postgresql.md` の節 7・8）。
- **確かめ方**: MySQL は `Extra: Using index`、PostgreSQL は `Index Only Scan` と小さい `Heap Fetches`。
- **危険**: 列を足すほどインデックスが大きくなり、書き込みが遅くなる。**PostgreSQL では、更新が多い表では可視性マップが立たず効かない**（文書: 表の変更が遅いときだけ意味がある）。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html、https://www.postgresql.org/docs/16/indexes-index-only-scans.html、https://isucon.net/archives/56842718.html

---

## 4. ORDER BY ... LIMIT とインデックス

- **効く条件**: `EXPLAIN` に `Using filesort`（MySQL）・`Sort`（PostgreSQL）があり、`LIMIT` が小さいのに多くの行を読んで並べている。「最新の 1 件」「新着 20 件」「ランキング上位 N」。
- **やり方**:
  - **インデックスが ORDER BY を満たせば並べ替えが要らず、`LIMIT n` で n 行読んだら止まる**（MySQL の文書）。条件: 等価条件の列を先頭に、並べ替える列を次に置く（`WHERE key_part1 = c ORDER BY key_part2` は `(key_part1, key_part2)` で足りる）。
  - **満たせない場合**（MySQL の文書）: 並べる列が別々のインデックスにある、インデックスの非連続な部分、式（`ORDER BY ABS(key)`）、`ORDER BY` と `GROUP BY` の式が違う、複数表の JOIN で並べる列が最初の表のものでない、列の別名が元の列を隠している。
  - 向きが混在（`ORDER BY a DESC, b ASC`）: MySQL 8.0 以降は降順のインデックスを張れる（`INDEX (a DESC, b ASC)`）。インデックスの向きが「同種（すべて同じ向きか、すべて逆）」なら後ろ向きの走査で使える。PostgreSQL も混在向きのインデックスを張れる（出典未確認）。
  - **同順位の順序を決める**: 同じ値の行は順序が不定で、`LIMIT` の有無でも変わりうる。**一意な列を `ORDER BY` の最後に足す**（MySQL の文書）。ページ境界・ランキングで必須。
  - MySQL の `filesort` は `LIMIT` があればメモリ内で済むことがある。それでも全件を読む。
  - `ORDER BY RAND LIMIT 1` を 2 回の SQL に分けても、往復が増えて速くならなかった例がある（ISUCON8 予選）。
- **確かめ方**: `Extra` に `Using filesort` が無い。`EXPLAIN ANALYZE` で読んだ行数が `LIMIT` 付近。
- **危険**:
  - **ORDER BY の列を替えると結果が変わる**。ISUCON3 予選の練習では、`created_at` 順を `id` 順に替えて 1,000 点未満に落ちて戻した（公開条件との複合インデックス `(is_private, created_at)` が正しい候補だった）。**同じ順序になる保証と、WHERE 条件も見る**。
  - 同順位の順序が変わる（タイブレーク列が無い）と、ページの境界・ランキングの順位が変わる。
  - 取得後フィルターの前に LIMIT を掛けると、結果が変わる（ISUCON11 予選: LIMIT を足したら後段の深刻度フィルタと食い違い、`level` 列を足して先に絞る形に直した）。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/order-by-optimization.html、https://dev.mysql.com/doc/refman/8.4/en/limit-optimization.html

---

## 5. N+1 の見つけ方と、往復の費用

### 見つけ方

- **効く条件**: 1 つの API のレスポンスの時間が、一覧の件数に比例して増える。DB の CPU は低いのに、DB 待ちが長い。
- **やり方**:
  - **回数で見る**: スロークエリの集計（`pt-query-digest`・pg_stat_statements の `calls`）で、**同じ形の SQL が 1 リクエストで何十回も**呼ばれている。合計時間の上位に「1 回は速い SQL」が並ぶ。
  - **トレース（OTel）で見る**: 1 リクエストの中の SQL のスパンが連なっている。ISUCON13 の p1ass は、トレースで `fillLivestreamResponse` / `fillUserResponse` が繰り返し呼ばれているのを見て、複数の ID を取る版に書き直した（11:35、7,993 点）。
  - **呼び出し元をコメントで SQL に付ける**: ISUCON14 の LayerX は `database/sql` のドライバをラップして、呼び出した関数名・ファイルを SQL にコメントとして付け、スロークエリのログから辿れるようにした。
  - **コードを読む**: ループの中の `db.Get`・`db.Query`・`Preload`・関連のアクセス（ORM の遅延ロード）。
  - **一部を直しただけで終わらない**: ISUCON13 の p1ass は、統計の N+1 を 15:59 に一部（35,046 点）、17:09 に残り（65,663 → 143,119 点）と**スロークエリの上位に残る限り再計測して直した**。
- **確かめ方**: 1 リクエストあたりの SQL の本数（トレースの子スパンの数）、API の平均時間。
- **出典**: https://blog.p1ass.com/posts/isucon13/、https://zenn.dev/layerx/articles/3bd55b77e047c4

### 往復の費用を見積もる（文を減らしても効かない場合がある）

- **効く条件**: N+1 を直したのに点が上がらない。
- **確かめ方**: トレースのスパンの合計時間と、SQL の実行時間の合計の差（往復と待ち）。
- **危険**: クエリ数だけを見て判断しない。

---

## 6. N+1 の直し方（IN でまとめる・JOIN・事前取得）

- **効く条件**: 節 5 で N+1 が見つかった。
- **やり方**:

  | 関連の形 | 第一候補 | 注意 |
  | --- | --- | --- |
  | 親 1 行に関連が最大 1 行 | `LEFT JOIN`（欠損した親も残す。関連の条件は `ON` に置く） | `WHERE` に置くと欠損した親が消える |
  | 関連が 1 対多で、一覧の `LIMIT` と順序が大事 | 親のページを先に確定 → 親 ID の集合で関連を `IN` で一括取得 → ID ごとにまとめ、親の元の順で組み立てる | `IN` の返却順を信用しない。JOIN の後の `LIMIT` は親が減る |
  | 件数・存在だけ必要 | `GROUP BY`・条件付き集計・`EXISTS` | 1 つの JOIN で関連同士が掛け合わされて件数が増える場合は、関連ごとに集計してから結ぶ |
  | 関連を何度も引く | **事前取得**（リクエストの最初に全部を 1 回取ってマップで引く。マスターは起動時に 1 回） | キャッシュの鮮度（[08 キャッシュとインメモリ化](08-cache-and-inmemory.md)） |

  - Go: `sqlx.In("... WHERE id IN (?)", ids)` を `db.Rebind` で書き換える。Rust（sqlx）: `QueryBuilder` の `push_bind` を並べる、PostgreSQL なら `= ANY($1)` に配列を渡す。**バインド変数の上限は 65535**（MySQL も PostgreSQL も）。上限を超えるなら分割。
  - ISUCON13 の実例: `fillLivestreamResponse` のタグを `JOIN`、複数配信を `IN` で一括、NG ワードの削除を条件付き `DELETE`、未読の登録を `INSERT ... SELECT`（ISUCON11 本選）。
  - `RowsAffected` を使って、直前の SELECT を省く。`LastInsertId` を使って、採番の確認 SELECT を省く。
- **確かめ方**: 1 リクエストの SQL の本数、API の時間。**応答の JSON が元と一致すること**（`diff`・互換テスト）。
- **危険**:
  - **応答を再現できなくなる**。ISUCON14 の catatsuy は `ownerGetSales` の N+1 を解こうとして、レスポンスを元と同じにできず数時間を使って断念した。小さなデータで、**関連が 0 件・1 件・複数、NULL、同順位、ページの境界、集計の期間境界**を、旧実装と比べる。
  - 空のリストで `IN ` や `INSERT ... VALUES` が構文エラーになる。空・重複・要求順の再構築を扱う。
  - **まとめれば必ず速いとは限らない**。ISUCON5 予選で、Redis の個別 GET を `WHERE IN` にまとめたら遅い懸念で戻した。ヒット率・往復数・転送量を比べる。
  - 親と関連を別の SQL で読む場合、同じ時点の参照が必要なら同じスナップショットで読む（接続プールから別々の接続を借りるだけでは、同じ世代の保証にならない）。
  - **一括の `INSERT` で通る経路と通らない経路がある**（ISUCON12 予選で、プレーヤー登録の一括挿入は失敗し、スコア行は通った。ID と挿入行の対応を確かめる）。
- **出典**: https://jmoiron.github.io/sqlx/、https://docs.rs/sqlx/0.8.6/sqlx/struct.QueryBuilder.html

---

## 7. 一括 INSERT・バルク UPDATE・UPSERT

- **効く条件**: スロークエリの上位が、同じ形の INSERT・UPDATE の連続。コミットの回数が多い。CSV の入稿・初期化が遅い。
- **やり方**:
  - **1 件ずつの INSERT を、複数行の `INSERT ... VALUES (...), (...)` にする**。MySQL: 自動コミットを切る・まとめて 1 トランザクション、`innodb_autoinc_lock_mode = 2`、主キー順に挿入、必要なら `unique_checks = 0`・`foreign_key_checks = 0`（重複・参照の不整合が無いと分かっているときだけ）（文書）。PostgreSQL: `COPY`（`05-postgresql.md` の節 11）。
  - **UPSERT**: MySQL は `INSERT ... ON DUPLICATE KEY UPDATE`、PostgreSQL は `INSERT ... ON CONFLICT (col) DO UPDATE SET x = EXCLUDED.x`（原子的。1 つの文が同じ行を 2 回変えるとエラー。衝突しなくても対象の行をロックする）。ISUCON12 予選の作問者の解説は、`INSERT ... ON DUPLICATE KEY UPDATE` と `VALUES` で、ランキング表の更新を一括挿入と合わせた。
  - バルクの `UPDATE`: 条件で絞って 1 文にする（`UPDATE ... WHERE id IN (...)`）。行ごとに値が違うなら `CASE` や JOIN での更新（これらは出典未確認。 には実例が少ない）。
  - **条件付き UPDATE で原子的に判定する**: 在庫確認 → 更新を `UPDATE ... WHERE stock > 0` の 1 文にして、更新件数で成否を判定する（ISUCON10 予選の購入）。
  - **更新対象の検索から、不要な JOIN を外す**（ロックの範囲が小さくなる。ISUCON11 本選）。
- **確かめ方**: SQL の本数・COMMIT の回数、書き込み API の時間、`/initialize` の時間。
- **危険**:
  - **二重購入・二重課金・更新件数 0 の扱い**を保つ（条件付き UPDATE にしたあとも、同時実行で整合が崩れないか並列で試す）。
  - **`RowsAffected` は、変更された行数と一致した行数で違う設定がある**（MySQL の `clientFoundRows`。ドライバと DB の設定を確認。 の注意）。
  - 一括 INSERT の途中失敗の巻き戻し、空リスト、`ON CONFLICT` の同一行の重複。
  - UPSERT の `VALUES` は MySQL 8.0.20 以降で非推奨の方向（出典未確認）。
  - 行数が減らない前提の UPSERT は、削除があるなら置換をトランザクションにする。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/optimizing-innodb-bulk-data-loading.html、https://www.postgresql.org/docs/16/sql-insert.html、https://www.postgresql.org/docs/16/populate.html、https://github.com/go-sql-driver/mysql/blob/master/README.md（`clientFoundRows`）、https://isucon.net/archives/56842718.html

---

## 8. COUNT の高速化（カウンタ列）

- **効く条件**: スロークエリの上位に `SELECT COUNT(*) FROM ... WHERE ...`。検索結果の総件数・未読数・残席数を毎回数えている。
- **やり方**:
  1. **まず COUNT の SQL を軽くする**: 不要な JOIN・全列の取得・並べ替えを外し、条件を評価できる細いインデックスを使う。元の JOIN が存在条件・重複除去を担うなら残す。
  2. **存在判定や件数が小さければ `COUNT` でなく `EXISTS`・件数の上限つき**（`SELECT 1 ... LIMIT 1`）。全列を取る `SELECT *` を `COUNT(*)` にするだけで済む場合もある（ISUCON10 予選の）。
  3. **カウンタ列・集計表を持つ**: 件数が少数の条件に集約でき、更新点を網羅できるなら、**元データと同じトランザクションで加減算する**。実例: 予約の残数を専用表で差分更新（ISUCON8 予選の復習）、ISUCON13 の sorah は `users` と `livestreams` に `total_tips`・`total_reactions`・`score` 列を持った、全 191,847 件でなく約 15,000 件の有効予約だけの表を作った（ISUCON8 予選）。
  4. PostgreSQL: `count(*)` は MVCC のため、条件のない全件の数え上げは全行を見る。`Index Only Scan` が使えれば速くなる。**推定件数**という手もあるが、厳密な件数が要る場合は使えない。
- **確かめ方**: COUNT の時間（`EXPLAIN ANALYZE`）、ページ取得の時間（**別々に測る**）。
- **危険**:
  - **任意の条件の厳密な COUNT を、近似・古い TTL の値に置き換えない**（検索条件の組み合わせが任意なら、カウンタは作れない）。ISUCON10 予選では、検索行と COUNT を JOIN した 1 クエリ化で、ブラウザでは動いたがベンチで不正データと判定されて 0 点になった（空のページで件数を受け取れなかった可能性は整理者の案。原因は未確定）。COUNT の並行実行でも通らなかった。
  - カウンタは**更新漏れ・競合・ロールバック・再初期化**で狂う。初期データの集計値を復元し、再集計でずれを検知できるようにする。
  - 件数だけをメモリ化しても点はあまり上がらなかった例。
- **出典**: https://wiki.postgresql.org/wiki/Slow_Counting

---

## 9. OFFSET からキーセットページングへ

- **効く条件**: `LIMIT n OFFSET m` の `m` が大きいページが遅い。`EXPLAIN ANALYZE` で、捨てる行を読んでいる。
- **やり方**: OFFSET は「何行スキップするか」だけを渡すので、**スキップする行も取得・並べ替えが要る**（ページが深いほど遅い）。キーセット（seek）は、**直前に返した行のキーより後ろだけを選ぶ**。
  ```sql
  -- 人気の降順・id の昇順で、直前の行 (p, i) より後
  SELECT ... WHERE popularity < :p OR (popularity = :p AND id > :i)
  ORDER BY popularity DESC, id ASC LIMIT 20;
  ```
  - 条件: **決まった ORDER BY と、一意なタイブレーク列と、それを支えるインデックス**。
  - 任意のページ番号が必須なら、`id` と並びのキーだけを `OFFSET`/`LIMIT` で選び、その ID の本文を取って選んだ順で返す（本文の読み込みを減らす案。「整理者の検証案」で、この収録の実例に成功例は無い）。
- **確かめ方**: 深いページの時間、`EXPLAIN` の読む行数。同順位の複数 ID・0 件・範囲外のページ・深いページで、旧実装と「件数・行集合・配列順」を比べる。
- **危険**:
  - **ページ API の意味が変わる**（任意のページ番号に飛べない）。`OFFSET` 指定の互換が必須なら使えない。カーソル化で API の意味を変えて逃げない。
  - 並行更新中のページの継続の契約を保つ。NULL の順序・型・照合順序が違うと述語を別に設計する。
  - 二段階の本文取得は、キー取得と本文取得の間の更新で世代が混ざる。
- **出典**: https://use-the-index-luke.com/no-offset

---

## 10. SELECT * をやめる

- **効く条件**: 一覧で必要なのは数列なのに、全列（本文・JSON・BLOB）を取っている。転送量・メモリ・デコードの CPU が大きい。カバリングにしたい（節 3）。
- **やり方**: 必要な列だけを書く。本文と一覧用の列を**別の列・別の表に分ける**（ISUCON5 予選で日記のタイトルと本文を別の列にして、一覧で本文を取らなかった）。存在確認なら全列でなく `COUNT`・`EXISTS`。
- **確かめ方**: 応答までの時間、DB の転送量（`Bytes_sent`）、アプリのメモリ。
- **危険**: 列を分けたら、**登録・更新の全経路・初期データの移行**で追従する（ISUCON5 の例でも、登録側の title 書き込み漏れを直した）。ORM のモデルが全列を前提にしていることがある。

---

## 11. 大きな列（BLOB・画像）を分ける

- **効く条件**: 画像・大きなバイナリを DB の列に持ち、読み出しが多い。DB の CPU・ネットワーク・バッファプール／shared_buffers が画像で埋まる。複製（PostgreSQL の同期レプリケーション）があり、WAL に画像が載って commit が遅い。
- **やり方**:
  - 画像を **DB からファイルへ移し、nginx などで配信する**（ISUCON11 予選。ISUCON13 でもアイコンをファイルに書き出して nginx から配信するチームが複数あったと、参加記の検索結果の要約にある。個別の URL は出典未確認）。
  - **大きな列は、一覧のクエリと別の表・別の行に分ける**（節 10）。
  - 画像のハッシュなど、毎回計算する値を**アップロード時に計算して列に保存する**（ISUCON13 の p1ass は 12:22 に実施し 19,325 点）。
- **確かめ方**: DB の転送量・WAL の量・ディスク I/O、画像 API の時間。
- **危険**:
  - **アップロード直後の参照・権限・初期化・複数台での保存先**を確かめる。
  - **静的ファイル・初期データの内容は変えない**（ルール）。初期データの画像を別の場所に移す場合の可否はマニュアルで確かめる。
  - ファイルに移したデータは、**再起動後に残ること**（永続化）と、**書く順序（実体が先、可視な参照があと）**を保つ。UNLOGGED 表を唯一の保存先にしない（`05-postgresql.md` の節 10）。
  - 複数台構成では、他の台へ取りに行く経路が増える。台間の通信の規定を確かめる。
- **出典**: https://blog.p1ass.com/posts/isucon13/

---

## 12. FOR UPDATE の範囲とロック

- **効く条件**: ロック待ちが長い（MySQL: `SHOW ENGINE INNODB STATUS` の待ち、`innodb_lock_wait_timeout`。PostgreSQL: `wait_event_type = 'Lock'`）。必要のない行まで `SELECT ... FOR UPDATE` している。
- **やり方**:
  - **ロックを取らずに済む形にする**: 確認と更新を `UPDATE ... WHERE 条件` の 1 文にして更新件数で判定（節 7）。不要な `FOR UPDATE` を外す（デッドロックの文書も「スナップショットで足りるなら `FOR UPDATE`/`FOR SHARE` を外す」）。
  - **範囲を絞る**: MySQL は **インデックスで引ける条件にする**（スキャンした範囲にロックが掛かる）。REPEATABLE READ では範囲検索・非ユニーク検索が next-key（ギャップ）ロックを取り、挿入も止める。ユニークなインデックスの 1 行の検索は 1 行のレコードロックだけ。READ COMMITTED はギャップロックを取らない（`04-mysql.md` の `transaction_isolation`）。
  - **ロックの強さを選ぶ**: PostgreSQL は `FOR UPDATE` > `FOR NO KEY UPDATE` > `FOR SHARE` > `FOR KEY SHARE`（`05-postgresql.md` の節 13）。
  - **`SKIP LOCKED`・`NOWAIT`**: ロックされた行を飛ばす・すぐエラーにする（MySQL の文書で確認。PostgreSQL にもあるが出典未確認）。**キューのような表**向き。通常のトランザクションの用途には、不整合な見え方になるので使わない（MySQL の文書）。
  - ロックを持ったまま、**重い処理・外部 API・画像の保存**をしない。
- **確かめ方**: ロック待ちの時間・件数。同じ負荷で API の p95。**並列の整合性テスト**（`race`・同時に N 本）。
- **危険**:
  - **ロックの範囲を狭めても点が変わらないことがある**（ISUCON13 の `FOR UPDATE` の位置変更）。待ち時間の内訳を測ってから直す。
  - 範囲を狭めると**二重予約・二重購入**が起きる。排他は保つ。
  - 弱いロックから強いロックへの格上げは、デッドロックになる。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html、https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html、https://www.postgresql.org/docs/16/explicit-locking.html

---

## 13. トランザクションを短くする

- **効く条件**: ロック待ち・接続プールの取得待ち（接続をトランザクションが長く握る）。`idle in transaction` が多い。
- **やり方**:
  - **参照だけの API のトランザクションを外す**（ISUCON11 本選の fujiwara 組）。複数 SELECT の一貫したスナップショットが要件に無いか確かめる。
  - **トランザクションの中に、外部 API・重い計算・画像の処理・`sleep` を入れない**（前に出す・後ろに出す）。
  - **排他のための外側のロック（`flock` など）を、DB のトランザクションに置き換える**（ISUCON12 予選の作問者の解説: スコアの入稿で、DELETE と INSERT をトランザクションにして `flock` を外した。確認の `SELECT COUNT` はロックの外に出した）。
  - 開いたままのトランザクション（コミット・ロールバックの漏れ）を作らない。PostgreSQL は `idle_in_transaction_session_timeout` が保険になる（`05-postgresql.md`）。
  - 文の本数を減らすのは、往復が高い構成のとき（節 5）。
- **確かめ方**: トランザクションの長さ（OTel のスパン）、ロック待ち、接続プールの取得待ち。
- **危険**:
  - **トランザクションをそのまま剥がすと整合性が崩れる**。ISUCON13 の amkkr は、多数呼ばれる処理のトランザクションを削除したが、ベンチ開始時のチェックに失敗した。**対象の処理と保護する不変条件を特定してから範囲を縮める**。
  - 外部 API をトランザクションの外に出すと、登録の意味が変わる（外部失敗時の巻き戻し・重複登録・途中状態の公開。ISUCON11 予選の）。
  - 原子性を落とすと、ベンチの整合性チェックが「再入稿中に行が少ない」を見て失敗する（ISUCON12 の解説の注意）。
- **出典**: https://isucon.net/archives/56842718.html

---

## 14. デッドロック

- **効く条件**: ログに `Deadlock found when trying to get lock`（MySQL）・`deadlock detected`（PostgreSQL）。負荷を上げたときだけ 500 が出る。
- **やり方**（詳細は `04-mysql.md` の「デッドロックと再試行」・`05-postgresql.md` の節 13）:
  1. **再試行を実装する**（上限つき・冪等）。
  2. **ロックを取る順序をそろえる**（複数の行は id の昇順など）。
  3. トランザクションを短くする。ロックの範囲を減らす（インデックス・`FOR UPDATE` を外す）。
- **確かめ方**: エラーログの件数。並列の試験。
- **危険**: **再試行なしで 500 を返すと、整合性の失格につながる**（前回の練習: ISUCON9 本戦。初期実装が再試行している箇所は、置き換えでも同じ条件で再試行する。）。再試行は冪等でないと二重に実行される。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html、https://www.postgresql.org/docs/16/explicit-locking.html

---

## 15. ORM の生成する SQL

- **効く条件**: コードにはループがないのに SQL が大量に出る。生成された SQL が `SELECT *` で、関連を 1 件ずつ引いている（遅延ロード）。
- **やり方**:
  - **事前ロード（eager loading）を使う**。GORM の `Preload` は関連ごとに別の SQL を `IN` で発行する。`Joins` は `LEFT JOIN` で 1 つの SQL（1 対 1 の関連のみ）。Rails の `includes` は基本は別の SQL で、関連に条件を付けると `LEFT OUTER JOIN` に切り替わる。`preload` は常に別の SQL、`eager_load` は常に 1 つの `LEFT OUTER JOIN`。
  - ORM を通さず**手書きの SQL にする**のが、JOIN や集計で速いことが多い（ISUCON の初期実装は手書きの SQL が多い。出典未確認。Rust の ORM の挙動も出典未確認）。
  - 生成された SQL を見る（slow log・general log・`pg_stat_statements`・ORM のデバッグログ）。
- **確かめ方**: 1 リクエストあたりの SQL の本数。
- **危険**: ORM の事前ロードで取得する行・列が増える（転送量）。関連の件数が多いと、IN のリストが大きくなる。ORM の更新の動作（全列を更新する、楽観ロックの追加の SELECT）を確認する（出典未確認）。
- **出典**: https://gorm.io/docs/preload.html、https://guides.rubyonrails.org/active_record_querying.html

---

## 16. 文字列検索（LIKE の前方一致・全文検索）

- **効く条件**: `WHERE name LIKE '%語%'`・`WHERE lower(name) = ?`・`WHERE YEAR(d) = 2024` が遅い（`type: ALL`・`Seq Scan`）。
- **やり方**:
  - **列に関数をかけない**: `YEAR(d) = 2024` → `d >= '2024-01-01' AND d < '2025-01-01'`（MySQL の一般論。MySQL の文書は「列を関数で包むとシークできない」を明記していない。出典未確認）。どうしても要るなら式インデックス・生成列（`04-mysql.md` の節 8、`05-postgresql.md` の節 8）。
  - **LIKE の前方一致（`'abc%'`）は B-tree が使える**（範囲に変換される）。**後方一致・部分一致（`'%abc'`・`'%abc%'`）は使えず全件走査**（MySQL の一般論。出典未確認）。
  - **型を揃える**: 数値の列と文字列の比較、文字列の列と日時の比較は、インデックスが使えなくなる。**文字コード・照合順序を揃える**（文書: `utf8mb4` の列と `latin1` の列の比較ではインデックスが使えない）。
  - **部分一致は全文検索へ**: MySQL は ngram パーサー（`WITH PARSER ngram`、`MATCH ... AGAINST`）、PostgreSQL は `pg_trgm` + GIN。**日本語は、ロケールと最小の長さに注意**（`04-mysql.md` の節 8、`05-postgresql.md` の節 8）。
  - **DB からアプリへ移す**（NG ワードの判定をアプリでやる）選択もある。ただし ISUCON13 の p1ass は、NG ワードの判定を LIKE から Go に移したら点が下がった（26,685 → 25,261。N+1 が残っていて、後の N+1 の解消で上がった。の別の例では集合演算で直した）。
  - NG ワードの置換など、大量のキーワードの置換は Trie。
- **確かめ方**: `EXPLAIN`。**検索結果が元の SQL と一致するか**（全文検索は一致の意味が変わる）。
- **出典**: https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html、https://dev.mysql.com/doc/refman/8.4/en/fulltext-search-ngram.html、https://www.postgresql.org/docs/16/pgtrgm.html、https://blog.p1ass.com/posts/isucon13/

---

## 17. ランキング・集計の事前計算

- **効く条件**: 一覧・ランキング・請求・統計のたびに、全履歴を `GROUP BY`・`SUM`・`MAX` で集計している（スロークエリの上位）。**書き込みより読み取りがずっと多い**（ISUCON12 の解説: 入稿は閲覧の 10〜20 分の 1）。
- **やり方**: **書き込み時（または確定時）に集計を保存し、読み取りは保存した値を返す**。
  - ISUCON12 予選の作問者の解説: **大会の終了時に請求レポートの表を作る**（管理 API は保存した値の合計だけ。開発中のベンチで約 5 倍）、**ランキングの表をスコアの入稿時に更新する**（`INSERT ... ON DUPLICATE KEY UPDATE`）。`visit_history` は初回時刻だけを持つ形に縮める（作問者の解説では約 322 万行 → 約 20 万行。ISUCON12 予選の rinsuki も初回だけを一意行で保持した）。
  - ISUCON13 の sorah は、`users` と `livestreams` に `total_tips`・`total_reactions`・`score` を持ち、順位用の複合インデックスを張った。ISUCON14 の Iceman は、`chair_locations` を現在位置と累積距離の UPSERT にした。nomeaning は `ride_statuses` の最新状態を `ride` 側に持った。
  - 履歴から最新だけ必要なら、窓関数（`ROW_NUMBER`）で最新行へ圧縮、または最新状態を別の列に持つ。
  - **現在値・確定値（フリーズ時点）を別の列に持ち、閲覧者に応じて選ぶ**（ISUCON10 本選の感想戦の kazeburo）。
- **確かめ方**: 集計の SQL が slow log から消えるか。**再集計の値と保存した値が一致するか**（ずれの検知）。
- **危険**:
  - **更新漏れ・競合・ロールバック・初期化で保存値が狂う**。初期データの集計値を復元する（`/initialize` で再構築する。時間が増える）。
  - **ISUCON14 の catatsuy は、椅子の現在地を `chair` 表に持たせたが減点が増えて撤回した**（初期データの不備は著者の推測）。履歴を残す要件と現在値を使う要件を分ける。
  - 配点・減点の内訳（レスポンスだけでなく）を比べる。
  - 同点の順位・遅延到着・属性の更新・削除を扱う。
  - 集計表への更新が**書き込みのホットな行**になる（ロック待ち）。同時刻の加算を別の行に追記して、あとで合算する案もある（ISUCON7 本選の najeira）。
- **出典**: https://isucon.net/archives/56842718.html、https://diary.sorah.jp/2023/11/28/isucon13

---

## よくある失敗

| 失敗 | 何が起きるか | 避け方 |
| --- | --- | --- |
| 計測せずに SQL を書き換える | 効かない・点が下がる | 合計時間の上位から。変更ごとに EXPLAIN とベンチの両方を見る |
| インデックスを足したが、使われていない・書き込みが遅くなる | 点が下がる（ISUCON12 の）・初期化がタイムアウト（ISUCON10 の） | `EXPLAIN` で使用を確かめ、`/initialize` の時間と書き込みの時間を測る |
| N+1 の解消で応答が変わる | レスポンスの不一致・不正データで失格（ISUCON14 の ・ISUCON10 の） | 小さなデータで 0 件・1 件・複数・NULL・同順位・境界を旧実装と比べる |
| JOIN の `WHERE` に関連の条件を置く | 関連の無い親の行が消える | 関連の条件は `ON` に置く（LEFT JOIN） |
| ORDER BY の列を替えて同じ順序と思い込む | 順序・ページが変わる・遅くなる（ISUCON3 の） | 同じ順序になる保証と WHERE の条件も見る。一意な列をタイブレークに足す |
| トランザクションをそのまま剥がす | ベンチ開始時のチェックで失敗（ISUCON13 の） | 保護する不変条件を特定してから縮める |
| ロックの範囲を狭めたが排他が崩れる | 二重予約・二重購入 | 並列の整合性テストを書いてから |
| デッドロックを 500 のまま返す | 整合性の失格（ISUCON9 本戦の練習） | 再試行（上限つき・冪等）。ロックの順序をそろえる |
| 往復が安い構成で、文の本数だけを減らす | 効かない | 往復の時間 × 本数で見積もる |
| 一部の N+1 だけ直して終わる | スロークエリの上位に残る（ISUCON13 の統計。65,663 → 143,119 点は残りを直したあと） | 直したあとに再計測する |
| カウンタ・集計表を更新漏れのまま入れる | 値が狂う・失格 | 更新の全経路（登録・削除・初期化）を洗い出し、再集計で検知する |
| 全文検索・LIKE の置換で結果が変わる | 整合性チェックで失格 | 旧実装との突き合わせ（oracle）で全パターンを確かめる |

---

## 出典一覧

公式ドキュメント・解説

- https://dev.mysql.com/doc/refman/8.4/en/explain-output.html
- https://dev.mysql.com/doc/refman/8.4/en/explain.html
- https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html
- https://dev.mysql.com/doc/refman/8.4/en/order-by-optimization.html
- https://dev.mysql.com/doc/refman/8.4/en/limit-optimization.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html
- https://dev.mysql.com/doc/refman/8.4/en/optimizing-innodb-bulk-data-loading.html
- https://dev.mysql.com/doc/refman/8.4/en/fulltext-search-ngram.html
- https://www.postgresql.org/docs/16/using-explain.html
- https://www.postgresql.org/docs/16/indexes-index-only-scans.html
- https://www.postgresql.org/docs/16/explicit-locking.html
- https://www.postgresql.org/docs/16/sql-insert.html
- https://www.postgresql.org/docs/16/populate.html
- https://www.postgresql.org/docs/16/pgtrgm.html
- https://wiki.postgresql.org/wiki/Slow_Counting
- https://use-the-index-luke.com/no-offset
- https://use-the-index-luke.com/sql/where-clause/the-equals-operator/concatenated-keys
- https://jmoiron.github.io/sqlx/
- https://docs.rs/sqlx/0.8.6/sqlx/struct.QueryBuilder.html
- https://gorm.io/docs/preload.html
- https://guides.rubyonrails.org/active_record_querying.html

ISUCON の記録

- https://isucon.net/archives/56842718.html（ISUCON12 予選 作問者の解説）
- https://blog.p1ass.com/posts/isucon13/
- https://diary.sorah.jp/2023/11/28/isucon13
- https://zenn.dev/layerx/articles/3bd55b77e047c4（ISUCON14）

