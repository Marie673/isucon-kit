# 10 並行処理・非同期・ジョブ

当日のマニュアルが優先する。応答の待ち時間・通知の方式・反映の猶予・ジョブの間隔・再起動後のデータ保持は、回ごとにルールが違う。**負荷テストの終了後に完了すべき処理の猶予**（ISUCON14 は決済が終了後 5 秒以内）や、再起動後にデータが残る条件を、必ず読む。非同期化・遅延書き込みは、永続化を捨てると失格になる。

## この章で扱う範囲

- 待ちの正体の切り分け（CPU か、I/O か、ロックか、スリープか）。
- 並行化（独立した呼び出しを同時に待つ）、ワーカーとキュー、バックプレッシャー。
- 一括処理（まとめて書く）、遅延書き込み（非同期で DB へ）と、その永続化の危険。
- 通知の配り方（ポーリング間隔の調整・ロングポーリング・SSE・WebSocket・Web Push）。
- 仕様で許される「応答の待ち時間」の調整（ISUCON14 の `retry_after_ms` 型）。
- ロックの粒度、デッドロック、マッチング・割り当てのアルゴリズム、同時実行数の上限（セマフォ）。

扱わないもの: キャッシュと無効化（08 章）、DB の接続プール・設定（04・05 章）、SQL とロックの SQL 側の細部（06 章）、ランタイムの GC・アロケータ（09 章）、複数台への分散（11 章）、外部 API の HTTP クライアント（13 章）。

## 最初に見るもの（当日の 5 分チェックリスト）

| 見るもの | 見方 | 分かること |
| --- | --- | --- |
| CPU が張り付いているか、空いているか | `top`・`vmstat 1`。**走行全体の平均でなく、山の窓で見る**（01 章） | 空いているのに遅い → 待ち（スリープ・ロック・外部・ポーリング）。張り付き → 非同期化では治らない |
| 一番多いリクエスト | alp の件数順。ポーリング系（通知・状態取得）が全体の何割か | 通知の方式・間隔の調整が効くか（ISUCON14 は初期実装が約 30ms ごとのポーリング。講評） |
| アプリ内の `sleep`・ticker・ジョブ | `grep -rn "Sleep\|time.Tick\|setInterval\|ticker\|interval"`、systemd の timer・サービス（ISUCON14 は `isuride-matcher` が 500ms ごとにマッチングを起動） | ジョブの間隔がスコアを決める箇所 |
| ロック待ち・デッドロック | MySQL: `SHOW ENGINE INNODB STATUS`、`innodb_print_all_deadlocks`。PostgreSQL: `pg_locks`、`pg_stat_activity` の `wait_event`。アプリ: pprof の mutex・block プロファイル（09 章） | ロックがボトルネックか。範囲を狭めるか、再試行か |
| 未コミットのトランザクション・接続の枯渇 | `SHOW PROCESSLIST` の `Sleep` で長い接続、`pg_stat_activity` の `idle in transaction` | コミット漏れ |
| 反映の猶予とタイムアウト | マニュアルの猶予時間・タイムアウトの表 | 一括・遅延書き込み・通知の間隔の上限 |
| 重い処理と、その配点 | API ごとの CPU 秒と配点 | 同時実行数を絞ってよい処理 |
| 再起動後に残るべきデータ | マニュアルの追試の手順 | 遅延書き込み・キューに溜めたデータの扱い |

## 待ちの切り分け

### 非同期化の前に、時間が何に使われているかを確かめる

- **効く条件**: 「遅いので非同期にしたい」と思ったとき。
- **やり方**: 区間ごとの時間を出す（トレース・ログ・pprof）。
  - CPU ボトルネック（JSON 生成・描画・署名）に非同期化は効かない。ISUCON2 本番で typester は、非同期実装を試したが、レンダリングを解消した後は従来方式で十分だったと報告した。
  - INSERT を goroutine に投げるだけでは改善しなかった（ISUCON11 予選。revert した）。
  - idle が多いなら、アプリ内の `sleep`、ベンチ側のポーリング間隔、外部サービスの待ち、ワーカー数の上限、DB のロック待ちを疑う（`isucon-kit` の実例は「無駄な通知」の仮説がログで「409 の嵐」に置き換わった）。
- **確かめ方**: 変更の前後で、同じ区間の時間を比べる。
- **危険**: 効かない非同期化は、複雑さと取りこぼしのリスクだけを残す。
- **出典**: 実例: [typester](https://unknownplace.org/memo/2012/11/07/1/)、[rymiyamoto](https://zenn.dev/rymiyamoto/articles/f635d1a4ad2f0e)。定石集 （出典は同ファイル内で、外部 URL なし）。

## 並行化

### 独立した呼び出しを同時に待つ

- **効く条件**: 1 つのハンドラが、互いに依存しない外部 API・DB 問い合わせを順番に待っている。トレースで直列の同じ幅のスパンが並ぶ。
- **やり方**: 依存図を書き、独立な部分だけを上限付きで並行実行する。両方の結果を確定してから応答する。
  ```go
  g, ctx := errgroup.WithContext(ctx)
  g.SetLimit(4)                                   // 同時 4 本まで
  g.Go(func error { return callPayment(ctx) })
  g.Go(func error { return callShipping(ctx) })
  if err := g.Wait; err != nil { /* 最初のエラーだけ返る */ }
  ```
  ```rust
  // Rust: 同じタスクで並行（並列ではない）。最初の Err で戻る
  let (user, orders) = tokio::try_join!(fetch_user(id), fetch_orders(id))?;
  // 別スレッドでの並列が要るなら tokio::spawn して JoinHandle を渡す
  ```
  - ISUCON9 予選: 購入処理の独立した外部 API 呼び出しを goroutine で並行して待ち、3010 点を記録した。講評も、購入で外部 API の呼び出しを並列化する案を挙げている。
  - ISUCON11 本選の講評は、総合点の一覧の N+1 が残る場合の妥協点として、「A と B を並列化する」程度を挙げている。
- **確かめ方**: 同じ API の p95 が下がったか。外部への要求数が増えていないか。
- **危険**:
  - **依存がある呼び出し**（片方の結果が次の入力・認可・副作用の前提）は順序を保つ。
  - **同じ決済を先行して並列に送らない**。ISUCON14 の感想戦で kawaemon は、冪等キー付きで同じ決済を複数同時送信する案を試し、成功後に届く重複要求で critical（CODE=35）になった。冪等性は投機的な並列化の許可ではない。
  - `try_join!` は並行であって並列ではない。ブロッキングする呼び出しが 1 つあると他も進まない。
  - `errgroup` は最初のエラーだけを返す。上限を 0 にして `Go` を呼ぶとブロックしたままになる。
  - 外部 API のタイムアウト後の再試行は、相手が受理した操作を取り消せたとは限らない。
- **出典**: [Go: errgroup](https://pkg.go.dev/golang.org/x/sync/errgroup)、[tokio: try_join!](https://docs.rs/tokio/latest/tokio/macro.try_join.html)、[ISUCON9 予選講評](https://isucon.net/archives/53789931.html)、[ISUCON11 本選講評](https://isucon.net/archives/56163308.html)。実例: [p1ass](https://blog.p1ass.com/posts/isucon9/)、[kawaemon](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)。

### 再試行と冪等キー

- **効く条件**: 外部 API（決済など）が一時的に失敗する。再試行の実装を自作している。
- **やり方**: 失敗を再試行するなら、**上限の回数・期限・間隔（指数バックオフ）を付け、冪等キーを付ける**。
  - ISUCON14: 作問側は決済の `Idempotency-Key` をブラフとして入れており、これに囚われたチームは少なかった（講評）。ponyo877 はライド ID を `Idempotency-Key` にして、自前のリトライ処理を省いた。kotapjp は外部決済 API のリトライを指数バックオフにした。
  - ISUCON9 予選: 仕様書にない nginx の 400 が時々返った外部 API に、400 のときのリトライを入れた。過大な同時要求が原因という説明は筆者の推測。
- **確かめ方**: 外部への要求数・失敗率・再試行の回数。
- **危険**: 入力の誤りの 400 を一律に再試行しない。成功した操作と同じものを重ねて送らない（上の）。ISUCON14 では、負荷テスト終了時点で行われていた決済は終了後 5 秒以内に完了する必要があり、超えると期待金額と一致せず FAIL になる（マニュアル）。
- **出典**: [ISUCON14 講評](https://isucon.net/archives/58869617.html)、[ISUCON14 マニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、[ponyo877](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)。実例: [kotapjp](https://zenn.dev/mixi/articles/10dd2dae1f1e00)、[takonomura](https://www.takono.io/posts/2019/09/isucon/)。

## ワーカーとキュー

### ワーカーの数・キューの長さ・待ち方

- **効く条件**: 重い処理（画像変換、集計、通知の配信、書き込み）が、リクエストの中で同期的に走っている。ジョブの取得に `sleep` ポーリングが使われている。
- **やり方**:
  - **上限付きのキュー＋固定数のワーカー**にする。無制限に goroutine を起こさない。ISUCON11 予選で、コンディションの INSERT を独自のキューで処理してワーカー数も調整したが、点は前後した。
  - Go: バッファ付きチャネル＋固定数の goroutine。Rust: tokio の `mpsc::channel(容量)`（bounded）。bounded は上限に達すると送信が空きができるまで待つ（バックプレッシャー）。`unbounded_channel` は送信が常に即座に完了し、受信が遅れるとメッセージが溜まり続ける。
  - **ジョブの待ちを `sleep` ポーリングからブロッキング取得に**。ISUCON10 本選の解答例（白金動物園）は、ジョブのキューを Redis にして `BRPOP` で待機した。kazeburo の感想戦は、エンキュー時に job ID を渡して、`RAND` による選択の競合をなくした。
  - **ワーカー数は増やしすぎない**。定石集に「並列 2 → 8 で TCP の再送が増えスコアが下がった」実例があるが、出典の記録は未確認。ISUCON8 予選の chakku は、ワーカーを 4 → 2 に減らして競合を隠したら約 29000 → 16000 点に下がり、根本の解決ではなかった。**負荷を下げて競合を隠す変更は、解決と取り違えない**。
  - CPU を使う処理を非同期ランタイムの上で走らせるときは、`spawn_blocking` の同時数をセマフォなどで制限する（tokio の公式の推奨）。長時間動くワーカーは `thread::spawn`。
- **確かめ方**: キューの長さ・待ち時間（キューに入ってから処理されるまで）・ワーカーの稼働率を計測する。ワーカー数を変えて、単独で比べる。
- **危険**:
  - キューに溜めたまま再起動すると失う。**再起動後に残るべき処理**は永続化する（次の節）。
  - キューが溢れたときの動作（待たせる・捨てる・エラーにする）を決める。捨てるとデータを失い、エラーにするとベンチの減点になる（次のバックプレッシャーの項）。
  - 非同期化した処理の完了前に、次の GET が来る。画像変換の非同期化は、直後の GET の要件を満たせず失敗した（ISUCON3 本選の tagomoris）。
- **出典**: [tokio: mpsc](https://docs.rs/tokio/latest/tokio/sync/mpsc/index.html)、[tokio: spawn_blocking](https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html)、[Redis: BRPOP](https://redis.io/docs/latest/commands/brpop/)、[ISUCON10 本選講評](https://isucon.net/archives/55140539.html)。[tagomoris](https://tagomoris.hatenablog.com/entry/20131111/1384129361)、[chakku](https://chakku.hatenablog.com/entry/2018/09/17/104513)、[kazeburo](https://kazeburo.hatenablog.com/entry/2020/10/14/111149)。

### バックプレッシャー（溢れたときにどうするか）

- **効く条件**: 山の瞬間に、キューやワーカーが処理しきれない量の要求が来る。
- **やり方**:
  | 動作 | 実装 | 向くもの |
  | --- | --- | --- |
  | 待たせる | bounded の `send.await`・バッファ付きチャネルへの送信（満杯でブロック） | 全部を処理すべき書き込み。ただし応答の遅れが利用者の次の行動に効く |
  | 捨てる／間引く | `try_send`・`select default` | 欠けてよいもの（ログ、重複する通知）。仕様が許す場合だけ |
  | エラーを返す | 429・503 | 仕様が許し、再試行が前提のとき |
  | まとめる | キューの中で同じキーを 1 つにする（coalescing） | 最新値だけで足りるもの（位置・状態） |
- **確かめ方**: キューの長さの最大値と、溢れた回数。ベンチの減点の内訳。
- **危険**:
  - 利用者の種類で応答を変えるのは禁止（ベンチだけを見分ける）。全員に同じ規則で。
  - tokio の bounded channel は、受信側が drop されると以降の送信がエラーになる。終了時は `close` で新規の送信を止め、残りを受信し切る。
- **出典**: [tokio: mpsc](https://docs.rs/tokio/latest/tokio/sync/mpsc/index.html)。

## 一括処理（まとめて書く）

### 書き込み・送信をまとめる

- **効く条件**: 1 件ずつの INSERT・外部送信が多く、DB・ネットワークの往復がボトルネック。仕様が反映の遅れ（猶予）を許している。
- **やり方**: 溜めて、**時間または件数・サイズの閾値**でまとめて書く。
  ```go
  func batchWriter(ch <-chan Row, every time.Duration, maxRows int) {
      t := time.NewTicker(every); defer t.Stop
      buf := make([]Row, 0, maxRows)
      flush := func { if len(buf) > 0 { bulkInsert(buf); buf = buf[:0] } }
      for {
          select {
          case r, ok := <-ch:
              if !ok { flush; return }       // 終了時に必ず flush
              if buf = append(buf, r); len(buf) >= maxRows { flush }
          case <-t.C:
              flush
          }
      }
  }
  ```
  - ISUCON10 予選: CSV の 1000 行を 1 件ずつ INSERT すると、負荷の高い終盤でタイムアウトしやすく、CSV 入稿のタイムアウトは即失格扱い。一括 INSERT（バルク）にする（講評）。
  - ISUCON11 予選: POST されたコンディションをキューに詰め、複数のリクエストをまとめて一定間隔で INSERT すると、さらに速くなる。根拠は、マニュアルがコンディションの反映の遅れを許容していること（講評）。
  - ISUCON10 本選: 0.3 秒以内に届いた複数の `report_benchmark_jobs` を待ち合わせ、リーダーボードの更新を 1 回にした。
  - ISUCON8 本選: 外部へのログ送信を、5 秒または未送信 1MB の到達でまとめて送る形にして、0 点から回復した。
  - ISUCON9 本選: 外部の決済キャンセルが 1 リクエストあたり約 1 秒かかる。ベンチは整合性チェックの前に 5 秒以上待つので、5 秒以内ごとにまとめてキャンセルした（講評）。
  - ISUCON14: 通知を、溜まっている分を 1 回のリクエストでまとめて返す（stream で全部返す）形にして、少ないクエリで多くの通知を返した（LayerX。通知の間隔を 300ms → 1000ms にした変更とあわせて 32003 → 39027）。位置情報は反映に 3 秒の猶予があり、非同期化・バルクにできる（講評）。
- **確かめ方**: 書き込みの SQL の `calls` が減ったか。遅延の最大値（溜めている時間）が猶予に収まるか。
- **危険**:
  - **反映の猶予を超えない**。ISUCON14 は座標の反映に 3 秒。ISUCON12 予選は失格処理の後の反映に 3 秒の猶予（講評）。閾値の時間は猶予より十分短く。
  - **終了時（SIGTERM）に flush する**。溜めたままのデータは、再起動で失う。
  - **順序**。同じ行への更新の順序がまとめで入れ替わらないようにする。
  - **失敗時の再送の重複**。バルク INSERT が途中で失敗したときの扱い（全体をやり直すか、重複を `INSERT IGNORE`・`UPSERT` で吸収するか）。
  - 一括の SQL が `max_allowed_packet` を超えない。0 件のとき構文エラーになる（Iceman はタグの INSERT をバルク化したとき、件数ゼロで構文エラーが出て原因特定に時間がかかった）。
- **出典**: [ISUCON10 予選講評](https://isucon.net/archives/55025156.html)、[ISUCON11 予選講評](https://isucon.net/archives/56044867.html)、[ISUCON9 本選講評](https://isucon.net/archives/53916974.html)、[ISUCON14 講評](https://isucon.net/archives/58869617.html)、[LayerX（ISUCON14）](https://zenn.dev/layerx/articles/3bd55b77e047c4)、[Iceman（ISUCON13）](https://zenn.dev/iceman/articles/5aad04f18b128f)。実例: [nomeaning](https://blog.hoge.cc/post/isucon-2018)、反映猶予 3 秒を使った非同期保存。

### 遅延書き込み（非同期で DB へ）と永続化の危険

- **効く条件**: 書き込みの同期待ち（commit・複製の待ち）が応答時間の大半。仕様が反映の猶予を許している。
- **やり方**: 選択肢を、失う可能性の小さい順に並べる。
  | 方式 | 失う範囲 | 注意 |
  | --- | --- | --- |
  | 同期で書く（既定） | なし | — |
  | まとめて書く（上の一括処理。数百 ms〜数秒） | 溜めている分 | SIGTERM で flush |
  | DB の非同期コミット（PostgreSQL の `synchronous_commit = off`） | 直近のコミット（`wal_writer_delay` の最大 3 倍の期間）。**データは壊れない**（WAL の順序で再生される） | トランザクション単位で設定できる。`fsync = off` はサーバー全体の同期を切り、**OS・ハードのクラッシュで破損しうる**（別物） |
  | MySQL の `innodb_flush_log_at_trx_commit = 2` | OS のクラッシュ・電源断で直近約 1 秒。プロセスだけが落ちても失わない | 出典未確認（マニュアルの該当ページを確認できなかった。04 章で確認する） |
  | アプリのメモリに溜めて別スレッドで DB へ書く | メモリにある分（プロセスが落ちると全部） | 永続化が無いとデータ保持違反 |
  | メモリだけ（DB に書かない） | 全部 | 再起動試験で失格（08 章） |
  - ISUCON12 本選の感想戦で NaruseJun は、DB 書き込みを goroutine で非同期化し、ユーザー × テーブル単位のアプリ側ロックを入れた（メモリキャッシュにヒットする経路はロックを避ける構成）。ISUCON14 の感想戦で kawaemon は、起動時に全データをメモリに載せ、更新は遅延して一括書き込みにした。
- **確かめ方**: **書き込み直後に `systemctl restart`・`reboot` して、書いたデータが全部あるか**。DB とメモリの差（書き込み待ちの件数）の最大値。
- **危険**:
  - **データ保持違反＝失格**。ISUCON14 のマニュアルは、負荷走行中に書き込まれたデータが再起動後に取得できなければ失格と定めている。kawaemon は本番で失格した。
  - **反映の猶予・5 秒ルール**。負荷テスト終了時点で行われていた決済は終了後 5 秒以内に完了する必要がある（ISUCON14）。
  - **非同期書き込みの失敗の扱い**（エラーを握りつぶして、読み取りと DB がずれる）。リトライ・ログ・メトリクス。
  - **書き込みの順序と依存**。A が失われて B だけが残る不整合は、DB の非同期コミットでは起きない（コミット順に再生）が、アプリの自前キューでは起きうる。
  - fsync を切る・永続化を捨てる手は、競技のルール・追試で失格になりうる（この章の冒頭）。
- **出典**: [PostgreSQL: Asynchronous Commit](https://www.postgresql.org/docs/current/wal-async-commit.html)、[MySQL: innodb_flush_log_at_trx_commit](https://dev.mysql.com/doc/refman/8.0/en/innodb-parameters.html)（本文で確認できず）、[ISUCON14 マニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、[kawaemon（失格・感想戦）](https://zenn.dev/kawaemon/articles/f40529e0c447f5)、[NaruseJun](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)。

## 通知の配り方

### 方式の選び方（ポーリング・ロングポーリング・SSE・WebSocket・Web Push）

- **効く条件**: 通知・状態取得のポーリングが、リクエスト数の大半を占めている。
- **やり方**: 次の表で、仕様と実装コストから選ぶ。**仕様が許す方式だけ**。
  | 方式 | 仕組み | 実績・注意 |
  | --- | --- | --- |
  | JSON のポーリングのまま、間隔を調整 | 応答で次の問い合わせ間隔（`retry_after_ms` 型）を指示 | ISUCON14 は多くのチームがこれでスコアを伸ばした。ライドを丁寧にキャッシュすれば JSON のままで 10 万点程度（講評の試し解き） |
  | ロングポーリング | 変化があるまで応答を保留し、変化で返す（タイムアウト付き） | ISUCON14 は通知のタイムアウトが 60 秒で、想定されていた（講評）。待機をプロキシに逃がす案は失敗した |
  | SSE | 1 本の HTTP 接続でサーバーから送る | 仕様が許すとき。上位でも必須ではなかった（講評）。即時通知（チャネル・Pub/Sub）と、接続を利用したキャッシュまで作らないと効果が出にくい |
  | WebSocket | 双方向の長い接続 | 仕様が許すとき。nginx の設定が要る |
  | Web Push | ブラウザの購読で送る | ISUCON10 本選。多くのチームは入れないか、入れても点が下がって戻した（thundering herd） |
  - 判断の基準: **メリットとコストを比べる**。ISUCON14 の講評は「ドキュメントに書いてあるから移行する」のではなく、コストと比べて判断することが重要で、移行を最初から捨てたチームもいた、と述べている。
- **確かめ方**: 通知系のリクエスト数と応答時間。通知の遅延（状態が変わってから届くまでの時間）が仕様の猶予（ISUCON14 は 3 秒）に収まるか。
- **危険**: 通知の順序・取りこぼし（次の項）。方式の変更は、仕様の通知形式（SSE なら接続直後に最新状態を即送る、など）を満たす必要がある。
- **出典**: [ISUCON14 講評](https://isucon.net/archives/58869617.html)、[ISUCON14 アプリケーションマニュアル](https://github.com/isucon/isucon14/blob/main/docs/ISURIDE.md)、[ISUCON10 本選講評](https://isucon.net/archives/55140539.html)。手元の実例 （通知・ポーリング・ジョブ起床待ちの分類）。

### ポーリング間隔・ロングポーリング

- **効く条件**: 初期実装が数十 ms 間隔でポーリングしていて、CPU・DB がそれで埋まっている。
- **やり方**:
  - 間隔を延ばす（次の「応答の待ち時間」の項）。
  - ロングポーリング: 変化が無ければ応答を保留する。待機は、メモリのチャネル・Redis の Pub/Sub・`BLPOP`・`BRPOP` で起こす。ISUCON7 予選で sorah は fetch を Pub/Sub でメッセージ更新まで待つ処理に変えたが、単独の効果は不明だった。同じ回で、待機だけの前段プロキシ（Go）を置いてアプリのスレッドを空ける案は、バグを直し切れず撤回した。
  - nginx を通すなら、保留が長いと切られる。nginx は上流が 60 秒（既定）データを送らないと接続を切る（WebSocket のドキュメントの記述。`proxy_read_timeout` で延ばす）。ベンチ側のタイムアウト（ISUCON14 の通知は 60 秒）より短いと、ベンチから見て失敗になる。
  - 通知は購読者がいない間に出たものを失うので、購読の開始時に**最新状態を即送る**。
- **確かめ方**: 通知のリクエスト数。保留中の接続数（`ss -s`）・メモリ。
- **危険**: 保留した接続がワーカー（スレッド・プロセス）を占有して、他のリクエストが詰まる（prefork 系・スレッド数が少ない実装）。タイムアウトの設定の食い違い。
- **出典**: [nginx: WebSocket proxying（60 秒の既定）](https://nginx.org/en/docs/http/websocket.html)、[ISUCON14 マニュアル（タイムアウト）](https://github.com/isucon/isucon14/blob/main/docs/manual.md)。[sorah](https://diary.sorah.jp/2017/10/23/isucon7q)。

### SSE

- **効く条件**: ポーリングが全体の大半で、仕様が SSE を許す（ISUCON14 は許した）。状態が変わったときだけ通知を送れる。
- **やり方**:
  - 仕様: `Content-Type: text/event-stream`。各メッセージは `data: ` に続けて 1 行の JSON、最後に空行。接続後に即座に最新状態を送る。状態が変わったときだけ即座に送るのが望ましい（ISUCON14 アプリケーションマニュアル）。
  - 実装: 状態が変わった書き込みの処理が、受信者ごとの通知キュー（Go はチャネル、Rust は `tokio::sync::mpsc`）に送る。SSE のハンドラがそれを受けて書く。
    ```rust
    // axum: 受信者ごとの mpsc を Sse のストリームにする
    async fn notify(State(app): State<App>, Path(id): Path<Id>) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
        let rx = app.subscribe(id);   // 現在の最新状態を最初に 1 件送り、以降は変更ごとに送る
        let stream = ReceiverStream::new(rx).map(|n| Ok(Event::default.data(n)));
        Sse::new(stream).keep_alive(KeepAlive::default)
    }
    ```
  - **`watch` は最新値だけを保持し、途中の値をすべて見られる保証が無い**（tokio）。ISUCON14 のマニュアルは、想定していない状態遷移の通知をエラーにしている。**1 つずつの状態遷移を全部送る必要があるなら、キュー（`mpsc`）**。`broadcast` は全受信者に全値を配る fan-out。
  - nginx: SSE の location で `proxy_buffering off`（応答のヘッダーに `X-Accel-Buffering: no` を付ける方法もある）。無いとイベントが溜まる。
  - 新着が無いときにも、仕様が求める情報（ISUCON6 本選の watcher_count）は定期的に送る。
  - コメント行（先頭が `:`）を定期的に送ると、タイムアウトを防げる（MDN）。
  - ISUCON14: ponyo877 は状態が変わったときだけ Go のチャネルに書き込み、SSE 側で受けて送る形にした。公式の解説に基づく改善として挙げている。kawaemon は椅子とユーザーごとに通知キューを持たせ、接続時に未送信分を配信した（送信完了を確実に知る手段が無いのが課題だった）。
- **確かめ方**: 通知のリクエスト数が、接続数に置き換わったか。通知の遅延。接続数とメモリ（fd の上限も。02 章）。
- **危険**:
  - **通知の順序**。ISUCON14 の本番で nomeaning（第 9 西東京市）は、SSE の導入で CODE=15（椅子がライドの完了通知を受け取る前に、別の新しいライドの通知を受け取った）が出て、最後まで特定できず投入できなかった。未導入のコードでも同じ失敗が起きたので、SSE 固有の原因とは断定できない。
  - 実装コスト。LayerX（4 位）は SSE をバグが取れず諦め、代わりにまとめて返す方式を採った。krtx（9 位）は実装の負荷が高く効果も未知数だったので手を付けず、Iceman（24 位）はコネクション維持のコストと通知基盤を時間内に作る自信が無いことから見送った。上位でも必須ではなかった（講評）。
  - HTTP/1.1 のブラウザは、同じドメインへの SSE が同時 6 接続までに制限される（MDN。ベンチは別）。
  - 再接続で `Last-Event-ID` を送らせる終了方法（当時のライブラリ固有）。
  - 即時通知にすると、通知の集中（thundering herd）が起きうる。
- **出典**: [ISUCON14 アプリケーションマニュアル](https://github.com/isucon/isucon14/blob/main/docs/ISURIDE.md)、[ISUCON14 講評](https://isucon.net/archives/58869617.html)、[MDN: Using server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)、[axum: sse](https://docs.rs/axum/latest/axum/response/sse/index.html)、[tokio: sync](https://docs.rs/tokio/latest/tokio/sync/index.html)、[ponyo877](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)、[kawaemon（感想戦）](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)、[LayerX](https://zenn.dev/layerx/articles/3bd55b77e047c4)、[nomeaning（ISUCON14）](https://blog.hoge.cc/post/isucon-14)。[kizkoh](https://kizkoh.hatenablog.com/entry/2016/10/24/092000)。

### WebSocket

- **効く条件**: 仕様が WebSocket を許し、双方向の長い接続が要る。
- **やり方**: nginx は `Upgrade`・`Connection` が hop-by-hop ヘッダーなので、明示的に渡す。
  ```nginx
  location /ws/ {
      proxy_pass http://backend;
      proxy_http_version 1.1;                       # 1.29.7 より前では必要
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_read_timeout 300s;                      # 既定の 60 秒を延ばす（または ping を送る）
  }
  ```
- **確かめ方**: 接続が維持されるか。ping の間隔。
- **危険**: 既定 60 秒で切断される。接続数の fd の上限。実装・保守のコストが SSE より大きい（出典未確認。競技の実例は、本調査では見つけられなかった）。
- **出典**: [nginx: WebSocket proxying](https://nginx.org/en/docs/http/websocket.html)。

### Web Push（ISUCON10 本選型）

- **効く条件**: 仕様が購読型の通知を持つ。通知の取得が重い。
- **やり方**: 購読があるクライアントには、ポーリングの応答で通知を返さない（二重配送を避ける）。全体通知はポーリングのまま、個別の通知を Push に分ける案が示されている。
  - ISUCON10 本選の解答例（白金動物園）: Web Push の購読があれば `GetNotifications` は通知を返さない。講評は、全体通知を Web Push にすると thundering herd が起きるので、全体通知はポーリングのまま、個別を Push にする案を示している。
- **確かめ方**: 通知の取得 API の件数。Push 送信の CPU・待ち。
- **危険**: **多くのチームは実装しなかったか、実装してもスコアが下がって戻した**（講評。即時通知で thundering herd が起きた）。hidekiy は 13:00 にサンプルを使って Web Push に変更し、catatsuy は Push 化後にスコアが約 1000 点に下がり、原因を確定できないまま実装を残した。**導入後に負荷の集中とスコアの低下を再計測する**。
- **出典**: [ISUCON10 本選講評](https://isucon.net/archives/55140539.html)。実例: [hidekiy](https://blog.hidekiy.com/2020/10/isucon10-8-isucon.html)、[catatsuy](https://zenn.dev/catatsuy/articles/f48e32c19529b6d1b119)。

## 仕様で許される「応答の待ち時間」の調整

### 次の問い合わせ間隔を返す（`retry_after_ms` 型）

- **効く条件**: 仕様が、応答で次の問い合わせまでの待ち時間をクライアントに指示することを許している。ISUCON14 は通知の JSON レスポンスの `retry_after_ms`（リファレンス実装は 30ms）。ポーリング系が全リクエストの大半。
- **やり方**: 間隔を延ばす。**数段階を単独で反映し、山（スコアが最大になる値）を探す**。
  - ISUCON14 の公式講評: JSON API のまま、この値を調整してスコアを伸ばしたチームが多かった。間隔を延ばすと、利用者や椅子が状態の変化に気づくまでの時間が伸び、満足度が下がる。負荷とのバランスを取る必要がある。
  - Iceman（24 位）: 30ms → 1000ms で、ベンチは落ちず、スコアは約 50% 向上した。さらに延ばすとベンチがランダムに落ち始めたので 1000ms を採用した。
  - LayerX（4 位）: 30ms → 100ms（4981 → 5578）、100ms → 300ms（ライドの完全終了の記録と同時で 23,894 → 30,936。どちらが効いたか分離できていない）、300ms → 1000ms（通知のまとめ返しと合わせて 32003 → 39027）。
  - 3 位のチーム: 作戦会議でポーリング間隔の調整を挙げ、最後の調整（マッチング間隔・ポーリング）で最終 52,150 点。
  - 初期実装の序盤は、頻度を下げて他の処理を進めるのも有効（講評）。
  - 同じ型の別の例: ISUCON11 予選の講評は、応答が遅いと閲覧できるページ数が減るので、`trend` の更新頻度を下げたり `dropProbability` を上げたりして負荷を絞ることも有効、と述べている。ISUCON9 予選の講評は、ログインの `bcrypt` が約 0.1 秒の CPU を使うが、`sleep` で負荷を下げると人気者出品に必要な人数が集まらないので、ログイン専用サーバーを分ける案を示している。
- **確かめ方**: 間隔を 1 段階ずつ変え、同じ commit で 2 本ずつ（ぶれの幅を超える差か）。通知の遅延の最大値が猶予（ISUCON14 は 3 秒）に収まるか。FAIL・減点の内訳。
- **危険**:
  - **間隔を延ばしすぎると、間欠的に失敗する**（Iceman）。満足度の評価が下がる（講評）。
  - **ルールを確認する**。仕様（マニュアル）が許している範囲だけ。ISUCON14 は `retry_after_ms` が仕様で、マッチングの間隔（`ISUCON_MATCHING_INTERVAL`）と `isuride-matcher` の仕様変更も、マニュアルが「自由に利用方法及び仕様を変更して構いません」と書いている。
  - **意図的な遅延で利用者の次の行動を変える手は別物**。応答を留めて次の行動を変える型は、グレーで、採用の前に仕様と失格の条件を確かめ、メインエージェントが根拠を記録して決める。503 で逃げない。全員に同じ規則で、返す中身はその時点の正しい状態、が最低条件。
  - 反映の猶予を超えると critical になる。
- **出典**: [ISUCON14 講評](https://isucon.net/archives/58869617.html)、[ISUCON14 アプリケーションマニュアル](https://github.com/isucon/isucon14/blob/main/docs/ISURIDE.md)、[ISUCON11 予選講評](https://isucon.net/archives/56044867.html)、[ISUCON9 予選講評](https://isucon.net/archives/53789931.html)、[Iceman（ISUCON14）](https://zenn.dev/iceman/articles/1780deef84eda8)、[LayerX（ISUCON14）](https://zenn.dev/layerx/articles/3bd55b77e047c4)、[3 位のチーム（モヒカンメモ）](https://blog.pinkumohikan.com/entry/isucon-14)。

## マッチング・割り当てのアルゴリズム

### 定期ジョブの間隔と、割り当ての質

- **効く条件**: 定期的なマッチング（ISUCON14 のライドと椅子の割り当て）が、スコアに直結する。待ちのライドが溜まる。
- **やり方**:
  1. **まず実行頻度を上げる**。ISUCON14 の初期実装は 500ms ごとに 1 件だけ、空き椅子をランダムに 1 つ割り当てる。講評は「アルゴリズムは変えず、マッチングの実行頻度を上げるだけで入賞したチームもあった」と述べている。3 位のチームは間隔を 0.5s → 0.1s にして 8775 → 25758 に伸びた。第 9 西東京市は 0.5 秒 → 0.05 秒にした（単独の寄与は分離されていない）。
  2. **1 回の処理で、待ちのライド全部を対象にする**。LayerX は「1 回のマッチングリクエストで複数 rides を処理」（4169 → 4981）。kawaemon も 1 件だけだった処理を全件対応にした。
  3. **近い椅子を優先し、遠すぎる組み合わせは見送る**。ISUCON14 の講評: ランダム性を除くとスコアが安定した。マニュアルの「ユーザーは地域内でしか移動しない」が根拠で、一定距離より遠い組み合わせを見送る。LayerX は、ランダム選択から空いている最近傍の椅子に変えて SELECT の N+1 も解消し、21372 まで伸びた。Iceman は、地域が異なれば割り当てない・距離が 200 を超える候補を除外する実装で、スコアに大きく効いた。地域の区別を外すと明確に下がった（遠くの椅子を向かわせるより、空きを待つほうがよかった）。
  4. **発展形**: 全体の移動距離を最小化する二部マッチング（最小費用流）。ponyo877 は配車を重み付き最大二部マッチングとみなし、最小費用流に帰着して解き、gRPC で別サーバーに切り出した。kawaemon は貪欲法から最適割り当て（ハンガリアン法）に変えたが、ベンチマーカーがクラッシュしたため、最適を狙わず目標スコアやマッチング数の上限を設けて調整した。
  5. **空き状態はメモリで持つ**。マッチ可能な椅子と待ちのライドを map・ソート済み構造で持ち、全件走査をやめる（ponyo877）。DB 側なら「割り当て可否」の列・最新位置テーブル・ライドの完了の記録を持つ（07 章。講評）。
  - 間隔の変更方法（ISUCON14）: `/home/isucon/env.sh` の `ISUCON_MATCHING_INTERVAL` を変えて `sudo systemctl restart isuride-matcher.service`。マニュアルは、`GET /api/internal/matching` と `isuride-matcher.service` の利用方法・仕様を自由に変えてよい、と書いている。
- **確かめ方**: マッチングの所要・待ちのライドの滞留数。完了ライド数。スコアの内訳（迎車距離・乗車後の距離・完了数。講評）。**1 つずつ変える**。
- **危険**:
  - **最近傍に変えるだけでは伸びないことがある**。3 位のチームは、最近傍の割り当てで乗車までの不満が 86.7% → 43.6% に下がったのにスコアは伸びなかった（ボトルネックがマッチング間隔に戻っていた）。「遠くまで乗るライドを優先する」実験もスコアが伸びず、2 つの実験に約 4 時間を使った。
  - **定期ジョブが前回の完了を待たずに並列で走る**と、同じ処理が重複して 1 ユーザーに複数の椅子が割り当たる。Iceman は `chair_id IS NULL` の条件を足しても直らず、真因は DB のサーバー上でも初期状態のアプリが動いてマッチングのジョブを実行していたこと（DB サーバーでアプリとジョブを止めて解決）。複数の matcher が動くなら、同じ椅子が重複しない排他制御が要る（講評）。
  - 通知の順序（完了通知より先に次のライドが届く）。
  - 最適化の計算量。ponyo877 は別サーバーに切り出した。ベンチマーカー側の想定を超えるとベンチが壊れうる（kawaemon）。
  - ISUCON8 予選の座席予約: 参照実装の座席の乱択が広い範囲を `FOR UPDATE` していた。空席を事前にレコードとして用意し、`ORDER BY ord LIMIT 1 FOR UPDATE SKIP LOCKED`（MySQL 8）で取り出す案が講評に示されている。`SKIP LOCKED` は一貫性のないビューを返すので、一般のトランザクション処理には向かない。
- **出典**: [ISUCON14 講評](https://isucon.net/archives/58869617.html)、[ISUCON14 アプリケーションマニュアル](https://github.com/isucon/isucon14/blob/main/docs/ISURIDE.md)、[ISUCON8 予選講評](https://isucon.net/archives/52520045.html)、[MySQL: Locking Reads](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html)、[3 位のチーム](https://blog.pinkumohikan.com/entry/isucon-14)、[LayerX](https://zenn.dev/layerx/articles/3bd55b77e047c4)、[Iceman](https://zenn.dev/iceman/articles/1780deef84eda8)、[ponyo877](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)、[kawaemon](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)。実例: [ganyariya](https://zenn.dev/ganariya/articles/isucon2024-ganyariya)。

## ロックの粒度とデッドロック

### アプリ内のロックの粒度（Go・Rust）

- **効く条件**: pprof の mutex・block プロファイルで、同じロックの待ちが上位。共有の map・状態 1 つに全リクエストが集まる。
- **やり方**: ロックの範囲を狭める。**まず区間計測で、本当にそのロックがボトルネックか確かめる**。
  - 単位を細かくする: ルーム単位（ISUCON7 本選。複雑な更新をルームごとのロックへ寄せて、ロールバックの検討が楽になった）、ユーザー × テーブル単位、キー単位（共有 `HashMap` 全体の `RwLock` を `DashMap` に）。
  - 種類を替える: `.await` を含まない短い排他は、非同期の `Mutex` を同期の `Mutex` に（高速化を報告、機序は筆者の推測）。`RWMutex` に替えただけでは小差で、区間の計測で待ちを特定し、保留していたトランザクション化を完成して約 4,000 点から約 17,000 点に進んだ。
  - **ロックの中で I/O・`.await` しない**。tokio のチュートリアルは、`MutexGuard` を `.await` またぎで持たない（`Send` でない。`drop` を書いてもスコープ判定で解決しない）、ロックを隠す構造体の同期メソッドにする、専用タスクとメッセージパッシングにする、シャーディングにする、を挙げている。
  - Go の `RWMutex`: 書き込み待ちがあると新しい `RLock` が待たされる。読み取りロックの再帰取得は禁止。
  - `DashMap`: 参照を保持したまま同じマップを操作するとデッドロックしうる。
- **確かめ方**: pprof の mutex・block。ロック取得の区間の時間。`-race`（Go）。
- **危険**: ロックの範囲を狭めると、不変条件が壊れる。**ロックを全削除して一度通っても、次の走行で整合性検証に失敗する**。プロセス内のロックは別プロセスを守らない（複数プロセス・複数台は DB のロックか 1 プロセスに寄せる）。
- **出典**: [tokio: Shared state](https://tokio.rs/tokio/tutorial/shared-state)、[Go: sync.RWMutex](https://pkg.go.dev/sync#RWMutex)、[DashMap](https://docs.rs/dashmap/latest/dashmap/struct.DashMap.html)。[kawaemon](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)、[agtn](https://agtn.hatenablog.com/entry/2017/11/26/141859)、[kakira9618](https://kakira96.hatenablog.com/entry/2022/07/25/234252)、[hidekiy](https://blog.hidekiy.com/2022/07/isucon12-isucon.html)。

### DB のロックの範囲を狭める

- **効く条件**: `SHOW ENGINE INNODB STATUS` や `pg_locks` で、同じ行・範囲の待ちが上位。更新が 1 つのキー（商品・予約・テナント）に集中。
- **やり方**:
  - **`FOR UPDATE` の範囲は、使われるインデックスで決まる**（検索で遭遇したインデックスレコードをロックする）。インデックスが無いと走査範囲が広がり、ロックも広がる（出典のページの推論）。条件の列にインデックスを作る。サブクエリの行は外側の `FOR UPDATE` ではロックされない。
  - 不要な `FOR UPDATE` を外す。ISUCON8 予選の講評: レポート処理の `FOR UPDATE` は不要で、ファントムリードを起こす効果があるだけ。ISUCON10 予選の講評: `SELECT FOR UPDATE` をやめて `UPDATE` と `affected_rows` の判定に置き換え、ロック時間を減らす案。catatsuyは、投稿時のロックを `FOR UPDATE` から `FOR SHARE` に変えた。
  - ロックの単位を小さくする。ISUCON9 本選の講評: 予約時のロックを車両単位にかけ、範囲をできるだけ狭くすると、予約の重複を防ぎつつ座席検索を続けられる。ISUCON8 予選の講評: 空席をレコードとして先に用意し、そこにロックを取る（存在しないレコードにはロックできず、広い範囲で直列化していたため）。`SKIP LOCKED` で空き行を取る案。
  - **ファイルロック（flock）を DB のトランザクションに**。ISUCON12 予選の講評: DELETE と INSERT を 1 つの SQLite トランザクションにまとめれば、flock を外せる。作問者の事前解答は、一括化した置換をトランザクションで保護し、読み取り経路の flock を外した。
  - ロック待ちの代わりに、ロックの待ち時間を決める・失敗したら再試行する。
- **確かめ方**: ロック待ちの件数・時間が減ったか。デッドロックの件数（`innodb_print_all_deadlocks`）。
- **危険**:
  - **トランザクションを使わずにロックだけ外すと、途中のデータが返ってベンチが FAIL する**（ISUCON12 予選の講評）。flock を外して一度は約 5,000 点が出たが、次の走行で DB が壊れた。
  - ロックを狭めても効果が出ないことがある（予約ロックの取得範囲を狭めても効果が出ない例）。
  - **トランザクションをそのまま剥がすと、開始時の検証に落ちる**。
  - 長い間開いたままのトランザクション・コミット漏れが接続を使い切る（ISUCON10 本選）。
  - `SKIP LOCKED`・`NOWAIT` は、ステートメントベースのレプリケーションでは unsafe。
- **出典**: [MySQL: Locking Reads](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html)、[PostgreSQL: Explicit Locking](https://www.postgresql.org/docs/current/explicit-locking.html)、[ISUCON12 予選講評](https://isucon.net/archives/56850281.html)、[ISUCON10 予選講評](https://isucon.net/archives/55025156.html)、[ISUCON9 本選講評](https://isucon.net/archives/53916974.html)、[ISUCON8 予選講評](https://isucon.net/archives/52520045.html)、[catatsuy](https://zenn.dev/catatsuy/articles/159edffe202c0c)。実例: [がんもどき](https://trap.jp/post/1131/)、[acidlemon](https://isucon.net/archives/56842718.html)。

### デッドロックの扱い

- **効く条件**: エラーに `Deadlock found when trying to get lock`（MySQL 1213）・`deadlock detected`（PostgreSQL）が出る。ベンチが間欠的に FAIL する。
- **やり方**:
  1. **アプリは、デッドロックを起こりうるものとして、再試行する**（短いトランザクションで、回数と期限を付ける）。MySQL のマニュアルは、トランザクションが失敗して再試行が必要なケースをアプリが必ず処理する必要がある、と述べている。PostgreSQL も、順序を完全に検証できないなら、中止されたトランザクションを再試行する、と述べている。
  2. **同じ順序でロックを取る**（複数の行・テーブルを、全てのトランザクションで同じ順序に。`ORDER BY id FOR UPDATE`）。
  3. トランザクションを小さく・短くする。開いたままにしない。
  4. `UPDATE`・`FOR UPDATE` の条件にインデックスを使う（範囲ロックを減らす）。
  5. 診断: `SHOW ENGINE INNODB STATUS`（直近 1 件）、`innodb_print_all_deadlocks`（全件をエラーログへ）。
  - 分離レベルはデッドロックの発生可能性に影響しない（デッドロックは書き込みで起きる。MySQL のマニュアル）。
- **確かめ方**: エラーログのデッドロックの件数。再試行の回数。
- **危険**:
  - **ワーカーを減らして競合を隠すのは解決ではない**。
  - メモリに持つ変更（キャッシュ・インメモリ化）の後にデッドロックが出て断念した例（ISUCON10 本選で contestant をメモリに載せる変更。DB との同期の競合）。
  - 再試行で副作用（外部 API・通知）が重複しない（再試行はトランザクション全体を、外部の呼び出しの前からやり直す）。
  - Rust: `DashMap` の参照を保持したままの再入。`std::sync::Mutex` を `.await` またぎで持つ。
- **出典**: [MySQL: InnoDB Deadlocks](https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks.html)、[PostgreSQL: Explicit Locking（デッドロック）](https://www.postgresql.org/docs/current/explicit-locking.html)。[がんもどき](https://trap.jp/post/1131/)。

## 同時実行数の上限（セマフォ）で重い処理を絞る

### 重いが配点の小さい処理の同時数を絞る

- **効く条件**: 配点の小さい API（画像の取得・検査など）が CPU や帯域を大きく使い、配点の大きい操作と山の瞬間に競合している。山の窓で、重い処理の待ち（枠の空き待ち）が処理時間を桁違いに上回る。
- **やり方**: 重い処理の同時数に上限を設ける。
  ```go
  // Go: x/sync/semaphore（待つ）。TryAcquire で待たずに諦める選択もある
  var sem = semaphore.NewWeighted(4)
  func heavy(ctx context.Context) error {
      if err := sem.Acquire(ctx, 1); err != nil { return err }   // ctx が終わると解除
      defer sem.Release(1)
      return doHeavy
  }
  ```
  ```rust
  // Rust: tokio::sync::Semaphore。FIFO で許可証を与える。CPU 重い処理は spawn_blocking 内で
  let permit = sem.clone.acquire_owned.await?;          // タスク境界を越えて持てる
  let out = tokio::task::spawn_blocking(move || { let r = heavy; drop(permit); r }).await?;
  ```
  - Go では `errgroup` の `SetLimit`、バッファ付きチャネル（トークン）でも同じことができる。
- **確かめ方**: 枠の空き待ち（`decode_wait` のようなスパン）と、処理（`decode_run`）の比。待ちが桁違いなら CPU 飽和で、**枠を増やしても効かない**。同時数を 1 段階ずつ変えて、配点の大きい操作の p95 と成功数を見る。
- **危険**:
  - **503 で逃がさない**（上のバックプレッシャーの項）。待たせる（セマフォ）なら、待ちの上限（タイムアウト）がベンチのタイムアウトより短いこと。
  - 絞ると、その API の件数が減る。配スコアの式で、得る点と失う点を比べる（型 3）。加点に上限があるなら、超えた分を絞っても減点にならない。
  - **tokio のセマフォは FIFO**。`acquire_many` で多くの許可証を要求している呼び出しが先頭にあると、後続の軽い `acquire` も待たされる。待ち中にキャンセルすると順番を失う。
  - セマフォの許可証の解放漏れ（早期 return・panic）。Go は `defer`、Rust は `Permit` を drop するスコープに置く。
  - 優先度を下げて同居させると、DB と取り合って DB が負ける。
- **出典**: [Go: semaphore](https://pkg.go.dev/golang.org/x/sync/semaphore)、[Go: errgroup](https://pkg.go.dev/golang.org/x/sync/errgroup)、[tokio: Semaphore](https://docs.rs/tokio/latest/tokio/sync/struct.Semaphore.html)、[tokio: spawn_blocking](https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html)。

## よくある失敗

| 失敗 | 防ぎ方 |
| --- | --- |
| CPU ボトルネックなのに非同期化する／INSERT を goroutine に投げるだけ | 先に区間の時間を測る。効かなければ戻す |
| 遅延書き込み・メモリのキューが再起動で消えて失格（データ保持違反） | 書き込みの直後に reboot して確かめる。SIGTERM で flush。永続化の方式を先に決める |
| 一括処理の閾値が反映の猶予を超える／終了時に flush しない | 閾値は猶予より十分短く。終了時にチャネルを閉じて flush |
| ポーリング間隔を延ばしすぎ、間欠的に FAIL する／満足度が下がる | 数段階を単独で反映して山を探す。同じ commit を 2 本ずつ |
| SSE・Web Push を入れて通知の順序が崩れる／点が下がる | 状態遷移を 1 つずつ全部送る（`watch` でなくキュー）。導入後に負荷とスコアを再計測。コストと比べて入れない選択もある |
| 即時通知で thundering herd | 全体通知はポーリングのまま、個別だけを Push に |
| 最近傍のマッチングだけを入れて伸びない／定期ジョブが重複して走る | 間隔と同時に直す。1 つずつ変える。ジョブの重複起動（別の台の初期アプリを含む）を止める |
| ロックを全削除して一度通る | 範囲を狭める。トランザクションで置き換える。次の走行でも通ること |
| ワーカーを減らして競合を隠す | 競合の原因（ロック範囲・順序）を直す |
| `.await` またぎで `MutexGuard`・`DashMap` の参照を持つ | ロックを隠す同期メソッド。参照は短く。`try_*` |
| 503・429 でセマフォの溢れを逃がす | 待たせる（タイムアウトはベンチより短く）。全員に同じ規則 |
| セマフォを増やして枠の待ちを解こうとするが CPU 飽和 | 枠の空き待ちと処理時間の比を見る。台を増やす・処理を軽くする |

## 出典一覧

### ISUCON 公式の講評・マニュアル

- [ISUCON14 問題の解説と講評](https://isucon.net/archives/58869617.html)、[ISUCON14 マニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、[ISUCON14 アプリケーションマニュアル（ISURIDE）](https://github.com/isucon/isucon14/blob/main/docs/ISURIDE.md)
- [ISUCON12 予選問題の解説と講評](https://isucon.net/archives/56850281.html)、[ISUCON12 予選の作問者の事前解答](https://isucon.net/archives/56842718.html)
- [ISUCON11 予選問題の解説と講評](https://isucon.net/archives/56044867.html)、[ISUCON11 本選問題の解説と講評](https://isucon.net/archives/56163308.html)
- [ISUCON10 予選問題の解説と講評](https://isucon.net/archives/55025156.html)、[ISUCON10 本選問題の解説と講評](https://isucon.net/archives/55140539.html)
- [ISUCON9 予選問題の解説と講評](https://isucon.net/archives/53789931.html)、[ISUCON9 本選問題の解説と講評](https://isucon.net/archives/53916974.html)
- [ISUCON8 予選問題の解説と講評](https://isucon.net/archives/52520045.html)

### 参加記・出題者・常連の記事

- ISUCON14: [LayerX（4 位）](https://zenn.dev/layerx/articles/3bd55b77e047c4)、[3 位のチーム（モヒカンメモ）](https://blog.pinkumohikan.com/entry/isucon-14)、[Iceman](https://zenn.dev/iceman/articles/1780deef84eda8)、[krtx](https://krtx.hatenablog.com/entry/2024/12/09/131052)、[RAKUS](https://tech-blog.rakus.co.jp/entry/20241210/isucon14)、[kawaemon（失格）](https://zenn.dev/kawaemon/articles/f40529e0c447f5)、[kawaemon（感想戦）](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)、[ponyo877（感想戦）](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)、[nomeaning（第 9 西東京市）](https://blog.hoge.cc/post/isucon-14)
- ISUCON13: [Iceman](https://zenn.dev/iceman/articles/5aad04f18b128f)
- [NaruseJun（ISUCON12 本選優勝）](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)

### 公式ドキュメント

- Go: [errgroup](https://pkg.go.dev/golang.org/x/sync/errgroup)、[semaphore](https://pkg.go.dev/golang.org/x/sync/semaphore)、[sync（RWMutex）](https://pkg.go.dev/sync#RWMutex)
- Rust: [tokio: sync](https://docs.rs/tokio/latest/tokio/sync/index.html)、[tokio: mpsc](https://docs.rs/tokio/latest/tokio/sync/mpsc/index.html)、[tokio: Semaphore](https://docs.rs/tokio/latest/tokio/sync/struct.Semaphore.html)、[tokio: spawn_blocking](https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html)、[tokio: try_join!](https://docs.rs/tokio/latest/tokio/macro.try_join.html)、[tokio: Shared state](https://tokio.rs/tokio/tutorial/shared-state)、[axum: sse](https://docs.rs/axum/latest/axum/response/sse/index.html)、[DashMap](https://docs.rs/dashmap/latest/dashmap/struct.DashMap.html)
- [MDN: Using server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)、[nginx: WebSocket proxying](https://nginx.org/en/docs/http/websocket.html)
- [MySQL: Locking Reads](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html)、[MySQL: InnoDB Deadlocks](https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks.html)、[PostgreSQL: Explicit Locking](https://www.postgresql.org/docs/current/explicit-locking.html)、[PostgreSQL: Asynchronous Commit](https://www.postgresql.org/docs/current/wal-async-commit.html)、[Redis: BRPOP](https://redis.io/docs/latest/commands/brpop/)

## 出典未確認の項目

次は、出典のページに直接の記載がなく、整理者が補った記述。使う前に確かめる。

- MySQL の `innodb_flush_log_at_trx_commit = 2` の挙動（値ごとの損失の範囲。取得したページの本文に該当箇所が無かった。04 章で確認する）。
- 定石集の「並列 2 → 8 で TCP の再送が増えスコアが下がった」実例（定石集に出典の URL が無い）。
- WebSocket が SSE より実装・保守のコストが大きい、という比較（競技の実例は見つけられなかった）。
- `FOR UPDATE` の範囲がインデックスに依存して広がる点は、MySQL のマニュアルの記述（検索で遭遇したインデックスレコードをロックする）からの推論。
- Rust の SSE の受信者ごとの `mpsc` の例（axum・tokio のドキュメントの型を組み合わせた整理者のコード。競技の実例ではない）。
