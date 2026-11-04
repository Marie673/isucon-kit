# 08 キャッシュとインメモリ化

当日のマニュアルが優先する。キャッシュしてよい鮮度（反映の猶予が 1 秒・3 秒・30 秒など）、再起動後のデータ保持、初期化の範囲、複数台の構成は、回ごとにルールが違う。再起動後にデータが残らないインメモリ化は、追試で失格になる。

## この章で扱う範囲

- プロセス内キャッシュ（Go の map＋ロック・`sync.Map`、Rust の `DashMap`・`moka`）と、TTL・無効化・世代番号。
- **書き込み側から無効化を送る作りと、読むたびに鮮度を聞く作りの違い**。
- singleflight、HTTP 応答のキャッシュ（nginx、アプリ内）、Redis・memcached・Valkey。
- 全面インメモリ化（DB を永続化だけにする）と、その永続化・追試の危険。
- initialize でのキャッシュの作り直しと暖気、複数プロセス・複数台での整合、メモリの見積もり。
- 整合性の検査（書いた直後の読み）に落ちない条件。

扱わないもの: 結果を DB に保存する非正規化（07 章）、nginx の設定そのもの（03 章）、画像のファイル配信（12 章）、ワーカー・キュー・通知（10 章）、複数台の役割分担（11 章）。

## 最初に見るもの（当日の 5 分チェックリスト）

キャッシュは「同じ参照が繰り返されている」ことを数えてから入れる。「麻薬」と呼ばれる（ISUCON8 予選の講評）。入れるのも、途中でやめるのも難しい。

| 見るもの | 見方 | 分かること |
| --- | --- | --- |
| 同じ SQL がどれだけ繰り返されるか | スロークエリ・`pg_stat_statements` の `calls` 上位。同じ ID・同じ引数の繰り返し（トレースで 1 リクエスト内の重複も） | キャッシュで消せる往復の数。N+1 は先に JOIN・一括取得で直す（06 章） |
| 読み:書きの比と、書き込みの経路 | alp の `GET` と `POST/PUT/DELETE` の件数。更新する SQL を grep で全部数える | 無効化の経路の数。経路が多い・漏れそうなら TTL か参照ごとの確認に倒す |
| 鮮度の仕様 | マニュアルの「反映の猶予」 | TTL の上限。猶予が無いものは書き込み時の無効化だけ |
| 書き込むプロセス・台は何か | アプリのプロセス数（prefork・複数台・読みと書きで別の台） | プロセス内キャッシュの無効化が届くか。届かないなら Redis か、台を 1 つに寄せる |
| 変わらないと言えるもの | 書き込み経路の無いテーブル、マスター、トークン→ID | 起動時に載せるだけで済むもの（無効化が要らない） |
| メモリの余裕 | `free -m`、アプリの RSS、DB のバッファ。競技によっては 1GB 制限 | キャッシュに使える量。載せきれないなら対象を絞る |
| initialize が戻すもの | initialize の処理と、メモリ・Redis・ファイルの有無 | キャッシュの作り直しが要る場所 |
| 再起動試験の条件 | マニュアルの追試手順 | 永続化の要否。メモリだけに持ったデータは再起動で消える |

## キャッシュ前の確認

### 本当に効くかを数える

- **効く条件**: キャッシュを入れたいが、上位の SQL が本当に「同じ値の繰り返し」かが分からない。
- **やり方**: キャッシュを入れる前に次を数える。
  1. 該当 SQL の `calls` と、そのうち同じ引数の割合（ログかトレースで）。
  2. 取得のコスト（1 回の時間）と、キャッシュの参照のコスト（ロック・シリアライズ）。
  3. 書き込みで無効になる頻度。
  効かない例がある。
  - リクエストのたびに全件を読み直すキャッシュは、むしろ遅くなる（ISUCON6 予選の練習）。
  - 重いのが比較処理（bcrypt の照合）のとき、オブジェクトをキャッシュしても照合コストは消えない。
  - 表示用のキャッシュを作るために追加の SELECT を足し、購入が遅くなった。
  - 検索の総件数だけをメモリ化しても点はあまり上がらなかった。
- **確かめ方**: キャッシュを入れた走行の前後で、該当 SQL の `calls` が実際に減ったか（`pg_stat_statements` の走行ごとの差分）。hit・miss・evict を数える。
- **危険**: キャッシュの効果が検証されていない段階で入れると、効果を測れないまま工数を使う（ISUCON10 予選の講評は、アプリ内キャッシュの効果を運営は未検証と明記している）。
- **出典**: [ISUCON10 予選講評](https://isucon.net/archives/55025156.html)、[ISUCON8 予選講評](https://isucon.net/archives/52520045.html)。実例: [hotpepsi](https://qiita.com/hotpepsi/items/c360a2d71f4616dbc026)、[matsuu](https://matsuu.hatenablog.com/entry/20121105/1352123423)、[methane](https://methane.hatenablog.jp/entry/2020/09/14/ISUCON10予選敗退してきました)。

### 共有データを書き換えない

- **効く条件**: キャッシュした値（スライス・マップ・オブジェクト）を、呼び出し側が受け取って加工している。
- **やり方**: キャッシュの値は**不変**として扱う。加工するならコピーしてから。Go ではスライス・マップは参照で渡る。Rust では `Arc<T>` で持ち、書き換えが要るなら新しい値を作って差し替える。
- **確かめ方**: キャッシュから取り出した値を加工するコードを grep する。`-race`（Go）で同時書き込みを検出する。
- **危険**: ISUCON8 予選の講評は、多くの実装が `events`・`sheets` のキャッシュの参照を受け取って破壊的に変更しており、DB から毎回作り直すコードでは問題にならなかったが、キャッシュ化すると共有データが壊れてバグの原因になった、と述べている。
- **出典**: [ISUCON8 予選講評](https://isucon.net/archives/52520045.html)。

## 起動時に載せるもの（無効化が要らないキャッシュ）

### 変わらないものを起動時にメモリへ

- **効く条件**: 書き込み経路が無いテーブル（マスター、タグ一覧）、ベンチ開始時に渡され以後変わらない値、変わらないトークンから ID への対応。
- **やり方**: 起動時と initialize の後に読み込み、以後は読むだけにする。
  ```go
  var tags atomic.Pointer[[]Tag]   // Go: 作り直しは Store で差し替える（読み側はロック不要）
  func loadTags { v := queryTags; tags.Store(&v) }
  ```
  - 「書き込み経路が無い」ことは、コードの `INSERT/UPDATE/DELETE` の grep と、マニュアルの API 一覧で確かめる。
  - ISUCON12 本選: マスターはすべてキャッシュに載せる想定だったが、マスターの更新は実際に起こる。講評は、キャッシュのキーにマスターのバージョンを含めると、パージ漏れがあっても古い版が参照されにくくなる、と述べている。
  - ISUCON14: Iceman は、決済先の URL（ベンチ開始時に渡され以後変わらない）を、一度 DB から読んだ後にメモリで使い回した。ただし**2 回目のベンチでアプリが落ちた**。initialize でキャッシュを消していなかったため。
- **確かめ方**: 該当 SQL が走行中に 1 回も出ないか。**initialize を 2 回続けて叩いても**動くか。
- **危険**:
  - 初期化で値が変わるもの（決済先 URL など）は、initialize でキャッシュを消して読み直す。
  - 「変わらない」と思ったものが変わる。ISUCON12 本選のマスターは更新がある。ISUCON5 本選では、更新頻度の違う API を一律に無期限でキャッシュして間欠的に失敗し、API ごとの期限に変えた。
  - 載せる量がメモリを超えない（ISUCON9 本選は搭載メモリ 1GB で、時刻マスターが 2,807,913 件あり、全件をメモリに載せる力技は通用しなかった。講評）。
- **出典**: [ISUCON12 本選講評](https://isucon.net/archives/56959385.html)、[ISUCON9 本選講評](https://isucon.net/archives/53916974.html)、Iceman（[ISUCON14 参加記](https://zenn.dev/iceman/articles/1780deef84eda8)）。実例: [p1ass](https://blog.p1ass.com/posts/isucon12-final/)、トークン→ID。効果は「微量」、[fujiwara](https://sfujiwara.hatenablog.com/entry/2015/11/02/103440)。

## プロセス内キャッシュの実装

### Go: map＋ロック、`sync.Map`、シャーディング

- **効く条件**: 参照の多いキーが決まっていて、1 つのプロセス（または 1 台）で完結する。
- **やり方**: 最初は map と `sync.RWMutex` でよい。
  ```go
  type Cache[K comparable, V any] struct {
      mu sync.RWMutex
      m  map[K]V
  }
  func (c *Cache[K, V]) Get(k K) (V, bool) { c.mu.RLock; defer c.mu.RUnlock; v, ok := c.m[k]; return v, ok }
  func (c *Cache[K, V]) Set(k K, v V)      { c.mu.Lock; c.m[k] = v; c.mu.Unlock }
  func (c *Cache[K, V]) Delete(k K)        { c.mu.Lock; delete(c.m, k); c.mu.Unlock }
  ```
  - **Mutex か RWMutex か**は、読みと書きのどちらが重いかで選ぶ（catatsuy のキャッシュライブラリは、書き込み重視に `sync.Mutex`、読み取り重視に `sync.RWMutex` を使い分けている）。
  - **`sync.Map`** は特殊用途向け。公式は「一度だけ書かれて何度も読まれるキー（増えるだけのキャッシュ）」と「複数の goroutine が重ならないキーを読み書きする場合」に向くと述べ、どちらでもなければ通常の map と別のロックを使うべきだとしている。型安全性と不変条件の維持のため。`Range` は一貫したスナップショットではない。
  - **`RWMutex`** は、書き込み待ちがあると新しい `RLock` が待たされる。**読み取りロックの再帰取得は禁止**（書き込み待ちが挟まるとデッドロックする）。ロックの昇格・降格はできない。`Mutex`・`RWMutex`・`WaitGroup`・`Once` は初回の使用後にコピーしない。
  - 競合がロック待ちに現れたら、キーのハッシュでシャーディングする（`[N]struct{mu; m}`）。
  - 重い取得の同時実行は singleflight（後述）。
  - 型付きのキャッシュ・TTL・非同期更新・singleflight をまとめたライブラリに、motoki317 の `sc`、catatsuy のキャッシュライブラリがある（標準以外の依存を入れる前に、ビルドと使い方を事前に確かめておく）。
- **確かめ方**: `go test -race`。pprof の mutex・block プロファイルでロック待ちを見る（09 章）。hit 率。
- **危険**: キーの取り違え・map の同時読み書き（`fatal error: concurrent map read and map write`）・更新時の過負荷（stampede）が典型の罠（motoki317）。TTL と無効化は利用者の責任で、ライブラリでは防げない。
- **出典**: [Go: sync.Map](https://pkg.go.dev/sync#Map)、[Go: sync.RWMutex など](https://pkg.go.dev/sync#RWMutex)、[catatsuy: キャッシュライブラリ](https://zenn.dev/catatsuy/articles/25f68f52c9e487)、[motoki317: Effective generic cache in Golang](https://speakerdeck.com/motoki317/effective-generic-cache-in-golang)。実例: セッション。

### Rust: `parking_lot`・`DashMap`・`moka`

- **効く条件**: Rust で書き直したアプリで、Go の map＋ロックと同じ用途。
- **やり方**:
  - **短い排他は同期の `Mutex`/`RwLock`**（`std` か `parking_lot`）。**`.await` をまたいでロックを持たない**。tokio のチュートリアルは、短いクリティカルセクションで `.await` をまたがないなら `std::sync::Mutex` で十分で、`tokio::sync::Mutex` は内部で同期ミューテックスを使うため通常は高コストだと述べている。
  - 競合が目立てば、**シャーディング**（`hash(key) % N` で分けた `Mutex<HashMap>`）か **`DashMap`**。`DashMap` は、マップへの参照を持ったまま同じマップに `insert`・`remove`・`entry` などを呼ぶとデッドロックしうる（公式に明記）。参照は短く保ち、取り出して値をコピー（`.clone`）してからロックを手放す。デッドロックを避けたければ `try_get`・`try_entry`。
  - **TTL・容量・追い出しが要るなら `moka`**。`time_to_live`・`time_to_idle`・`max_capacity` を持ち、既定の追い出しは TinyLFU。**読み取りは並行**、ハッシュテーブルへの更新は即座に見える一方、ポリシー側は結果整合で `entry_count` は概算。`invalidate_all` は直後から古い値を返さないことが保証され、実際の削除は後で行う。
    ```rust
    use moka::sync::Cache;
    use std::time::Duration;
    let cache: Cache<u64, std::sync::Arc<User>> = Cache::builder
        .max_capacity(100_000)
        .time_to_live(Duration::from_secs(60))
        .build;
    let u = cache.get_with(id, || std::sync::Arc::new(load_user(id)));  // 同じキーの同時初期化は 1 回だけ実行される
    cache.invalidate(&id);                                               // 書き込み側から
    ```
    - ISUCON14 の感想戦で kawaemon は、非同期 `Mutex` を同期版に替え、共有 `HashMap` を `DashMap` に替え、さらに文字列のインターンを使った（単独の効果は不明）。
- **確かめ方**: ロック待ちを区間計測する（`RWLock` に替えただけでは伸びず、区間の計測で排他がボトルネックと分かって初めて進んだ）。tokio-console や `tracing` のスパンで、ロック取得の時間を出す。
- **危険**: `DashMap` の `Ref` を保持したままの再入。`moka` の `entry_count` を正確な値として使わない。非同期の `Mutex` を `.await` またぎで持つとデッドロックや `Send` のエラー。
- **出典**: [tokio: Shared state](https://tokio.rs/tokio/tutorial/shared-state)、[DashMap](https://docs.rs/dashmap/latest/dashmap/struct.DashMap.html)、[moka](https://docs.rs/moka/latest/moka/)、[moka::sync::Cache](https://docs.rs/moka/latest/moka/sync/struct.Cache.html)、[kawaemon（ISUCON14 感想戦）](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)。[hidekiy](https://blog.hidekiy.com/2022/07/isucon12-isucon.html)。

## TTL・無効化・世代番号

### 無効化の方式を選ぶ

- **効く条件**: 更新のある値をキャッシュする。
- **やり方**: 4 つの方式を、鮮度の仕様と更新の経路の数で選ぶ。
  | 方式 | いつ使うか | 例 |
  | --- | --- | --- |
  | 変わらないので無効化なし | 書き込み経路が無い |  |
  | **書き込み側から無効化** | 更新の経路が数えられて少ない。鮮度の猶予が無い | ISUCON12 予選の講評（ランキングは CSV のアップロード時にしか変わらない。応答全体を Redis などに載せ、アップロードのタイミングで破棄すれば不整合は起きにくい）。保存済み HTML を、変わったキーワードに関係する行だけ無効化 |
  | 世代番号（バージョン）をキーに含める | 無効化の漏れが心配。古い版が残ってよい | ISUCON12 本選のマスターバージョン（講評）。 |
  | TTL だけ | 鮮度の猶予が TTL より長く、少し古くてよい | ISUCON11 予選の `trend`（ただし変化がないと利用者が増えない設計だったので、鮮度の低下は加点に響いた。講評） |
  - **TTL だけに頼らない**。ISUCON10 本選で、1 秒のキャッシュに同時更新の抑制がないと、DB が少し遅くなっただけで古いランキングを返し失敗した。nginx の `proxy_cache_valid` で上限ぎりぎりの TTL を指定して不安定になった例もある。
  - 無効化の対象は、**更新の経路を全部列挙**してから決める。キーワードの追加・削除の反映漏れで FAIL した例。
- **確かめ方**: 「更新→直後の読み」を、キャッシュが温まった状態と冷えた状態の両方で。更新の経路ごとのテストを書く。
- **危険**: 更新の経路の漏れ（管理 API・削除・退会・initialize）。部分更新（キャッシュに載っている一覧に 1 件追加する差分更新）は、欠損時の復元が無いと表示全体を失う。
- **出典**: [ISUCON12 予選講評](https://isucon.net/archives/56850281.html)、[ISUCON12 本選講評](https://isucon.net/archives/56959385.html)、[ISUCON11 予選講評](https://isucon.net/archives/56044867.html)、[ISUCON10 本選講評](https://isucon.net/archives/55140539.html)。実例: [NaruseJun](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)、[tagomoris](https://tagomoris.hatenablog.com/entry/2016/09/20/193730)、[catatsuy](https://zenn.dev/catatsuy/articles/f48e32c19529b6d1b119)、[s-tajima](https://blog.s-tajima.work/post/2020/isucon10-final-hz46/)。

### 世代番号とバージョンをキーに含める

- **効く条件**: マスターのように、まとめて入れ替わるデータ。パージの漏れが心配。
- **やり方**: キャッシュのキーに世代（マスターのバージョン）を含める。世代が上がると、古い世代のキャッシュは参照されない。バージョンの確認は、バージョンが変わりうる入口（管理 API、ログイン時）だけにする（構成を単一アプリに固めた後、マスターの確認・更新を Admin API の使用時だけにした）。
- **確かめ方**: バージョン更新の前後で、キーが切り替わり、古い世代のメモリが増え続けないか（古い世代の掃除）。
- **危険**: **毎回バージョンを DB に聞く**と、その確認が 1 往復として残る。 は複数アプリを想定して、毎回バージョンだけを確認し、変わった時にマスターを再読込した（確認が軽い 1 本のクエリで、マスターは大きいので成り立った）。確認の対象が重い・遠い・大量なら、前の節の「読むたびに鮮度を聞く」と同じ失敗になる。単一アプリなら確認の契機を Admin API だけにできる。世代が上がった直後に再読込が集中する（thundering herd。singleflight で抑える。次の節）。
- **出典**: [ISUCON12 本選講評](https://isucon.net/archives/56959385.html)。[NaruseJun](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)。

## singleflight（同時の同じ取得を 1 本に）

### Go: `singleflight`

- **効く条件**: キャッシュが切れた瞬間・マスターが更新された瞬間・同じ重い集計に、同時に大量のリクエストが集中する（thundering herd、stampede）。`pg_stat_activity` に同じ SQL が並ぶ。
- **やり方**: 同じキーの処理が実行中なら、後から来た呼び出しは完了を待って同じ結果を受け取る。
  ```go
  var g singleflight.Group
  func getGrades(ctx context.Context, key string) (any, error) {
      v, err, _ := g.Do(key, func (any, error) { return computeGrades(ctx, key) })
      return v, err
  }
  ```
  - ISUCON11 本選: 終了した科目の総合点は永続的にキャッシュでき、GPA 一覧は科目の終了時以外は更新されないので、終了の間の呼び出しを使い回せる（講評）。ISUCON12 本選: `master_version` 変更後の再読込の集中を singleflight で抑え、スコアの揺れが収まった。ISUCON10 本選: キャッシュ更新が同時に殺到しないよう singleflight や Varnish を使う（講評）。公開状態が等しい 2 つのダッシュボードを同じ singleflight にまとめた例もある（13719 → 14162）。
- **確かめ方**: 集中の瞬間の同時 SQL 数（`pg_stat_activity`）。`Do` の `shared` が true の割合。
- **危険**:
  - **取得中に更新が来ると、更新後に来た読みが、更新前に始まった取得の結果を共有して古い値を返す**（motoki317 の指摘）。即時反映が要るなら、更新時に `Forget(key)` を呼ぶ。`Forget` 後の呼び出しは既存の処理を待たず新しく実行する。motoki317 の `sc` には、読み取りの開始前に始まった取得の結果を再利用しない `EnableStrictCoalescing`（ttl=0 のときの「Zero Time Cache」）があるが、キャッシュによる負荷軽減効果は小さくなる。
  - エラーも共有される。1 回の失敗が待っていた全員に返る。
  - catatsuy のライブラリのように実行後の変数の削除を非同期にする実装では、実行終了直後に古い値が返りうる（記事は一般の用途では問題になりにくいとしている）。
- **出典**: [Go: singleflight](https://pkg.go.dev/golang.org/x/sync/singleflight)、[motoki317](https://speakerdeck.com/motoki317/effective-generic-cache-in-golang)、[catatsuy: キャッシュライブラリ](https://zenn.dev/catatsuy/articles/25f68f52c9e487)、[ISUCON11 本選講評](https://isucon.net/archives/56163308.html)、[ISUCON10 本選講評](https://isucon.net/archives/55140539.html)、[catatsuy](https://zenn.dev/catatsuy/articles/159edffe202c0c)。実例: [fujiwara](https://sfujiwara.hatenablog.com/entry/isucon11-final)、[がんもどき](https://trap.jp/post/1131/)。

### Rust: `moka` の `get_with`

- **効く条件**: 上と同じ。Rust で書いている。
- **やり方**: `moka` の `get_with` は、同じキーへの同時呼び出しで初期化のクロージャを 1 回だけ実行し、他は完了まで待つ。`try_get_with`（`Err` を返すと挿入しない）、`optionally_get_with` も同様。`init` が panic すると、残りの呼び出しのうち 1 つが再実行する。非同期版は `moka::future::Cache`。
- **確かめ方**: 集中の瞬間に、取得の SQL が 1 本だけ走るか。
- **危険**: `get_with` のクロージャの中で重いブロッキング処理をすると、待っている全員を巻き込む。更新との競合（更新後に古い取得の結果を共有）は Go と同じ。更新の直後に `invalidate` しても、その時点で実行中の取得が古い値を入れうる。
- **出典**: [moka::sync::Cache](https://docs.rs/moka/latest/moka/sync/struct.Cache.html)。

## HTTP 応答そのもののキャッシュ

### nginx の `proxy_cache`

- **効く条件**: 認証・パラメータが無い、または少数のキーで決まる GET の応答（ダッシュボード、ランキング、トレンド）が、同じ内容で繰り返し返っている。ISUCON10 本選のオーディエンス向けダッシュボード（最大 1 秒のキャッシュが許された）、ISUCON11 予選の `GET /api/trend`。
- **やり方**: 設定の要点（nginx の公式ドキュメントから）。
  ```nginx
  proxy_cache_path /var/cache/nginx keys_zone=app:10m max_size=1g inactive=10m;
  location /api/dashboard {
      proxy_pass http://app;
      proxy_cache app;
      proxy_cache_key "$scheme$host$request_uri";
      proxy_cache_valid 200 1s;
      proxy_cache_lock on;                          # 同じキーの生成を 1 リクエストに限定し、他は待つ
      proxy_cache_use_stale updating error timeout; # 更新中は古い応答を返す
      proxy_cache_background_update on;             # use_stale updating が前提
  }
  ```
  - `proxy_cache_lock` は既定で off。`proxy_cache_lock_timeout` は既定 5 秒（期限が切れたリクエストは上流へ渡されるが、その応答はキャッシュされない）。
  - `proxy_cache_valid` は時間だけを書くと 200・301・302 だけが対象になる。`X-Accel-Expires` が最優先、次に `Expires`・`Cache-Control`。**`Set-Cookie` を含む応答はキャッシュされない**。
  - `proxy_cache_key` の既定は `$scheme$proxy_host$request_uri`。認証・ユーザーで応答が変わるなら、キーに含めるか、キャッシュしない（`proxy_cache_bypass`・`proxy_no_cache`）。
  - アプリ側でヘッダー（`X-Accel-Expires`）を返す方式にすると、無効化の制御をアプリに寄せられる。
- **確かめ方**: `$upstream_cache_status` をアクセスログに出し、HIT/MISS/EXPIRED/UPDATING の割合を alp で見る（01・03 章）。全 MISS なら設定が効いていない（Squid で全 MISS の調査に約 1 時間を使って断念した）。
- **危険**:
  - **鮮度の仕様**。ISUCON10 本選は「1 秒以内に結果が反映される」条件があり、単純なキャッシュは負荷が高いとき条件を満たせず、古いリーダーボードのエラーが出た（講評）。`proxy_cache_valid` のミリ秒指定が使えず上限ぎりぎりの TTL で不安定になった例もある。**書き込みで無効化できない**ので、猶予が TTL より長いものだけに使う。
  - 個人化された応答を共有キャッシュに載せない（別のユーザーの応答が返る）。ベンチは複数ユーザーで検証する。
  - キャッシュを入れて負荷が減ると、ファイルディスクリプタなどの上限に当たる（ISUCON11 予選の講評。`trend` をキャッシュしてリクエストが増え、fail した）。
  - `trend` のように、変化がないと利用者が増えない設計では、鮮度を落とすと加点に響く（講評）。
- **出典**: [nginx: ngx_http_proxy_module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[ISUCON10 本選講評](https://isucon.net/archives/55140539.html)、[ISUCON11 予選講評](https://isucon.net/archives/56044867.html)。URL・パラメータ・ヘッダーをキーに含める。

### アプリ内で応答のバイト列まで作っておく

- **効く条件**: 共通の応答（ダッシュボード）を、JSON にシリアライズする CPU が大きい。
- **やり方**: 一定時間内で共有する値を、シリアライズ後のバイト列まで作ってキャッシュし、そのまま書き出す。ISUCON10 本選の hidekiy は、Audience Dashboard を一定時間共有し、シリアライズ後の結果まで共通処理にした。ETag を付けて 304 を返すこともできる（ISUCON10 本選の解答例は、リーダーボードから ETag を生成した）。
- **確かめ方**: 該当エンドポイントのアプリの CPU（pprof）。応答のバイト列が元の実装と一致するか（互換テスト）。
- **危険**: 上の `proxy_cache` と同じ鮮度の問題。ユーザーごとに変わる部分（本人向け・他人向けの値）を分ける（共通計算とユーザー固有の調整を分けた。質問一覧を共通取得後にユーザーで絞る案は、閲覧範囲の不整合で戻した）。
- **出典**: [ISUCON10 本選講評](https://isucon.net/archives/55140539.html)。実例: [hidekiy](https://blog.hidekiy.com/2020/10/isucon10-8-isucon.html)。

### ブラウザ・ベンチ側にキャッシュさせる（`Cache-Control`・ETag・304）

- **効く条件**: 静的ファイル、作成後に変わらないリソース（アイコン、画像）への GET が多い。
- **やり方**: `Cache-Control: max-age=N` で再利用させる。`ETag`／`If-None-Match` で再検証して 304 を返す。ISUCON11 予選の講評は、静的ファイル・アイコンへの 304 と、`max-age` でリクエスト自体を減らすことを挙げている。ISUCON13 の講評は、アイコンのハッシュを DB の列に持って条件付き GET に対応する形を示している（アイコンの更新から 2 秒以内に反映する条件を守る）。
  - すべての応答に明示的な `Cache-Control` を付けるほうが安全（無い場合、ヒューリスティックにキャッシュされうる。MDN）。個人化された応答は `private`。`no-cache` は「保存しない」ではなく「再利用前に再検証する」。
- **確かめ方**: alp で 304 の割合、リクエスト数の減り。ベンチがキャッシュを使うかは、ベンチの仕様に依存する（使わないなら効かない）。
- **危険**: ベンチが更新の反映を厳しく見る場合、長い `max-age` で古い画像を返す。**静的ファイル・フロントエンドの成果物の内容を変えてはいけない**（ヘッダーを足すだけで内容は変えない。マニュアルで確かめる）。
- **出典**: [MDN: HTTP caching](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Caching)、[ISUCON11 予選講評](https://isucon.net/archives/56044867.html)、[ISUCON13 講評](https://isucon.net/archives/58001272.html)。

## Redis・memcached・Valkey

### いつ外部のキャッシュを使うか

- **効く条件**: アプリが複数のプロセス・台に分かれていて、プロセス内キャッシュでは無効化が届かない。セッション・ジョブのキュー・Pub/Sub を共有したい。
- **やり方**:
  - プロセス間で値を共有する用途（ISUCON7 予選のセッションを Redis へ移して複数アプリで共有した例。ISUCON10 本選の解答例のジョブキュー）。
  - 往復と直列化のコストを含めて選ぶ。ISUCON12 予選で das08 は、プレイヤー情報を Redis に移すと悪化して撤回し、インメモリへ方針転換した（TCP と JSON 化の負荷が理由と考えた）。同じ台のプロセス内で足りるなら、Redis より in-process のほうが速い。
  - 複数のキーは 1 往復にまとめる（memcached の `get_multi`・`set_multi`、Redis の pipeline・`MGET`）。ISUCON5 本選で songmu は multi get/set を実装して 1 回のベンチで通した。
  - 参照のキーを、解決済みの値に寄せて往復を省く（Redis のキーを user id 基準から login 基準に変えて ID の解決を省いた）。
  - 運用: Redis の `INFO stats` の `keyspace_hits` と `keyspace_misses` から hit 率（`hits / (hits + misses)`）を出す。`evicted_keys` が多ければ追い出しが効きすぎている。
- **確かめ方**: Redis の呼び出しの往復・時間をトレースで。hit 率。
- **危険**:
  - **永続化**。Redis は既定でスナップショット（RDB）を保存するが、`kill -9`・電源断では直近を失う。AOF の `appendfsync everysec`（既定）なら失うのは最大約 1 秒。RDB と AOF の併用が PostgreSQL に近い安全性の目安、と公式は述べている。memcached のドキュメントは永続化に触れていない（追い出しと期限切れで消える前提）。**再起動後に残すべきデータを Redis・memcached だけに置かない**。
  - **メモリの上限**。Redis の `maxmemory` の既定は 0（無制限）。`maxmemory-policy` が `noeviction` だと、上限に達すると書き込みがエラーになる。キャッシュ用途なら `allkeys-lru` が公式の既定の勧め。複製・永続化を使うなら、バッファ用にメモリを空けて `maxmemory` を決める。
  - **Pub/Sub は at-most-once**。購読者がいない間・切断中のメッセージは失われる。無効化の通知を Pub/Sub だけに頼ると、取りこぼしで古い値が残る。順序・重複・遅延で最新メッセージのメモリ保持を断念した例もある。取りこぼしが許されないなら Redis Streams。
  - `initialize` で Redis の中身も戻す（Redis とテナント DB の削除漏れでデバッグ祭りになった）。
  - Valkey は Redis の派生で、永続化の考え方（RDB・AOF・`appendfsync`）は同じだが、互換の範囲は出典のページに記載がない（出典未確認）。
- **出典**: [Redis: Persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)、[Redis: Key eviction](https://redis.io/docs/latest/develop/reference/eviction/)、[Redis: Pub/Sub](https://redis.io/docs/latest/develop/pubsub/)、[Valkey: Persistence](https://valkey.io/topics/persistence/)、[memcached ドキュメント](https://docs.memcached.org/)、書籍『達人が教えるWebパフォーマンスチューニング』第 7 章「キャッシュの活用」（目次のみ。[技術評論社](https://gihyo.jp/book/2022/978-4-297-12846-3)）。実例: [das08](https://blog.das82.com/post/isucon12q/)、[sorah](https://diary.sorah.jp/2017/10/23/isucon7q)、[sora_h](https://isucon.net/archives/40793620.html)。

## 事前計算

### 計算結果・生成済み応答を事前に作っておく

- **効く条件**: 入力の集合が有限（初期データ）で、同じ計算（署名、HTML の生成、累乗）が繰り返される。CPU が重い計算が上位にある。
- **やり方**:
  - 生成コストの高い HTML を、初期のうちに作って Redis に積む（ISUCON6 予選）。
  - 購入個数ごとの累乗計算の結果を、起動時に使う範囲（`count` 0〜50）だけ作る（ISUCON7 本選）。
- **確かめ方**: 該当の関数が pprof の上位から消えたか。事前計算の結果が元の実装と一致するか（固定の入力で、旧実装と 1,000 件程度突き合わせるテスト）。
- **危険**:
  - **上限を増やすと、起動時間とメモリが破綻する**。k0kubun は事前計算を 50 件から 500 件に増やして起動できず、150 件でも 2GB を使い切った。initialize にも効く。
  - 事前計算の結果をファイルにシリアライズして読み込む案は、復元のバグで戻した。再読込まで試す。
  - 画像を全件事前変換したらメモリ不足になった例。対象はアクセスされやすいものに絞る。
- **出典**: 実例: [momoiroshikibu](https://qiita.com/momoiroshikibu/items/5755ba1cd39bc18102d8)、[k0kubun](https://k0kubun.hatenablog.com/entry/isucon7-final)、[ijin](https://ijin.github.io/blog/2013/11/11/isucon3-final/)。

## 全面インメモリ化（DB を永続化だけにする）

### 全データをメモリに持つ

- **効く条件**: DB の CPU・ロック・往復がボトルネックで、インデックスや非正規化を尽くしても足りない。データ全体がメモリに収まる。書き込みの経路が 1 つのプロセスに集中できる。
- **やり方**: 状態を、アプリのメモリ上のデータ構造（map・ソート済みの配列・グラフ）に持つ。DB は永続化のためだけに使うか、ファイルの追記ログ（seed＋delta）に置き換える。
  - ISUCON14 感想戦: kawaemon は、起動時に全データをメモリにキャッシュし、更新は遅延して一括書き込みした（85 万点級。ベンチの測定限界に近いと本人は述べている）。ponyo877 は `sync.Map` などに移し、クーポンはユーザーごとの slice と先頭位置のキュー、マッチ可能な椅子と待ちのライドは map と mutex、総距離は椅子の位置更新時に累積して持った（864,990 点）。
  - ISUCON12 本選: NaruseJun は優勝（341,258 点）の後の感想戦で、セッションなどのオンメモリ化、DB 書き込みの非同期化、GOMAXPROCS=1 とCPU アフィニティの固定などを行い、ベンチのアクセスではほぼメモリだけで応答できるようにして 691,921 点に達した。
  - **seed＋delta の型**（`template/templates/inmemory-rust/`）: 起動時に seed（`isu-export-seed` が書いた `<table>.jsonl`）から状態を作り、delta を先頭から再生する。書き込みは書き込みロックの中で「検証 → 反映 → delta バッファに積む」。ベンチ中はファイルに書かず、アイドル 1 秒でバッファを追記して fsync する。SIGTERM（`stop`・`reboot`）では即 flush してから終了する。initialize は状態を seed に戻して delta を空にする。クラッシュで途中まで書かれた行は、起動時に捨てて切り詰める。`cargo test` で再生・アトミックな拒否・reset・壊れた末尾行を確かめられる。
  - 古い例として、ISUCON2 本番でデータをメモリに置き、永続化はテキストへの JSON 追記で行った例がある。
- **確かめ方**: **再起動試験を自分で行う**（04 章・15 章）。ベンチを回し、アプリを `systemctl restart`・`reboot` し、ベンチ中に書いたデータが全て読めるか。`kill -9` の直後の挙動も確かめる（失う範囲を知る）。
- **危険**:
  - **追試で失格になる**。ISUCON14 のマニュアルは、負荷走行中に書かれたデータが再起動後に取得できなければ失格、と定めている。kawaemon は本番で「データ保持違反」により失格した（メモリ上にキャッシュする戦略を取っていたが、原因は本人も特定できていない）。ponyo877 の感想戦の実装は、再起動試験の対策が未実装で、再起動でデータを失う（「永続化未実装なら本番用にならない」）。
  - **ロックがボトルネックに変わる**。1 つの `RwLock` に全データを入れると、書き込みが読みを止める（10 章のロックの粒度）。
  - **整合性の検査**。DB の制約（一意・外部キー・トランザクション）が無くなるので、検証・順序（同点の `id` 順）を自分で守る。
  - `fsync` を切る・永続化を捨てる手は、競技のルールで失格になりうる。マニュアルを確かめる。
- **出典**: [ISUCON14 マニュアル（追試）](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、[kawaemon（失格）](https://zenn.dev/kawaemon/articles/f40529e0c447f5)、[kawaemon（感想戦）](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)、[ponyo877](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)、[NaruseJun](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)、`isucon-kit` の `template/templates/inmemory-rust/README.md`。販売確定後の状態をメモリだけに残す構成は採用不可。

### メモリ上の状態を永続化する（seed＋delta、JSON への退避）

- **効く条件**: 全面または部分的にメモリへ持ち、再起動後も残す必要がある。
- **やり方**: 次のどれかを選ぶ。
  1. **DB に書き続ける**（書き込みは同期。読みだけメモリ）。最も安全。書き込みの時間は DB のまま。
  2. **追記ログ（delta）**。書き込みのたびにバッファに積み、アイドルか一定サイズで追記して fsync。seed からの再生で復元（上の型）。
  3. **DB に非同期で書く**（書き込みの遅延。10 章）。永続化の遅れの範囲を決める。
  4. **終了時に JSON・バイナリへ退避**し、起動時に読む（NaruseJun の感想戦は session と one_time_token を再起動時に JSON へ退避した。異常終了時の復旧の試験の記録は無い）。
  - 起動時に、メモリが空のときだけ DB から復元する経路を作る（ISUCON7 本選で mizkei は、再起動後に不整合が出ないよう、部屋の作成・再入場時に加算・購入の状態を MySQL から復元した）。
- **確かめ方**: 再起動試験（上と同じ）。**`SIGTERM` の経路で flush が実行されるか**（systemd の `TimeoutStopSec` を超えると `SIGKILL` される）。
- **危険**: 通常の再起動・OS の reboot では SIGTERM で flush できるが、`kill -9`・電源断・OOM kill では直近を失う。追試は reboot なので通常は SIGTERM。ただし OOM kill が重なると失う。flush の失敗（ディスクフル）、書き込み途中の壊れた行、再生の冪等性。
- **出典**: `template/templates/inmemory-rust/README.md`・`src/store.rs`、[NaruseJun](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)。[mizkei](https://mizkei.hatenablog.com/entry/2017/11/26/184741)、[chiba](https://blog.everqueue.com/chiba/2012/11/04/589/)。

## initialize・暖気・複数プロセス・メモリ

### initialize でキャッシュを作り直し、暖気する

- **効く条件**: キャッシュ・メモリ上の状態・事前計算が 1 つでもある。ベンチは initialize から始まり、複数回走る。
- **やり方**:
  1. **起動時と initialize の両方で、同じ `build_cache` を呼ぶ**。initialize だけで充填すると、再起動後に空になる（ISUCON10 本選でコンテスト情報を initialize 時だけキャッシュに入れていたため、再起動後のブラウザ操作で取得エラーになった。筆者は失格の原因と推定している）。
  2. initialize は、**メモリ・Redis・ファイル・複製先のすべて**を初期状態に戻す（マニュアルが求める）。
  3. 複数のアプリ・台がある場合、initialize を受けた 1 台だけでなく、全台に再読込を伝える（ISUCON9 予選でにがりは 2 台目を初期化する専用エンドポイントを設けた）。initialize を DB のある台に固定する工夫もある。
  4. 空の状態から最初のアクセスで復元する遅延構築か、initialize の時間内に事前構築する。事前構築は制限時間（30 秒・60 秒など）と、メモリの上限に収める（事前計算を増やすと起動できなかった）。
- **確かめ方**: **initialize を 2 回以上続けて叩き、2 回目以降もベンチが通るか**。再起動の直後にベンチを流して、点が極端に下がらないか。
- **危険**: 2 回目のベンチで落ちる（Iceman：キャッシュを消していなかったためアプリが落ちた）。initialize が制限時間を超える。複数プロセスの一部だけ再読込して古い値が残る。
- **出典**: [ISUCON14 マニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、Iceman（[ISUCON14 参加記](https://zenn.dev/iceman/articles/1780deef84eda8)）。[garasubo](https://garasubo.com/hexo/2020/10/04/isucon10-final.html)、[tohutohu](https://to-hutohu.com/2019/09/09/isucon9-qual)。

### 複数プロセス・複数台でのキャッシュの整合

- **効く条件**: アプリが prefork・複数プロセス・複数台で動く。読みと書きで台が別。
- **やり方**:
  - **1 つのプロセス・1 台に寄せる**のが最も簡単。ISUCON12 本選で NaruseJun は、構成を単一アプリに固めた後、マスターの確認・更新を Admin API の使用時だけにした。
  - 寄せられないなら、(1) Redis など共有の置き場に移す（Node Cluster でプロセス内共有に失敗し、初期化時にユーザーを Redis に ID と名前の両方で登録した）、(2) 無効化を書き込み側から全プロセスへ送る（上の節）、(3) groupcache のような分散キャッシュ（motoki317 のスライドが検討対象に挙げている）。
- **確かめ方**: 書いたプロセスと別のプロセスで、書いた直後に読む。全プロセスで同じ initialize 後の状態か。
- **危険**: 時計の混在、無効化の取りこぼし（Pub/Sub の at-most-once）、再起動した 1 台だけが冷えている。
- **出典**: [motoki317](https://speakerdeck.com/motoki317/effective-generic-cache-in-golang)、（構成の前提） の型 7。[agatan](https://www.wantedly.com/users/17993775/post_articles/81190)。

### メモリの見積もり

- **効く条件**: 全面・大規模なキャッシュを入れる。メモリに制限がある回（ISUCON10 本選は 1GB、ISUCON9 本選も搭載 1GB）。DB と同居している。
- **やり方**:
  - 件数 × 1 件のサイズ × 言語の表現のオーバーヘッドで見積もり、**小さな件数で実測して外挿する**（Go は `runtime.MemStats`・heap プロファイル、Rust は `size_of` と実測、OS は RSS。出典未確認。やり方は 09 章）。
  - 上限を決める（`moka` の `max_capacity`、Redis の `maxmemory`）。
  - DB のバッファ・アプリの各プロセス・nginx・OS のキャッシュの合計で、**OOM kill に当たらない**ようにする。
- **確かめ方**: ベンチ中の `free -m`・`ps` の RSS・`dmesg` の OOM の記録。
- **危険**: ISUCON10 本選の講評は、1GB のメモリ制限がオンメモリの戦術を有利にしすぎないための制限で、無理をするとサーバーが応答しなくなった、と述べている。OOM kill で DB が落ちると、データを失い、その走行は 0 点になる。
- **出典**: [ISUCON10 本選講評](https://isucon.net/archives/55140539.html)、[ISUCON9 本選講評](https://isucon.net/archives/53916974.html)。

## 整合性の検査（書いた直後の読み）に落ちない条件

### 入れる前に、落ちる条件を潰す

- **効く条件**: キャッシュ・インメモリ化を入れる。ベンチの整合性チェックで FAIL したことがある。
- **やり方**: 次のチェックリストで、入れる前に確かめる。
  1. **更新の経路を全部列挙**した（管理 API、削除、退会、一括入稿、initialize、バッチ・ジョブ）。
  2. **更新の直後の読み**に、新しい値が返る。キャッシュが温かい状態と冷たい状態の両方で。
  3. **順序・同点・件数**が元の実装と同じ。ISUCON10 予選で methane は、検索をオンメモリ化したとき `ORDER BY popularity DESC, id ASC` の `id` を落とし、直した後も間欠的に不一致が残った。件数だけをメモリ化しても点はあまり上がらなかった。
  4. **一覧に欠落が無い**。キーワードの追加・削除の反映漏れで FAIL した例。
  5. **閲覧範囲・権限**が人によって違う一覧を、共通のキャッシュにしていない（質問一覧を共通取得後にユーザーで絞る案が、閲覧範囲の不整合で戻った）。
  6. 「確定後は変わらない」「更新されない」と思ったものが、本当にそうか（どれも整合性の検査に失敗した）。
  7. **鮮度の猶予**が TTL より長い。ISUCON14 は座標の反映に 3 秒、ライドの通知に 30 秒の猶予を許す（マニュアル）が、`nearby-chairs` の割り当て可否は即時反映が要り、応答全体を 3 秒キャッシュすることはできない（講評）。
  8. **initialize を 2 回続けて**、**再起動の直後に**、**ベンチを 3 回続けて**通る。
  9. 不正データ（ベンチが不正と判定するデータ）を返していない（安い椅子の一覧をメモリのスライスに保持して INSERT・UPDATE のたびに取り直したが、ベンチでは不正データとなり 0 点。原因は記事に無い）。
- **確かめ方**: 更新→直後の読みのテストを書く（critical を再現するテストは skill `critical-tests`）。ベンチで FAIL したら、ログを間引かずに保存する。
- **危険**: 1 回通っても次の走行で落ちる（ベンチは乱択で検証し、スコアが上がるほど失敗しやすくなる。ISUCON8 予選の講評）。キャッシュを元に戻した後も、他の変更が残っていないか。
- **出典**: [ISUCON8 予選講評](https://isucon.net/archives/52520045.html)、[ISUCON14 講評](https://isucon.net/archives/58869617.html)、[ISUCON14 マニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)。

## よくある失敗

| 失敗 | 防ぎ方 |
| --- | --- |
| 同じ参照が繰り返されていないのにキャッシュを入れる／キャッシュ用の追加 SELECT で遅くなる | 入れる前に `calls` と同じ引数の割合を数える。入れた後に SQL の呼び出し数が減ったか見る |
| **読むたびに DB で世代番号・権限を確かめる**作りにして、往復が減らず点が下がる | 書き込み側から無効化を送る。応答の前に届き終わっていることを確かめる |
| TTL だけに頼り、DB が詰まると鮮度の条件を満たさない（ISUCON10 本選） | 書き込み時の無効化＋同時更新の抑制（singleflight・`proxy_cache_lock`） |
| singleflight で、更新前に始まった取得の古い結果を更新後の読みが受け取る | 更新時に `Forget`。即時反映が要るなら厳密な合流（motoki317 の strict coalescing） |
| 更新の経路を数えそびれ、一覧の欠落・古い値で FAIL | 更新する SQL を grep で全部数え、経路ごとに無効化を入れる |
| initialize でキャッシュを消さず、2 回目のベンチで落ちる／initialize 時だけ充填して再起動後に空になる | 起動時と initialize で同じ構築関数。initialize を 2 回続けて試す |
| キャッシュした共有データを呼び出し側が書き換える | 値は不変。加工するならコピー |
| メモリだけに持ったデータが再起動で消えて失格（データ保持違反） | seed＋delta・DB への書き込み・終了時の flush。再起動試験を自分で行う |
| 全面インメモリ化の着手が遅く、凍結前に整合性の検査が間に合わない | 全面化は最初に決める。他と絡まない部分から段階的に（検索・通知・セッション） |
| 事前計算の上限を増やして起動できない・メモリを使い切る | 上限は起動時間とメモリを測って決める |
| Redis・memcached にだけデータを置き、再起動で消える／Pub/Sub の取りこぼし | 残すべきデータは DB かファイル。Pub/Sub は at-most-once |
| キャッシュを入れて負荷が減り、別の上限（fd・接続）に当たる／大きな変更を 1 回に混ぜて帰属できない | 1 つずつ反映。上限を先に直す（02・03 章） |

## 出典一覧

### ISUCON 公式の講評・マニュアル

- [ISUCON14 問題の解説と講評](https://isucon.net/archives/58869617.html)、[ISUCON14 マニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)
- [ISUCON13 問題の解説と講評](https://isucon.net/archives/58001272.html)
- [ISUCON12 予選問題の解説と講評](https://isucon.net/archives/56850281.html)、[ISUCON12 本選問題の解説と講評](https://isucon.net/archives/56959385.html)
- [ISUCON11 予選問題の解説と講評](https://isucon.net/archives/56044867.html)、[ISUCON11 本選問題の解説と講評](https://isucon.net/archives/56163308.html)
- [ISUCON10 予選問題の解説と講評](https://isucon.net/archives/55025156.html)、[ISUCON10 本選問題の解説と講評](https://isucon.net/archives/55140539.html)
- [ISUCON9 本選問題の解説と講評](https://isucon.net/archives/53916974.html)
- [ISUCON8 予選問題の解説と講評](https://isucon.net/archives/52520045.html)

### 参加記・出題者・常連の記事

- ISUCON14: [Iceman](https://zenn.dev/iceman/articles/1780deef84eda8)、[kawaemon（失格）](https://zenn.dev/kawaemon/articles/f40529e0c447f5)、[kawaemon（感想戦、85 万点）](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)、[ponyo877（感想戦、864,990 点）](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)
- [NaruseJun（ISUCON12 本選優勝、感想戦 691,921 点）](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)
- [motoki317: Effective generic cache in Golang（ISUCON LT 2023）](https://speakerdeck.com/motoki317/effective-generic-cache-in-golang)
- [catatsuy: キャッシュライブラリ](https://zenn.dev/catatsuy/articles/25f68f52c9e487)
- 書籍『達人が教えるWebパフォーマンスチューニング ISUCONから学ぶ高速化の実践』（目次のみ参照。[技術評論社](https://gihyo.jp/book/2022/978-4-297-12846-3)、第 7 章がキャッシュ）
- 手元の資料: `template/templates/inmemory-rust/`

### 公式ドキュメント

- Go: [`sync`（Map・RWMutex）](https://pkg.go.dev/sync)、[`singleflight`](https://pkg.go.dev/golang.org/x/sync/singleflight)
- Rust: [tokio: Shared state](https://tokio.rs/tokio/tutorial/shared-state)、[DashMap](https://docs.rs/dashmap/latest/dashmap/struct.DashMap.html)、[moka](https://docs.rs/moka/latest/moka/)、[moka::sync::Cache](https://docs.rs/moka/latest/moka/sync/struct.Cache.html)
- [nginx: ngx_http_proxy_module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
- [MDN: HTTP caching](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Caching)
- [Redis: Persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)、[Redis: Key eviction](https://redis.io/docs/latest/develop/reference/eviction/)、[Redis: Pub/Sub](https://redis.io/docs/latest/develop/pubsub/)、[Valkey: Persistence](https://valkey.io/topics/persistence/)、[memcached](https://docs.memcached.org/)

## 出典未確認の項目

次は、出典のページに直接の記載がなく、整理者が補った記述。使う前に確かめる。

- Valkey と Redis の互換の範囲（Valkey の永続化の説明は Redis と同じ構成だが、互換性の記述はページに無い）。
- メモリ使用量の見積もりの方法（件数 × サイズ × オーバーヘッド、`runtime.MemStats`・`size_of`・RSS での実測）。各言語のツールの使い方は 09 章で確かめる。
- 「同じ台のプロセス内キャッシュは Redis より速い」という一般則（1 件の実例が根拠）。
- Go の `atomic.Pointer` での差し替えのコード例（言語の標準機能で、競技の実例ではない）。
- `Cache-Control` を付けるだけでヘッダーは静的ファイルの「内容」の変更に当たらない、という解釈（当日のマニュアルで確かめる）。
