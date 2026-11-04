# 07 データモデル

当日のマニュアルが優先する。スキーマの変更、初期データの加工、初期化の制限時間、再起動後のデータ保持、画像など静的ファイルの扱いは、回ごとにルールが違う。

## この章で扱う範囲

- 非正規化（JOIN を省く列、検索用の派生列）。
- 集計値・最新状態の保存（カウンタ列、最新の 1 件だけを別テーブル、確定した結果の保存）。
- 事前計算と、履歴の圧縮・追記。
- 画像や大きな本体を DB の外へ出す判断。
- テーブルの分割・パーティション。
- ID の採番（AUTO_INCREMENT、UUID、ULID、アプリでの採番）と時刻の持ち方。
- スキーマ変更と initialize の整合、初期データの変換と読み込みを速くする方法。

扱わないもの: インデックスの設計と EXPLAIN（06 章）、DB の設定（04・05 章）、DB を複数台に分ける構成（11 章）、画像を nginx から返す設定（12 章）、キャッシュ（08 章）。

## 最初に見るもの（当日の 5 分チェックリスト）

| 見るもの | 見方 | 分かること |
| --- | --- | --- |
| 初期データの行数と、行数の多いテーブル | `SELECT table_name, table_rows FROM information_schema.tables WHERE table_schema = DATABASE`（MySQL）。`SELECT relname, n_live_tup FROM pg_stat_user_tables`（PostgreSQL） | 圧縮・分割・移行の対象。2022 の予選は 320 万行と 370 万行のテーブルが主役だった |
| 毎回集計している SQL | スロークエリ・`pg_stat_statements` の上位で `COUNT`・`SUM`・`MAX`・`GROUP BY`・`LAG`・サブクエリが回っているもの | カウンタ列・最新状態テーブル・確定時保存の候補 |
| 読む前に毎回 JOIN している表示用の値 | 上位の SQL の JOIN 先が「名前だけ欲しい」ものか | 非正規化の候補 |
| 取得後にアプリで絞っている処理 | `LIMIT` を付けられず全件取得してフィルターしているループ | 検索用の派生列の候補 |
| DB に入っている大きな列 | `BLOB`・`bytea`・長い `TEXT` の列と、それを `SELECT` している SQL | DB の外に出す候補。`SELECT *` で本体まで読んでいないか |
| initialize の処理内容と所要時間 | マニュアルの制限時間（30 秒・60 秒など）と、ローカルでの実測 | スキーマ変更で initialize が間に合うか |
| 再起動後に残るべきデータ | マニュアルの追試の手順（再起動後に負荷走行中のデータが取れるか） | メモリ・ファイルに逃がしたデータの永続化 |
| 時刻・ID の仕様 | マニュアルの「時刻の精度」「ID の形式」「並びの条件」 | 列の型の変更可否 |

## 非正規化

### JOIN を省くための列を持つ

- **効く条件**: 上位の SQL が、表示したいだけの値（名前・種別・件数）のために毎回 JOIN している。スロークエリの `Rows_examined` が JOIN 先で膨らむ。
- **やり方**: 参照元の行に値を複写する。書き込みのとき（行を作るとき）に 1 回だけ取り込む。
  ```sql
  ALTER TABLE announcements ADD COLUMN course_name VARCHAR(255) NOT NULL DEFAULT '';
  -- 追加時: INSERT のとき、科目テーブルを 1 回引いて course_name に入れる
  ```
  複写元が後から変わるなら、更新の経路すべてで複写先も更新する。変わらないなら（マスターなど）追加時に取り込むだけでよい。
- **確かめ方**: 該当 SQL の `Rows_examined`・実行時間と、`EXPLAIN` の JOIN が消えたか。`pg_stat_statements` の走行ごとの差分で DB 時間が下がったか。初期データの既存行にも値が入っているか（initialize か移行で埋める）。
- **危険**: 複写元の更新に追従しないと表示が古くなり、整合性の検査で落ちる。既存行の埋め忘れ（`DEFAULT ''` のまま）に注意。列を足すだけなら失格の心配は小さいが、API の応答の中身は変えない。
- **出典**: ISUCON11 本選の講評（お知らせに科目名を持たせる案。[講評](https://isucon.net/archives/56163308.html)）。実例: （announcements に course_name、ISUCON11 本選・[acidlemon](https://beatsync.net/main/log20210921/)）、reservations に dep_id・arrival_id（ISUCON9 本選・[rosylilly](https://rosylilly.hatenablog.com/entry/2019/10/07/020627)）。

### 検索用の派生列を書き込み時に計算して持つ

- **効く条件**: 取得した行をアプリで解釈してから絞る処理があり、DB 側で `WHERE`・`LIMIT` を使えない。「条件に合う行を N 件」のために全件を読んでいる。降順と昇順が混在する `ORDER BY` でインデックスが効かない。
- **やり方**: 書き込み時に派生値を計算して列に入れ、そこにインデックスを作る。
  - ISUCON11 予選: `condition_level` を POST のときに計算して保存する。講評は「問題は正規化されていないことではなく `LIMIT` を取れないこと」と述べている。
  - ISUCON10 予選: `popularity` の降順と `id` の昇順の混在は、MySQL 5.7 では単純なインデックスが効かない。講評は `popularity_desc`（`-popularity`）を生成列にしてインデックスを張る案を示している。緯度経度は `POINT` の生成列に空間インデックスを張る案。
  - ISUCON13: アイコンのハッシュを毎回計算せず、列に保存する。講評は INSERT/UPDATE 時にトリガーで計算する例を挙げている。
  生成列かアプリ側の計算かトリガーかは、書き込みの経路の数で決める。経路が 1 つならアプリ、複数ならトリガーか生成列が漏れにくい。
- **確かめ方**: `EXPLAIN` で `WHERE` がインデックスを使い、`LIMIT` が効いているか。取得行数（`Rows_sent` と `Rows_examined`）が減ったか。
- **危険**:
  - 派生列の定義が誤ると、検索結果の整合性が崩れる。ISUCON10 予選の講評は、初期データの派生値と、新規登録・CSV 入稿で作った行の派生値の形式がそろわないと並びがずれる、と注意している。**初期データの既存行にも同じ規則で値を入れる**。
  - MySQL 5.7 では、生成列に `SPATIAL INDEX` を張るには `STORED` が要る（ISUCON10 予選の講評）。
  - 境界条件を読み違えると失敗する。（出入口の最小・最大寸法列）は失敗例。
- **出典**: [ISUCON11 予選講評](https://isucon.net/archives/56044867.html)、[ISUCON10 予選講評](https://isucon.net/archives/55025156.html)、[ISUCON13 講評](https://isucon.net/archives/58001272.html)。実例: condition_level、`LIMIT` を先に付けて食い違った例を含む、アイコンのハッシュ列。

### 非正規化が逆効果になる場合

- **効く条件**: 派生値や複写値が更新のたびにずれやすい。更新の経路が多い。初期データに不備がありうる。
- **やり方**: 入れる前に、更新の経路を grep で全部数える。経路が多いなら、読み取り側のキャッシュ（08 章）か JOIN のままインデックスで解く案と比べる。
- **確かめ方**: 変更後のベンチで、スコアだけでなく減点（整合性違反・タイムアウト）の内訳を比べる。
- **危険**: ISUCON14 で catatsuy は、椅子の現在地を `chair` の列に持たせたが減点が増えて撤回した（初期データの不備は筆者の推測で、原因は確定していない）。ISUCON11 予選の acidlemon は、`trend` の最新状態のキャッシュを直してベンチを通したが点が伸びず、後続の負荷が増えた可能性を考えた。
- **出典**: [acidlemon](https://beatsync.net/main/log20210825/)。

## 集計値・最新状態の保存

### カウンタ列・集計テーブルを書き込み時に更新する

- **効く条件**: 読み取りのたびに `COUNT`・`SUM`・`GROUP BY` で数え直している。履歴の全件を走査している。ランキング・件数・残数が上位の SQL にある。
- **やり方**: 書き込み時に加減算する。専用テーブルか、親の行の列に持つ。
  ```sql
  -- 予約・取消で残数を加減算（専用テーブルの例）
  UPDATE sheet_reserved SET reserved = reserved + 1 WHERE event_id = ? AND rank = ?;
  ```
  - 件数が分かれば、読み取りは `SELECT` 1 本になる。
  - 同じ行の加算が衝突して待つ場合は、加算を別行の INSERT にして後でまとめる方法がある（次の「追記」の項）。
  - 数を分解して持つ手もある。ISUCON11 本選の講評は、GPA を「取得単位数と重み付き得点を別々に集計し、最後に割る」形に分けられると述べている。
- **確かめ方**: 該当 SQL が消えるか軽くなるか。更新側の SQL が増えた分の時間（書き込みの p95）。**更新経路の全部で加減算しているか**を、取り消し・削除・初期化・管理 API まで含めて確かめる。
- **危険**:
  - 加減算の漏れで数が狂う。初期化で戻すのを忘れる。ISUCON8 予選の復習で arosh は、予約追加・取消・初期化・イベント作成の 4 つで更新した。
  - 保存する行を間引くときは、件数を別に数える。ISUCON12 予選で hidekiy は、成績行を最新だけに圧縮した際に行番号を件数に流用して件数チェックに違反した。専用のカウンターで直した。
  - 書き込みが増える分だけ書き込みの競合も増える。行ロックの待ちを `SHOW ENGINE INNODB STATUS`・`pg_locks` で確かめる。
- **出典**: [ISUCON11 本選講評](https://isucon.net/archives/56163308.html)、[ISUCON10 本選講評](https://isucon.net/archives/55140539.html)（`teams.last_answered_clarification_id` やカウンタキャッシュ）。実例: [arosh](https://kujira16.hateblo.jp/entry/2018/10/22/013611)、total_tips・total_reactions・score（ISUCON13・[sorah](https://diary.sorah.jp/2023/11/28/isucon13)）、[hidekiy](https://blog.hidekiy.com/2022/07/isucon12-isucon.html)、[kazeburo](https://kazeburo.hatenablog.com/entry/2020/10/14/111149)、スコアのサマリーテーブル。

### 最新の 1 件を別テーブルに持つ（現在値＋累積）

- **効く条件**: 履歴の全件から「最新の位置」「総移動距離」「最新の状態」を毎回計算している。`LAG`・`SUM`・`ROW_NUMBER`・`MAX(created_at)` のサブクエリが上位にある。ISUCON14 の `GET /api/owner/chairs`（全椅子の総走行距離を計算してからオーナーで絞る）、`GET /api/app/nearby-chairs`（椅子ごとに問い合わせ）が典型。
- **やり方**: 履歴は残したまま、最新の 1 行と累積値を持つ別テーブルを `UPSERT` で更新する。
  ```sql
  -- 椅子の最新位置と累積距離（UPSERT）。累積の計算は「旧位置」を使うので、列の更新順に注意
  INSERT INTO chair_locations (chair_id, latitude, longitude, total_distance, updated_at)
  VALUES (?, ?, ?, 0, NOW(6))
  ON DUPLICATE KEY UPDATE
    total_distance = total_distance + ABS(latitude - VALUES(latitude)) + ABS(longitude - VALUES(longitude))
    latitude = VALUES(latitude)
    longitude = VALUES(longitude)
    updated_at = VALUES(updated_at);
  ```
  - ISUCON14 の講評は、最新位置と総走行距離を持つテーブルを `UPSERT` で更新すれば、古い位置情報は総距離の計算以外に使われないので多くのクエリが簡素になる、と述べている。
  - ライドの完了を同じトランザクションで記録しておけば、ステータステーブルを結合せずに完了の判定ができる（講評）。
  - 初期データの履歴から、最新テーブルの初期状態を作る（ISUCON14 の krtx は、初期データの約 2 万件の移動履歴を考慮して累計距離を持たせた）。
- **確かめ方**: 該当 SQL の実行時間。走行後に「履歴から計算した総距離」と「保存した総距離」が一致するか（テストで 1 回突き合わせる）。
- **危険**:
  - **更新順の誤り**。Iceman は `UPSERT` で先に緯度経度を更新したため、後続の距離の計算が常に 0 になり、`total` の更新を先頭に移して直した。累積の列は、旧い位置を使う側を先に書く。走行後に履歴から計算した総距離と突き合わせて確かめる。
  - **反映の猶予を確かめる**。ISUCON14 は位置の反映に 3 秒の猶予があり、位置の書き込みを非同期・バルクにできる。一方 `nearby-chairs` の割り当て可否は即時反映が要り、応答全体を 3 秒キャッシュすることはできない（講評）。
  - 初期データの不備や更新順で減点が増えて撤回した例もある。
  - 初期化で最新テーブルも作り直す。
- **出典**: [ISUCON14 講評](https://isucon.net/archives/58869617.html)、[ISUCON14 マニュアル（猶予時間）](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、Iceman（[ISUCON14 参加記](https://zenn.dev/iceman/articles/1780deef84eda8)）、krtx（[ISUCON14 参加記](https://krtx.hatenablog.com/entry/2024/12/09/131052)）。[nomeaning](https://blog.hoge.cc/post/isucon-14)。

### 確定した結果を、確定の時点で保存する

- **効く条件**: 後から変わらない結果（終了した大会の課金、終了後のランキング、運賃）を、読み取りのたびに計算している。
- **やり方**: 確定する操作（終了 API、入稿）の中で計算して保存する。
  - ISUCON12 予選の講評は、課金情報を大会の終了時に計算して保存し、読み取りのたびに計算しないことを勧めている。
  - 作問者の事前解答では、入稿時にランキング表を一括 `UPSERT` し、初期化用の順位表も作った。
  - ISUCON10 本選の解答例は、負荷走行が成功した時点でリーダーボードを作って Redis に保存した。
- **確かめ方**: 読み取りの SQL が `SELECT` 1 本になったか。確定の API の所要が許容内か（確定の処理に計算が移るため）。
- **危険**: 「確定後は変わらない」と思い込んで失敗した例が複数ある。終了済み配信の集計、未来の状態の計算結果、課金のキャッシュ範囲を広げて不整合。**確定の条件**（本当に変更・削除・やり直しが無いか）をマニュアルとコードで確かめる。ISUCON12 予選の講評は、終了していない大会はキャッシュ・事前保存できないと注意している。
- **出典**: [ISUCON12 予選講評](https://isucon.net/archives/56850281.html)、[ISUCON10 本選講評](https://isucon.net/archives/55140539.html)、[ISUCON12 予選の作問者の解答](https://isucon.net/archives/56842718.html)。

## 事前計算・履歴の圧縮・追記

### 初期データを事前に計算・圧縮しておく

- **効く条件**: 初期データの行数が多く、その大半が使われない（履歴の過去行、重複した閲覧記録）。走査・インデックス・移行・初期化のすべてが重い。
- **やり方**: 必要な行だけに圧縮する。
  - ISUCON12 予選の講評: `visit_history`（初期 320 万行超）は、テナント・大会・参加者ごとに最初の 1 行しか使われない。`MIN(created_at)` で圧縮して `UNIQUE INDEX` を張ると約 20 万行になる。`player_score`（初期約 370 万行）は、大会・参加者ごとに最後に出現する 1 行だけが有効で、`MAX(row_num)` で抽出して作り直すと約 18 万行になる。講評は「この削減に気づいたチームは MySQL への移行に成功しやすかった」と述べている。
  - 圧縮した結果を初期データ（ダンプ）として配り、initialize で復元する（次の「initialize」の節）。
- **確かめ方**: 圧縮前後の行数、同じ SQL の `Rows_examined`、initialize の所要。圧縮した後の応答が元と一致するか（互換テスト）。
- **危険**: 有効な行の判定を誤ると応答が変わる。書き込みが入った後も同じ規則で圧縮された形を保つ（INSERT のたびに「初回だけ」「最新だけ」を保つ。`INSERT IGNORE` と `UNIQUE`、`UPSERT`）。 は、閲覧履歴の重複抑制で初回時刻の仕様を読み違えて戻した例。
- **出典**: [ISUCON12 予選講評](https://isucon.net/archives/56850281.html)。実例: [rinsuki](https://rinsuki.hatenablog.jp/entry/2022/07/25/215902)、[karupanerura](https://techblog.karupas.org/entry/2022/07/30/210337)。

### UPDATE を追記（INSERT）に変えて行ロックの競合を避ける

- **効く条件**: 同じ行への加算が集中し、`UPDATE` の行ロックの待ちがボトルネックになっている。
- **やり方**: 加算を別行の `INSERT` にして、読むときに合算する。溜まったら閾値でまとめ直す。
  - ISUCON7 本選: 同時刻の加算を別レコードの INSERT にした。確定済みの加算イベントを一定件数ごとに `DELETE` して総和の 1 行を `INSERT` した例もある。
- **確かめ方**: ロック待ち（`innodb_row_lock_waits`・`pg_locks`）が減ったか。合算の SQL が重くなっていないか。
- **危険**: 合算の読み取りが重くなる。まとめ直しの途中で読まれると二重に数える・数えそびれる。トランザクションの境界を決める。
- **出典**: 実例: [najeira](https://zenn.dev/najeira/articles/2017-12-04-qiita-8ac2c4ac2c2e4e4e170b)、[ymyzk](https://blog.ymyzk.com/2017/11/isucon7-final/)。

### 固定の値をコード・計算に置き換える

- **効く条件**: 変わらないマスター（カテゴリ、座席、既定の画像）を毎回 JOIN・SELECT している。
- **やり方**: マスターをコードに埋め込む、計算で復元する。
  - ISUCON9 予選: カテゴリは更新がないのでソースコードに直接書いたチームが多かった（講評）。
  - ISUCON8 予選の復習: 座席 ID から座席のランクと番号を計算で復元し、`LEFT JOIN` をやめた。
  - ISUCON13: 既定アイコンのハッシュを事前に計算してコードに埋め込んだ（Iceman）。
- **確かめ方**: 該当 JOIN が消えたか。コードの値と DB の値が一致するか（テストで 1 回突き合わせる）。
- **危険**: マスターが更新される問題では使えない。ISUCON12 本選のマスターは更新があった（08 章のキャッシュの無効化へ）。再起動後・initialize 後も同じ値になること。
- **出典**: [ISUCON9 予選講評](https://isucon.net/archives/53789931.html)、[ISUCON12 本選講評](https://isucon.net/archives/56959385.html)、Iceman（[ISUCON13 参加記](https://zenn.dev/iceman/articles/5aad04f18b128f)）。

## 画像や大きな本体を DB の外へ

### 画像・大きなバイナリをファイルに出す

- **効く条件**: `BLOB`・`bytea` の列があり、それを `SELECT` する SQL が上位にある。DB の転送量・バッファプールが画像で埋まる。一覧の取得で画像の本体まで読んでいる。
- **やり方**:
  1. 画像の本体をファイルに保存し、DB にはメタデータ（ID、ハッシュ、パス）だけを残す。
  2. 配信は nginx から返す（`X-Accel-Redirect`。12 章）。アプリは権限の確認だけして、本体はアプリを通さない。
  3. 一覧の `SELECT` で画像の列を読まない（`SELECT *` をやめる。ISUCON11 予選の講評は MariaDB の `INVISIBLE COLUMNS` なら 1 行で済むと述べている）。
  4. ハッシュ（`ETag`）が要る場合は、書き込み時に計算して列に保存する（ISUCON13。上の「派生列」）。
- **確かめ方**: 画像を読む SQL が消えたか。DB のバッファプールのヒット率・ディスクの読み取り量。ファイル配信の帯域（12 章）。
- **危険**:
  - **アップロード直後の読み取り・権限・初期化・複数台での保存先**を確かめる。他の台から取れない場所に置くと、台をまたぐ読みで 404 になる。
  - **削除・無効化**。ファイルも消す。
  - **initialize で、ファイルも初期状態に戻す**。初期データ由来のファイルを消してしまうと戻せない。
  - **再起動後もファイルが残る**場所に置く（`/tmp` と tmpfs は不可）。
  - 静的ファイル・フロントエンドの成果物は変えない（マニュアルが禁じる）。ここで動かすのはユーザーが投稿したデータで、静的ファイルではない。区別をマニュアルで確かめる。
- **出典**: [ISUCON11 予選講評](https://isucon.net/archives/56044867.html)。実例: ISUCON11 予選・[makino](https://tech.mirrativ.stream/entry/2021/08/25/120426)。

## テーブルの分割・パーティション

### 履歴の分離（現役の行と終わった行を別テーブルにする）

- **効く条件**: 行数が多く、大半が「終わった行」（受け取り済み、完了、期限切れ）で、現役の行だけを引くのに全体を走査している。
- **やり方**: 現役と履歴を別のテーブルにして、現役のテーブルを小さく保つ。ISUCON12 本選の講評は、受け取り済みのプレゼント（`deleted_at` が入り以後変更されない）を別の履歴テーブルに分け、そのうえで水平分割する案を示している。
- **確かめ方**: 現役のテーブルの行数、該当 SQL の `Rows_examined`。移動の処理（現役→履歴）の所要。
- **危険**: 現役と履歴を横断する検索（履歴の一覧）が UNION になる。移動の途中で二重に見える・消える（同じトランザクションで）。初期データの分け方を initialize に入れる。
- **出典**: [ISUCON12 本選講評](https://isucon.net/archives/56959385.html)。有効な予約だけの専用表。

### 宣言的パーティション（PostgreSQL・MySQL）

- **効く条件**: 時系列の巨大な表で、「最近の範囲だけ」を読む問い合わせが多い。古いデータを一括で消す（`DROP`・`DETACH`）要件がある。競技の 1 日（数時間）で、既存の行を再配置する時間があるとは限らない。
- **やり方**:
  - PostgreSQL: `PARTITION BY RANGE (created_at)` などで宣言的に分ける。`WHERE` と矛盾するパーティションは実行計画から外れる（パーティションプルーニング）。`EXPLAIN` で確かめる。
  - MySQL（InnoDB）: `PARTITION BY RANGE`・`HASH` など。プルーニングは同様。
- **確かめ方**: `EXPLAIN` でスキャンされるパーティションが絞られるか。問い合わせの実行時間。
- **危険**:
  - **主キー・UNIQUE 制約にパーティションキーの全列を含める**必要がある（PostgreSQL・MySQL とも）。既存の主キーを変えるので、`ON DUPLICATE KEY`・`UPSERT`・外部参照・API の ID の一意性に影響する。
  - PostgreSQL では、通常のテーブルを直接パーティションテーブルにはできない。再作成とデータ移行が要り、initialize の時間に効く。パーティションが多すぎると計画の時間とメモリが増える。
  - MySQL のパーティションは、外部キーを持てない（出典のページには記載がなく、公式の制限の章で確認する。出典未確認）。
  - 競技のデータ量（数百万行）では、適切なインデックスのほうが効くことが多い（出典未確認。06 章を先に）。
- **出典**: [PostgreSQL: Table Partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html)、[MySQL: Partitioning Overview](https://dev.mysql.com/doc/refman/8.0/en/partitioning-overview.html)。

### テナント単位・キー単位の水平分割（DB を複数に分ける）

- **効く条件**: 行が「テナント」「ユーザー」「ルーム」などで閉じていて、横断する問い合わせが少ない。1 つの DB の CPU・ロックがボトルネック。
- **やり方**: 分割の単位ごとに DB（SQLite ファイル・別 DB・別台）を割り当てる。分割の単位から保存先を引く仕組み（ハッシュ・対応表）をアプリに持つ。台をまたぐ構成は 11 章。
  - ISUCON12 予選: テナントごとに SQLite のファイルがある。作問者の事前解答は、テナントの DB を最初のテナント要求で作る形にして、3 台への分散を可能にした。講評は、SQLite のまま複数台で使うにはテナントとホストの対応をルーティングする必要があり、組まずに複数台化するとデータの所在が合わなくなる、と述べている。
  - ISUCON12 本選: NaruseJun は DB を分割し、ユーザー ID でアプリ側のシャーディングをした。講評は、IDの下 1 桁などを使う分割は、除算の余りで振り分け先を決める場合に ID の採番と相性が悪いので、振り分け用の桁を乱数などで工夫する必要がある、と述べている。
  - ISUCON10 予選: `chair` と `estate` の間に JOIN が無いので、DB を 2 つに分ける構成が有効で、突破チームはこの構成だった（講評）。
- **確かめ方**: 各 DB の CPU・接続数が均等か。横断する問い合わせが残っていないか。
- **危険**: 横断クエリ・横断トランザクションが壊れる。initialize で全ての DB を戻す（Redis とテナント DB の削除漏れ）。分割のキーが偏ると効果が出ない。
- **出典**: [ISUCON12 予選講評](https://isucon.net/archives/56850281.html)、[ISUCON12 本選講評](https://isucon.net/archives/56959385.html)、[ISUCON10 予選講評](https://isucon.net/archives/55025156.html)、[NaruseJun 優勝記](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)。

## ID の採番

### AUTO_INCREMENT の挙動を知る

- **効く条件**: 同時 INSERT が多く、ロック待ちに `AUTO-INC` が出る。大量挿入（`INSERT ... SELECT`・`LOAD DATA`）が遅い。ISUCON12 予選の講評は、`auto_increment` を持つ表を更新して ID を採番する処理が重い、と述べている。
- **やり方（MySQL）**: `innodb_autoinc_lock_mode`（MySQL 8.0 の既定は 2、5.7 以前は 1）を知る。0 は文ごとの表ロック、1 は大量挿入だけ表ロック、2 は表ロックを使わず最も並行性が高い。大量挿入の初期ロードでは 2 を勧めている（出典のドキュメント）。
- **確かめ方**: `SHOW ENGINE INNODB STATUS` のロック待ち、`SHOW VARIABLES LIKE 'innodb_autoinc_lock_mode'`。
- **危険**:
  - ステートメントベースのレプリケーションでは、モード 2 はソースとレプリカで採番がずれうる（行ベース・ミックスは安全）。
  - 欠番が出る。ロールバックした値は戻らない。モード 2 では並行の文の間でも飛ぶ。**連番の連続性を API が要求していないか**を確かめる。
  - 8.0 では最大値がクラッシュ後も保たれるが、異常終了では再利用を完全には防げない。
  - mixed-mode の INSERT（一部の行だけ ID を明示する）は、重複キーで失敗しうる。
- **出典**: [MySQL: AUTO_INCREMENT Handling in InnoDB](https://dev.mysql.com/doc/refman/8.0/en/innodb-auto-increment-handling.html)、[ISUCON12 予選講評](https://isucon.net/archives/56850281.html)。

### 不要な採番をやめる・主キーを自然キーにする

- **効く条件**: `id` を使っていない、参照していない表。行が `(機器, 時刻)` などで一意に決まる。二次インデックスの更新が INSERT の負荷になっている。
- **やり方**: 自動採番をやめ、自然キーを（複合）主キーにする。InnoDB のクラスタインデックスは主キーの順に行を持ち、二次インデックスは主キーの値を持つので、主キーは短く、挿入順に並ぶ値が有利。
  - ISUCON11 予選: 同一 ISU で `timestamp` が重複しないので、`(jia_isu_uuid, timestamp)` を主キーにすると負荷が下がる（講評）。acidlemon は不要になった `id` の採番を外して、終盤に点数が約 60〜80% 伸びたと報告した。
  - ISUCON10 予選: 価格を先頭にした複合主キーにして、`id` は一意の補助インデックスにした。
- **確かめ方**: `EXPLAIN` で `PRIMARY` が使われるか。表・インデックスのサイズ（`information_schema.tables`）。INSERT の時間。
- **危険**: 「重複しない」仕様を読み違えると主キーの重複で失敗する（マニュアルで確かめる）。`id` を外部参照・API の応答で使っていないか。主キーが長くなると二次インデックスが全て膨らむ。**移行・再作成で主キーを落とさない**（ISUCON12 予選で、SQLite からの移行時の PK 漏れが 800 点を 3,657 点に戻した修正がある）。
- **出典**: [ISUCON11 予選講評](https://isucon.net/archives/56044867.html)、[MySQL: Clustered and Secondary Indexes](https://dev.mysql.com/doc/refman/8.0/en/innodb-index-types.html)。実例: [acidlemon](https://beatsync.net/main/log20210825/)。

### UUID・ULID・アプリ採番と挿入性能

- **効く条件**: ID が文字列（UUID・ULID）で、DB の採番テーブルを更新して ID を作っている。INSERT が多く、主キーの二次インデックス・クラスタインデックスの更新が重い。
- **やり方**: 選択肢の特徴を知って選ぶ。
  | 方式 | 特徴 | 注意 |
  | --- | --- | --- |
  | AUTO_INCREMENT | 挿入順に増える。クラスタインデックスの末尾に追記される | 欠番。複数台で衝突。ID の連番が外から見える |
  | ランダムな UUID（v4） | どこでも作れる。順序なし | 挿入がインデックス全体に分散してページ分割と I/O が増える。Percona の記事は、大きな表では挿入のたびに読み 1・書き 1 の I/O が起きうると説明している。36 文字の文字列は大きく、比較も遅い |
  | 時刻順の UUID（v1 を並べ替え、v7） | 挿入順にほぼ並ぶ | MySQL の `UUID_TO_BIN(uuid, 1)` は v1 の時刻部分を先頭へ並べ替える（v1 にだけ有効）。PostgreSQL 18 から `uuidv7`（17 以前は `gen_random_uuid`＝v4）。`BINARY(16)` / `uuid` 型で持つとサイズが減る |
  | ULID | 先頭 48bit が時刻のミリ秒、残り 80bit が乱数。26 文字の Base32。文字列順に時系列で並ぶ | 同一ミリ秒内の順序は仕様上保証されない。文字列で持つと 26 文字。`CHAR(26)` は ASCII だけなので `latin1` にするとインデックスが縮む |
  | アプリでの採番（Snowflake 風、メモリのカウンタ） | DB に聞かずに作れる。1 台なら衝突しないことを確かめるだけでよい | Mutex で直列化すると UUID・ULID より遅いこともある。再起動後に最大値から再開する。複数台・複数プロセスでは台・プロセスの識別子を入れる |
  - ISUCON12 予選の講評は、ID が文字列なので UUID・ULID などに置き換えられると述べ、参考チューニングは UUID にした。ISUCON12 本選の講評は、アプリ側の採番で DB のロックが解消されたと述べ、Snowflake に似た方式を想定している。
  - ID の形式が仕様で決まっているなら変えない（は 16 進の文字列 ID を `BIGINT` に変換したが、URL との相互変換に漏れが出た）。
- **確かめ方**: INSERT の所要・ロック待ち。インデックスサイズ。ID の形式がマニュアルの仕様に合うか。
- **危険**:
  - メモリのカウンタで採番するなら、**再起動後と initialize 後に DB の最大値から再開する**。そうしないと重複キーで失敗する。
  - 複数プロセス・複数台で同じ ID を作らない。
  - ID の並びで ORDER BY している API（作成順の一覧）は、ランダムな ID にすると並びが変わる。作成時刻の列を別に持つ。
  - MySQL の `UUID` は v1 で、ステートメントベースのレプリケーションでは安全でない（警告が出る）。
- **出典**: [MySQL: UUID_TO_BIN](https://dev.mysql.com/doc/refman/8.0/en/miscellaneous-functions.html)、[PostgreSQL: UUID Functions](https://www.postgresql.org/docs/current/functions-uuid.html)、[ULID spec](https://github.com/ulid/spec)、[Percona: UUIDs are popular, but bad for performance](https://www.percona.com/blog/uuids-are-popular-but-bad-for-performance-lets-discuss/)、[ISUCON12 予選講評](https://isucon.net/archives/56850281.html)、[ISUCON12 本選講評](https://isucon.net/archives/56959385.html)。実例: [logica0419](https://zenn.dev/logica0419/articles/isucon-12-qualifier)、[acidlemon](https://beatsync.net/main/log20210921/)、[karupanerura](https://techblog.karupas.org/entry/2022/07/30/210337)。

## 時刻の持ち方

### 型・精度・タイムゾーンを決める

- **効く条件**: 時刻の比較（反映の猶予、期限、並び）が仕様にある。DB とアプリで時刻を作っている。複数の台・プロセスで時刻を作っている。
- **やり方**:
  - MySQL: `DATETIME` はタイムゾーン変換をしない。`TIMESTAMP` は接続の `time_zone` から UTC に変換して保存し、範囲が `2038-01-19` まで。小数秒は `DATETIME(6)` のように桁を指定する（0〜6）。接続の `time_zone` を明示する。
  - PostgreSQL: `timestamptz` は UTC の瞬間として保持し、表示時に `TimeZone` 設定で変換する。`timestamp`（タイムゾーンなし）は入力のタイムゾーンを無視する。精度はマイクロ秒。
  - 仕様がミリ秒の整数（epoch）なら、列を `BIGINT` にして変換を省く手もある（出典未確認。仕様と一致するかを先に確かめる）。
  - 「いつ作られたか」の時刻は、DB 側で 1 つの時計から取る（`NOW(6)`、`now`）か、アプリ側の 1 つの時計から取るかに統一する。
- **確かめ方**: 同じ行を別の台・別のタイムゾーン設定で読み、値が変わらないか。並びを時刻で決めている API の同点の扱い（`ORDER BY created_at DESC, id DESC` のように決定的にする）。
- **危険**:
  - 複数の台のアプリで時刻を作ると、台の時計のずれがそのまま順序になる。
  - ダンプ・移行で日時の型を保つ。ISUCON9 本選で rosylilly は、マスターの pretty print のダンプで壊れる Date を、型を保持する形式に変えて直した。信頼できないシリアライズデータは読まない。
  - 並びに同点が出る場合、元の `ORDER BY` を維持する（`popularity DESC, id ASC` の `id` を落として不一致になった）。
- **出典**: [MySQL: The DATE, DATETIME, and TIMESTAMP Types](https://dev.mysql.com/doc/refman/8.0/en/datetime.html)、[PostgreSQL: Date/Time Types](https://www.postgresql.org/docs/current/datatype-datetime.html)。

## スキーマ変更と initialize の整合

### initialize の制限時間と、スキーマ変更の再現

- **効く条件**: スキーマ（列・インデックス・テーブル）を変える、初期データを加工する、移行する。
- **やり方**: 変更を initialize に組み込み、**毎回同じ結果になる**ようにする。
  - 初期化が失敗すると負荷走行は即失敗する（ISUCON14 マニュアル）。
  - 方法:
    - スキーマの SQL を毎回流す。Iceman（ISUCON13）は `DROP TABLE` と `10_schema.sql` の実行を initialize に組み込み、インデックスを貼りやすくした（初期データが軽かったので毎回作り直せた）。
    - 初期データ用の別テーブルから実行用のテーブルを作る（`init_` 表から `CREATE ... SELECT`）。インデックス・制約が落ちないか DDL と一緒に確かめる。
    - 事前に変換・圧縮した結果をダンプにして配り、initialize で復元する（初期閲覧データだけのダンプで初期化を速くした）。
    - MySQL の `/var/lib/mysql` を丸ごとコピーして戻す・Percona XtraBackup などの物理バックアップを使う（ISUCON12 予選の講評）。`mysqldump` の論理バックアップの復元はデータが多いと 30 秒に収まらない。
    - SQLite は初期ファイルのコピーで戻せる（WAL ファイルの残りも消す）。
- **確かめ方**: **ローカルで initialize を 2 回以上続けて叩き、所要時間を測り、2 回目以降もベンチが通るか確かめる**。上限ぎりぎりの単発の測定を採用条件にしない。
- **危険**:
  - **時間超過は失格・FAIL**。ISUCON12 予選で、全 SQLite を読み直す移行が約 33 秒でタイムアウトして戻した例がある。データを圧縮せずに全件移行しようとして時間切れになる失敗が、講評で繰り返し挙げられている。
  - **initialize の整合**。マニュアル（ISUCON14）は「初期化処理が行っている内容を漏れなく提供してください」と述べている。**メモリ・Redis・ファイル・別 DB・キャッシュの全てを戻す**（Redis とテナント DB の削除漏れ。2 回目のベンチで落ちた）。
  - 2 回目以降の initialize で落ちる（1 回目しか試していない）。
  - initialize を DB を持つ台に固定する。複数台なら全台のメモリを連動して初期化する。
  - 初期化が長くても、競技のルール上、負荷テストの時間は削られない回もある。縮める価値は、制限の余裕と失敗しないこと（`AGENTS.md`）。
- **出典**: [ISUCON12 予選講評](https://isucon.net/archives/56850281.html)、[ISUCON14 マニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、Iceman（[ISUCON13 参加記](https://zenn.dev/iceman/articles/5aad04f18b128f)）。

### 初期データの読み込みを速くする（MySQL）

- **効く条件**: initialize か、変換後のデータを入れる処理が遅い。行数が数十万以上。
- **やり方**: MySQL 8.0 のマニュアルの方法。
  - 行ごとの `INSERT` をやめて、複数行の `INSERT ... VALUES (...), (...)`、または `LOAD DATA`。ISUCON12 予選の講評は、`sqlite3-to-sql` が 1 行ずつ INSERT を出力すると 370 万行の移行に時間がかかりすぎる、と述べ、CSV に出力して `LOAD DATA` で読み込む案を示している。
  - `autocommit=0` にして、数千〜数万行ごとに `COMMIT`（トランザクションが大きすぎると undo とメモリが増える）。
  - **主キーの順に**挿入する（クラスタインデックス）。
  - `unique_checks=0`、`foreign_key_checks=0`（セッションで一時的に）。ロードの後に元へ戻し、整合性を確かめる。
  - FULLTEXT インデックスは、ロードの後に作る。
  - 大量挿入の間は `innodb_autoinc_lock_mode=2`。
  ```sql
  SET autocommit=0; SET unique_checks=0; SET foreign_key_checks=0;
  -- 主キー順の複数行 INSERT を数万行ごとに COMMIT
  COMMIT; SET unique_checks=1; SET foreign_key_checks=1;
  ```
- **確かめ方**: ロードの所要時間（ローカルの本番と同じ件数で）。ロード後の `COUNT(*)` と重複キー・外部キーの突き合わせ。
- **危険**:
  - `unique_checks=0` で重複キーがあるとエラーなく入る。事前にデータに重複が無いことを確かめる。`foreign_key_checks=0` は参照整合性を保証しない。
  - `ALTER INSTANCE DISABLE INNODB REDO_LOG` は**新しいインスタンスへのロード専用**。稼働中の DB で切ると、停止でデータ損失・破損になりうる。使わない。
  - `max_allowed_packet` を超える巨大な 1 文にしない。
- **出典**: [MySQL: Bulk Data Loading for InnoDB Tables](https://dev.mysql.com/doc/refman/8.0/en/optimizing-innodb-bulk-data-loading.html)、[ISUCON12 予選講評](https://isucon.net/archives/56850281.html)。

### 初期データの読み込みを速くする（PostgreSQL）

- **効く条件**: 同上。
- **やり方**: PostgreSQL のマニュアルの方法。
  - `COPY` を使う（`INSERT` より大幅に速い）。`COPY` を使えないときは `PREPARE` した `INSERT` を繰り返す。
  - 複数の `INSERT` は `BEGIN ... COMMIT` で囲む。
  - **インデックスは、データを入れた後に作る**。外部キーも、入れた後に付ける（行ごとのトリガーのキューが数百万行ではメモリを圧迫する）。
  - `maintenance_work_mem` を一時的に増やす（インデックス・外部キーの作成に効く。`COPY` にはほぼ効かない）。`max_wal_size` を一時的に増やす（チェックポイントの回数が減る）。
  - ロードの後に `ANALYZE`。
  - `pg_restore` は `-j`（並列）と `--single-transaction`。
  ```sql
  BEGIN;
  TRUNCATE mytable;  -- 同じトランザクションの TRUNCATE + COPY は、wal_level = minimal のとき WAL を省略できる
  COPY mytable FROM '/path/data.csv' WITH (FORMAT csv);
  COMMIT;
  ANALYZE mytable;
  ```
- **確かめ方**: ロードの所要時間。ロード後に `ANALYZE` して `EXPLAIN` の行数の見積もりが合うか。
- **危険**:
  - `wal_level = minimal`・`archive_mode = off`・`max_wal_senders = 0` は再起動が要り、**複製・スタンバイが使えなくなる**。 のようにスタンバイを使う構成では入れない。設定を戻すのを忘れない。
  - ユニークインデックス・外部キーを外している間は、重複・不正データが入りうる。
  - 一時的に増やした設定は作業後に戻す。
  - `fsync = off` は破損の恐れがあり、競技でも使わない（05 章）。
- **出典**: [PostgreSQL: Populating a Database](https://www.postgresql.org/docs/current/populate.html)、[PostgreSQL: Asynchronous Commit（fsync=off との違い）](https://www.postgresql.org/docs/current/wal-async-commit.html)。

### 外部キー・不要なインデックスを見直す

- **効く条件**: INSERT・UPDATE・DELETE が上位にあり、その表に外部キーや使われていないインデックスがある。
- **やり方**: 外部キーは、更新のたびに追加の確認を要する（ISUCON11 本選の講評は「外部制約キーは INSERT・UPDATE・DELETE 時に追加の CPU オーバーヘッドを生む」と述べている）。使われていないインデックスは INSERT を遅くする（ISUCON11 予選の講評）。使われない降順インデックスを外した例もある。**仕様が参照整合性を求めていないか確かめてから**外す。
- **確かめ方**: 書き込みの SQL の時間、インデックスの使用状況（MySQL: `sys.schema_unused_indexes`、PostgreSQL: `pg_stat_user_indexes.idx_scan`。出典未確認。使い方は 06 章）。
- **危険**: 外部キーを外すと、アプリのバグで孤児行ができても検出できない。整合性の検査（ベンチの終了時チェック）で落ちる場合がある。インデックスを外した後に別の SQL が遅くなる。
- **出典**: [ISUCON11 本選講評](https://isucon.net/archives/56163308.html)、[ISUCON11 予選講評](https://isucon.net/archives/56044867.html)。

## よくある失敗

| 失敗 | 防ぎ方 |
| --- | --- |
| 非正規化・集計列の更新経路を数えそびれ、取り消し・削除・管理 API・初期化で値が狂う | 更新の経路を grep で全部数える。更新→直後の読み取りのテストと、走行後に「集計値と履歴から計算した値」を突き合わせるテスト |
| 初期データの既存行に派生列・集計列が入っていない | initialize か移行で既存行を埋める。件数と値を突き合わせる |
| 「確定後は変わらない」と思い込んでキャッシュ・事前保存し、整合性の検査で落ちる | 確定の条件をマニュアルとコードで確かめる |
| 履歴を圧縮するとき、有効な行の判定を誤る・件数を別に数えない | 圧縮の規則を書き込み後も保つ。件数のカウンターを別に持つ |
| initialize が制限時間に収まらない・2 回目以降に落ちる | 事前に変換・圧縮したダンプを復元する。ローカルで 2 回以上続けて測る。メモリ・Redis・ファイル・別 DB も戻す |
| 移行で主キー・インデックス・制約を落とす | 件数だけでなくインデックス・一意制約・列の型を比べる |
| 画像をファイルに出したが、初期データ由来のファイルを消す・再起動後に消える・削除を反映しない | 保存先は再起動後も残る場所。initialize で戻せるもの（初期データ）と戻さないもの（新規）を分ける |
| メモリで採番して再起動・initialize の後に重複する | 起動時・initialize 後に DB の最大値から再開する |
| 時計の混在（DB の時刻とアプリの時刻、台ごとの時計） | 時刻を作る場所を 1 つにする。並びは `id` などで同点を決定的にする |
| パーティション・水平分割で主キー・UNIQUE・横断クエリが壊れる | 先にインデックスと SQL の見直し（06 章）。分割は主キーにパーティションキーを含める制約を確かめてから |
| `unique_checks=0`・`foreign_key_checks=0`・`wal_level = minimal` を戻し忘れる | ロードの手順に「戻す」と「整合性の確認」を入れる |

## 出典一覧

### ISUCON 公式の講評・マニュアル

- [ISUCON14 問題の解説と講評](https://isucon.net/archives/58869617.html)
- [ISUCON14 マニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、[アプリケーションマニュアル](https://github.com/isucon/isucon14/blob/main/docs/ISURIDE.md)
- [ISUCON13 問題の解説と講評](https://isucon.net/archives/58001272.html)
- [ISUCON12 予選問題の解説と講評](https://isucon.net/archives/56850281.html)
- [ISUCON12 本選問題の解説と講評](https://isucon.net/archives/56959385.html)
- [ISUCON12 予選の作問者の事前解答（acidlemon）](https://isucon.net/archives/56842718.html)
- [ISUCON11 予選問題の解説と講評](https://isucon.net/archives/56044867.html)
- [ISUCON11 本選問題の解説と講評](https://isucon.net/archives/56163308.html)
- [ISUCON10 予選問題の解説と講評](https://isucon.net/archives/55025156.html)
- [ISUCON10 本選問題の解説と講評](https://isucon.net/archives/55140539.html)
- [ISUCON9 予選問題の解説と講評](https://isucon.net/archives/53789931.html)

### 参加記・出題者・常連の記事

- Iceman: [ISUCON13](https://zenn.dev/iceman/articles/5aad04f18b128f)、[ISUCON14](https://zenn.dev/iceman/articles/1780deef84eda8)
- krtx: [ISUCON14（9 位）](https://krtx.hatenablog.com/entry/2024/12/09/131052)
- [NaruseJun（ISUCON12 本選優勝）](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)
- 手元の振り返り:

### 公式ドキュメント

- MySQL: [AUTO_INCREMENT Handling in InnoDB](https://dev.mysql.com/doc/refman/8.0/en/innodb-auto-increment-handling.html)、[Bulk Data Loading for InnoDB Tables](https://dev.mysql.com/doc/refman/8.0/en/optimizing-innodb-bulk-data-loading.html)、[Clustered and Secondary Indexes](https://dev.mysql.com/doc/refman/8.0/en/innodb-index-types.html)、[UUID_TO_BIN など](https://dev.mysql.com/doc/refman/8.0/en/miscellaneous-functions.html)、[DATE・DATETIME・TIMESTAMP](https://dev.mysql.com/doc/refman/8.0/en/datetime.html)、[Partitioning Overview](https://dev.mysql.com/doc/refman/8.0/en/partitioning-overview.html)
- PostgreSQL: [Populating a Database](https://www.postgresql.org/docs/current/populate.html)、[UUID Functions](https://www.postgresql.org/docs/current/functions-uuid.html)、[Date/Time Types](https://www.postgresql.org/docs/current/datatype-datetime.html)、[Table Partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html)、[Asynchronous Commit](https://www.postgresql.org/docs/current/wal-async-commit.html)
- [ULID spec](https://github.com/ulid/spec)
- [Percona: UUIDs are popular, but bad for performance](https://www.percona.com/blog/uuids-are-popular-but-bad-for-performance-lets-discuss/)

## 出典未確認の項目

次は、出典のページに直接の記載がなく、整理者が補った記述。使う前に確かめる。

- 秒・ミリ秒の epoch を `BIGINT` で持つ案（仕様との一致は問題ごと）。
- MySQL のパーティションが外部キーを持てない点（公式の制限の章で確認する）。
- 未使用のインデックスを調べるビュー（`sys.schema_unused_indexes`、`pg_stat_user_indexes`）の使い方。
- 競技の規模（数百万行）ではパーティションよりインデックスが効く、という経験則。
