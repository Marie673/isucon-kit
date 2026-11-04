# 09 アプリと言語ランタイム

**当日のマニュアル・レギュレーションが優先する。** この章の手は、その回のルールで禁止・失格になることがある。特に注意するのは次の 6 つ。

- パスワードの保存方式・ハッシュの強さを変える。
- 検査・検証を省いて、不正な入力を通す。
- ベンチ中に外部のサーバーへ処理を渡す（開発・計測・テストでの利用は別）。
- 静的ファイルや応答の構造を変える。
- DB を外すなどして、再起動後にデータが残らなくなる。
- ベンチを見分けて、違う応答を返す。

## この章で扱う範囲

- Go と Rust のランタイム設定: GC・スレッド・プール・JSON・アロケータ・ビルド。
- 他の言語（Ruby・Python・Node.js・PHP・Perl）は、プロセスとワーカーの数など要点だけ。
- 言語を問わない、重い CPU 処理の見つけ方と、軽くする・まとめる・事前計算する・配る方法。

扱わないもの。

| 話題 | 章 |
| --- | --- |
| プロセス内キャッシュ・全面インメモリ化 | 08 |
| 非同期化・キュー・ワーカー数の設計・通知 | 10 |
| nginx と上流の keepalive | 03 |
| 台の役割の分け方 | 11 |
| 画像の配信・事前生成 | 12 |
| 外部 API 用の HTTP クライアントの設計 | 13 |
| 言語の置き換えそのものの判断 | 16 |

## 最初に見るもの（当日の 5 分チェックリスト）

アプリの設定は、計測で CPU がアプリに寄っていると分かってから触る。ただし次の 8 つは先に見る。

| 見るもの | コマンド | 異常のしるし |
| --- | --- | --- |
| アプリの CPU の使い方 | `top -H -p <pid>`、`pidstat -u -t 1` | 1 スレッドだけ 100%。他のコアが空いている（ワーカー数・`GOMAXPROCS`・プロセス数を疑う） |
| どの関数が重いか | Go: `go tool pprof -top -cum`。Rust・他: `perf record -F 99 -g -p <pid> -- sleep 30` と `perf report --no-children` | 上位が JSON・ハッシュ・画像・正規表現・割り当て（malloc、GC）のどれか |
| GC の割合 | Go: `GODEBUG=gctrace=1`。Rust: 割り当て関数（`malloc`・`free`）の割合を perf で見る | Go で GC が数 % 以上、`mallocgc` が上位 |
| DB プールの待ち | Go: `db.Stats` の `WaitCount`・`WaitDuration`。Rust: `acquire` を囲んだトレースの待ち時間 | 待ちが多い、p99 が桁違い |
| CPU の命令セット | `grep -o -w -E 'sha_ni\|avx2\|avx512f\|aes' /proc/cpuinfo \| sort \| uniq -c`（台ごと。キットの `tools/cpu-features`） | 台によって `sha_ni` の有無が違う。ハッシュ・署名が上位 |
| ログの出力 | アクセスログ・フレームワークのロガー・SQL ログが標準出力かファイルへ出ているか | ベンチ中に大量に出る（`journald` や `systemd` の CPU も見る） |
| ビルドの種類 | Rust: `Cargo.toml` の `[profile.release]`。Go: `go version`、ビルドコマンド | debug ビルドで動かしている。古い Go（GOMAXPROCS が cgroup を見ない） |
| 再起動後に手作業なしで起動するか | `systemctl is-enabled <アプリのサービス>` | `disabled`。新しいサービスを足したのに enable していない |

perf は競技サーバーでベンチ中に短く採る。重い計測は避ける。誰がどの台に ssh してよいかは、その回のルールとチームの決まりに従う。Go の pprof を本番で採るときは、1 度に 1 つだけ採る。CPU プロファイルにはオーバーヘッドがある（[Go diagnostics](https://go.dev/doc/diagnostics)）。

---

## 共通: 重い CPU 処理

### 重い CPU 処理を見つける

- **効く条件**: アプリの CPU が高く、DB が空いている。または 1 台だけ CPU が張り付いている。
- **やり方**
  1. プロファイルの上位を、処理の名前（署名、JSON、画像のデコード、正規化、ハッシュ、ZIP）に直す。
  4. flat と cum を区別する。flat が大きい関数は自分が重く、cum だけ大きい関数は呼び先が重い（[Go blog: pprof](https://go.dev/blog/pprof)）。
- **確かめ方**: 同じ負荷で、変更の前後に同じ上位関数の割合を比べる。割合が下がっても、スコアの増加の全部をその変更のせいにしない。
- **危険**: 特になし（計測のみ）。perf のオーバーヘッドは小さくないので、ベンチ中は短くする。
- **出典**: [Go blog: Profiling Go Programs](https://go.dev/blog/pprof)、[Go diagnostics](https://go.dev/doc/diagnostics)。実例: （子関数と親関数の時間を取り違えない）、DB が軽くなったら CPU プロファイルへ。

### 重い処理を軽くする 5 つの手（優先順）

- **効く条件**: 重い CPU 処理が特定できた。
- **やり方**: 上から順に検討する。上ほど安全で、効きやすい。

| 順 | 手 | 例 | 危険 |
| --- | --- | --- | --- |
| 1 | **やめる**（結果を使っていない、同じ値を繰り返し計算している） | 保存名のためだけの全量 SHA1 を省く。同じ署名の再計算を省く | 仕様が使う値かを確かめる |

- **確かめ方**: 手ごとに、前後で同じ関数の割合とスコアを比べる。1 回の反映に複数の手を混ぜない（どれが効いたか分からなくなる）。
- **出典**: の型 1・2・10。

### 検査を減らす危険（不正な入力を通さない）

- **効く条件**: 受け付けた画像・JSON・CSV・認証情報の検査が CPU の大きい塊で、仕様の範囲で省けそうなとき。
- **やり方**
  - 省いた検査と元の検査が同じ結果を返すことを、境界の入力（壊れた入力、上限ちょうど、上限超過）の golden で確かめる。
  - 検査済みであることを示す印を別の台へ渡す場合は、偽造できない形（署名つき）にする。
- **確かめ方**: critical のテスト（不正入力の 4xx）、互換テスト（16 章）。変更後に 4xx の件数が変わっていないか、アクセスログで見る。
- **危険**
  - 寸法だけの事前チェックは、壊れた画像の記録（abuse の判定）を変えうる。
  - NG ワードの判定を DB からアプリへ移したら、期待 400 に対して 201 を返す不具合が増えた例がある。
  - 検査を省いて点が上がっても、失格・追試で落ちれば 0 点になる。

### パスワードのハッシュ（bcrypt）

- **効く条件**: ログインやユーザー登録の API の CPU が高い。プロファイルに bcrypt 関数が上位に出る。
- **やり方**
  - **コストの値は、仕様・保存済みのハッシュ・追試で変えられないことが多い。** 変えてよいかはマニュアルで確かめる。変えるなら、保存済みの全ユーザーの照合が通ることを確かめる。
  - 変えられないときの手は、「その API を空いた台へ振る」。ISUCON9 予選で matsuu のチームは、bcrypt が重いので 2・3 台目にアプリを置き、`/login` だけを振り分けた。その前提は、ログイン後のセッションを別の台でも読めること。
  - bcrypt のオブジェクトをキャッシュしても、重いのは比較処理なので効かない（ISUCON9 予選 genya0407）。同じユーザーが繰り返しログインしないなら、キャッシュも当たらない。
  - コストは 1 増えるごとに計算時間が約 2 倍になる。
- **確かめ方**: プロファイルの bcrypt 関数の割合、`/login` のレイテンシの p99。
- **危険**: コストを下げると、パスワード保存の強度が下がる。マニュアルが認めない場合は失格や減点になりうる。ログイン専用の台を作るときは、セッションの共有と再起動後の動作を確かめる。
- **出典**: [matsuu の ISUCON9 予選の記録](https://matsuu.hatenablog.com/entry/2019/09/08/195735)、[genya0407 の記録](https://genya0407.github.io/post/isucon9)、[PHP RFC: bcrypt cost 2023](https://wiki.php.net/rfc/bcrypt_cost_2023)（コストと計算時間の関係）。

### JSON の生成・解釈が重い

- **効く条件**: プロファイルで JSON のエンコード・デコード（`encoding/json`、`serde_json`、`reflect`）が上位。大きな一覧・ダッシュボードの応答。
- **やり方**（効く順）
  1. 整形（インデント）を外す。
  2. 同じ内容の応答は、バイト列まで作って使い回す（共通ダッシュボードの JSON を作って共有）。ユーザー固有の部分だけを足す。
  3. ライブラリを替える（Go は下の「`encoding/json` の代わり」、Rust は serde の項）。
  4. 手書きで JSON を組み立てる。固定フォーマットを前提とした手書きの JSON は、適用範囲が狭い。エスケープを間違えると応答が壊れる。
- **確かめ方**: 同じ応答を作る単体のベンチ。プロファイルの JSON の割合。**プロファイルで JSON が重く見えても、交換が効かないことがある**（ISUCON10 予選 lostfind: DB 取得より JSON エンコードの時間が長いと見て GoJay に替えたが、点もプロファイルも変わらず撤回した）。
- **危険**: 数値の表現（浮動小数、桁数）、キーの順序、HTML のエスケープ、`null` と空配列、日時の形式がライブラリで変わる。ベンチが応答を検査するなら、応答のバイト列を旧実装と突き合わせる。

### ZIP・画像変換などの外部コマンドをやめる

- **効く条件**: プロセスの生成（`fork`・`exec`）が多い。`zip`・`convert`・`identify` を毎回呼んでいる。
- **やり方**: 言語のライブラリで置き換える。書籍『達人が教えるWebパフォーマンスチューニング』の 8-1 は「外部コマンドを実行する代わりにライブラリを使う」を高速化手法として挙げる。ISUCON11 本選の講評は、zip コマンドをライブラリに替え、圧縮せず格納（stored）にする案を示した。
- **確かめ方**: プロセス生成の数（`perf trace -s`、`ps` の短命なプロセス）、プロファイル。
- **危険**: 圧縮形式・ファイル名の文字コード・権限・画像の出力を保つ。ライブラリ化しても速くならない例がある（ISUCON3 本選の `convert`→fastimage_resize）。
- **出典**: [ISUCON11 本選の講評](https://isucon.net/archives/56163308.html)、[書籍の目次（技術評論社）](https://gihyo.jp/book/2022/978-4-297-12846-3)。

### ログ出力を減らす・切る

- **効く条件**: アクセスログ・フレームワークのロガー・SQL ログがベンチ中に大量に出る。`journald`・`systemd-journal` が CPU を使っている。
- **やり方**: 計測が終わってから（終盤に）切る。ISUCON13 優勝の NaruseJun は 17:00〜18:00 に Echo のアクセスログと pprotein のログを切り、400k を超えた。ISUCON11 予選の講評も、systemd-journal が CPU を使っているので、アプリのログ（落とした POST のログを含む）を消すよう勧めた。
- **確かめ方**: 切る前後の CPU（`journald`・アプリ）とスコア。
- **危険**
  - ログを切ると、施策の検証ができなくなる（ISUCON12 予選 daiju は、アクセスログを切った状態で振り分けの動作を確かめられなかった）。切るのは最後のほうにする。
  - ベンチの結果の判定にログが要らないかを、マニュアルで確かめる。
  - ログのローテーションやファイルの肥大でディスクが埋まらないようにする。
- **出典**: [ISUCON11 予選の講評](https://isucon.net/archives/56044867.html)、[NaruseJun の ISUCON13](https://zenn.dev/tohutohu/articles/923bdf5dcd73af)、[daiju の ISUCON12 予選](https://zenn.dev/daiju/articles/165da7a5589434)、書籍の目次 8-2。

### 文字列検索・置換・テンプレート

- **効く条件**: プロファイルで正規表現・文字列の連結・テンプレートの描画が上位。キーワードを大量に持つ置換（ISUCON6 予選の htmlify）。
- **やり方**
  - 正規表現は起動時に 1 度だけコンパイルする（関数の中で毎回コンパイルしない）。
  - 大量のキーワードの置換は Trie に替える。**最長一致の順序**・エスケープ・追加削除の意味を保つ。
  - 改行の分割など、単純な区切りで済む処理から正規表現を外す。
  - テンプレートは起動時にコンパイルして使い回す（Go の `html/template` は下の項）。
  - 同じ一覧を 1 つのビューの中で何度も取らない。
- **確かめ方**: プロファイル。置換の結果を旧実装と突き合わせる（HTML のエスケープ、引用符、重なるキーワード）。
- **危険**: 2 回に分けた置換を 1 回にすると、シングルクォートを含むキーワードで壊れた例がある。
- **出典**: [Go regexp](https://pkg.go.dev/regexp)（コンパイルは 1 度。`Regexp` は複数の goroutine から使える）。実例: 〜。

### 一時オブジェクトの再利用（割り当てを減らす）

- **効く条件**: Go では `mallocgc`・GC がプロファイルの上位。Rust では `malloc`・`free`・`realloc` が上位。
- **確かめ方**: `go tool pprof -alloc_space`（heap プロファイル）や `GODEBUG=gctrace=1` の GC 回数。perf の割り当て関数の割合。
- **危険**: プールに戻したバッファの残留値、同時使用、戻し忘れ。単独の効果が測られていない例が多い。
- **出典**: [Go diagnostics（heap プロファイル）](https://go.dev/doc/diagnostics)、[Go blog: pprof](https://go.dev/blog/pprof)（割り当てを減らして GC の時間を縮めた例）。[NaruseJun の ISUCON12 本選](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)。

### ワーカー数・プロセス数・スレッド数

- **効く条件**: CPU が空いているのにスループットが出ない。1 プロセス・1 スレッドしか動いていない。初期実装が prefork の言語（Ruby・Python・Perl・PHP）。
- **やり方**
  - prefork 型の言語は、既定のワーカー数だと初期スコアが低い。ISUCON12 予選の講評は、30〜50 程度に増やすと改善するとした。
  - Rust の actix-web は、既定が CPU 数で、ワーカーを 1 から増やして点が上がった例がある（ISUCON12 本選 Dice801: 3 ワーカーで 101,647→132,197。予選でも 2・3・4 を比べて 3 を採用）。
  - ワーカーごとに DB 接続プール・キャッシュ・メモリを持つので、**全体の接続数 = ワーカー数 × プールの大きさ**で計算する。
- **確かめ方**: `top -H`、`pidstat -u -t 1`、ベンチ前後のコアごとの使用率、DB の接続数（`pg_stat_activity`・`SHOW PROCESSLIST`）。
- **危険**: 増やしすぎると、DB のメモリ不足、実行待ち、共有ロックの待ちが増える（実例: 並列 2→8 で TCP の再送が増えて点が下がった）。ワーカー数の最適値は CPU 数・DB の位置で変わる。
- **出典**: [ISUCON12 予選の講評](https://isucon.net/archives/56850281.html)、[Dice801 の ISUCON12 本選](https://zenn.dev/dice801/articles/ebc027207257b1)、[daiju の ISUCON12 予選](https://zenn.dev/daiju/articles/165da7a5589434)、[actix-web HttpServer](https://docs.rs/actix-web/latest/actix_web/struct.HttpServer.html)。

### 非同期化は、待ちを測ってから

- **効く条件**: 応答に不要な処理（通知・集計・ログ）が応答時間に入っている。
- **やり方**: 「状態の確定 → 処理 → 通知 → 受信」のうち、どこを待っているかを測ってから、待つ区間だけを外す。**CPU がボトルネックのところを非同期にしても速くならない**（ISUCON2 本番 typester）。DB の処理能力を超える同時実行は増やさない。INSERT を goroutine に投げるだけでは改善せず、revert した例がある（ISUCON11 予選）。詳細は 10 章。
- **確かめ方**: 区間ごとの時間（トレース）。
- **危険**: 応答後に書き込む処理は、エラーの伝播・終了時の書き込み完了・再起動で失われないことを確かめる（失格）。
- **出典**: 詳細は 10 章。

---

## Go

### `GOMAXPROCS`

- **効く条件**: コンテナ・VM の CPU 数と、Go が見る CPU 数が違う。同居するプロセス（nginx・DB）と CPU を取り合う。
- **やり方**
  - 環境変数が未設定なら、論理 CPU 数・CPU affinity・Linux の cgroup の CPU 上限から既定値を決める（[runtime のドキュメント](https://pkg.go.dev/runtime#hdr-Environment_Variables)）。Go 1.25 から、コンテナの CPU 上限が CPU 数より小さいとき、`GOMAXPROCS` の既定がその上限になる。`go.mod` を Go 1.25 以上にすると新しい既定になる（[Go blog](https://go.dev/blog/container-aware-gomaxprocs)）。上限を端数にすると切り上げる。
  - ISUCON の VM は cgroup の上限が無いことが多く、既定（論理 CPU 数）でよい。
  - 同居の nginx と CPU を固定する手がある。ISUCON13 の感想戦で NaruseJun は `GOMAXPROCS=1`・CPU コアの固定・nginx の worker 1 を入れ、「CPU affinity が効いた」と書く（他の変更も同時で、単独の効果ではない）。
- **確かめ方**: `top -H` でスレッドの CPU、ベンチ前後のスコア。
- **危険**: 1 にすると、1 プロセスで使える CPU が 1 コアになる。**ロックを外す根拠にしない**（`GOMAXPROCS=1` でもゴルーチンは割り込まれる）。台の CPU 数を確かめてから値を決める。
- **出典**: [Go runtime の環境変数](https://pkg.go.dev/runtime#hdr-Environment_Variables)、[Go 1.25 container-aware GOMAXPROCS](https://go.dev/blog/container-aware-gomaxprocs)、[NaruseJun の ISUCON13（感想戦）](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)。

### GC（`GOGC`・`GOMEMLIMIT`）

- **効く条件**: pprof で `runtime.mallocgc`・GC のワーカーが上位。`GODEBUG=gctrace=1` で GC の割合が高い。メモリに余裕がある。
- **やり方**
  - 目標のヒープ = 生きているヒープ + (生きているヒープ + GC ルート) × `GOGC` / 100。`GOGC` を 2 倍にすると、ヒープのオーバーヘッドが約 2 倍、GC の CPU が約半分になる。既定は 100（[GC ガイド](https://go.dev/doc/gc-guide)）。
  - `GOGC=200〜400` から試す（定石集の値。出典は定石集）。ISUCON13 の感想戦で NaruseJun は `GOGC` を大きく上げた（約 2000）。
  - `GOMEMLIMIT` は、そのプロセスだけがメモリを使う環境で、コンテナの上限の 5〜10% 手前に置く。ソフトな上限で、GC の CPU は約 50% で頭打ちになる（GC ガイド）。
  - `GOGC=off` と `GOMEMLIMIT` の組み合わせは、同居するプロセス（DB など）とメモリを分け合う環境では避ける（GC ガイド）。
- **確かめ方**: `GODEBUG=gctrace=1`（GC 1 回ごとに 1 行。GC の時間の割合、ヒープの大きさ）、ベンチ中の RSS（`ps -o rss`）、スコア。
- **危険**
  - **メモリを使い切って OOM になる**（DB と同居する台は特に）。ヒープの上限が生きているヒープより小さいと、GC が走り続けて止まったようになる（thrashing。GC ガイドと [golang/go#68346](https://github.com/golang/go/issues/68346)）。
  - ISUCON のツール（isucon-go-tools）の作者は、`GOMEMLIMIT` の自動設定がうまく働かず、素直に `GOGC` と `GOMEMLIMIT` を設定するほうがよいとして、自動の調整を削除した（[PR #91](https://github.com/mazrean/isucon-go-tools/pull/91)）。
  - 定石の値（200〜400）に ISUCON での効果の数字は無い。問題で変わる。
- **出典**: [Go GC ガイド](https://go.dev/doc/gc-guide)、[golang/go#68346](https://github.com/golang/go/issues/68346)、[isucon-go-tools PR #91](https://github.com/mazrean/isucon-go-tools/pull/91)、[Go runtime](https://pkg.go.dev/runtime#hdr-Environment_Variables)（`gctrace`）、[NaruseJun の ISUCON13（感想戦）](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)。

### `encoding/json` の代わり

- **効く条件**: pprof で `encoding/json`・`reflect` が上位で、DB が軽い。大きな応答。

- **やり方**

| 候補 | 要点 |
| --- | --- |
| `goccy/go-json` | import を置き換えるだけで使える。`encoding/json` 互換をうたう。個人プロジェクトで、互換の差は README の比較表で確かめる。ISUCON11 本選の fujiwara は echo の JSONSerializer に差し込んだ。ISUCON12 本選の NaruseJun は 13:10 に go-json に替えた |
| `bytedance/sonic` | AMD64、または ARM64（Go 1.20 以上）。`ConfigDefault` は HTML のエスケープとキーの並べ替えが無い（標準と違う）。`ConfigStd` は標準に合わせる。初回呼び出しが遅い・メモリが増えることがあり、`Pretouch` で緩和する。Go 1.24.0 は非対応 |
| 標準の `encoding/json/v2` | ISUCON13 の NaruseJun は 12:11 に「json/v2」に替えた（23,632 点付近）。実験機能の扱いと互換は版で変わる |

- **確かめ方**: 同じ構造体・同じ応答で、差し替えの前後を単体のベンチにする。プロファイルの JSON の割合。**差し替えても効かない例がある**（GoJay。上の JSON の項）。
- **危険**: キーの順序、HTML のエスケープ（`<`・`>`・`&`）、`null`・空配列、数値の表現が変わる。ベンチが応答を検査するなら、旧実装と応答を突き合わせる。sonic の既定はエスケープが標準と違い、RFC 8259 に沿わないと README は書く。
- **出典**: [goccy/go-json](https://github.com/goccy/go-json)、[bytedance/sonic](https://github.com/bytedance/sonic)、[NaruseJun の ISUCON13](https://zenn.dev/tohutohu/articles/923bdf5dcd73af)、[NaruseJun の ISUCON12 本選](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)。

### テンプレートの事前コンパイル（`html/template`）

- **効く条件**: ビューの描画（`Execute`）より、テンプレートの読み込み・パースが毎回走っている。
- **やり方**: 起動時に `template.Must(template.ParseFiles(...))` で 1 度だけパースして、変数に持つ。`Execute` は複数の goroutine から同時に呼んでよい（同じ `Writer` を共有しなければ。出力が混ざる）。最初の `Execute` の後でテンプレートを書き換えない（エスケープの処理が最初の実行で確定する）。
- **確かめ方**: プロファイルで `Parse` が消える。
- **危険**: テンプレートのファイルを書き換えたとき、再読み込みの時刻を決める。
- **出典**: [Go html/template](https://pkg.go.dev/html/template)。実例: （EJS で同じ手）。

### HTTP サーバー（net/http・echo・chi・fiber）

- **効く条件**: プロファイルで、ルーティング・ミドルウェア・ロガー・ヘッダーの処理が上位。nginx の `upstream` との間の接続が多い。
- **やり方**
  - ロガーのミドルウェア（echo の `middleware.Logger`、chi の `middleware.Logger`）は、計測が終わったら外す（上の「ログ出力」）。
  - nginx との間は upstream の keepalive と、必要なら Unix ドメインソケットにする（03 章）。
  - ISUCON13 の感想戦で NaruseJun は、フレームワークを fiber に替え、nginx との間を Unix ドメインソケットにした（効果は他の変更と合算）。
  - プロファイルの endpoint を `net/http/pprof` で出す場合は、**ベンチを見分ける挙動や外部に公開しない**ポートにする（バインドは `127.0.0.1`）。
- **確かめ方**: プロファイルの上位、ベンチ前後。
- **危険**: フレームワークの置き換えは、Cookie・ヘッダー・エラーの形（ステータス・本文）・ルーティングの細部が変わりうる。置き換える前に応答を突き合わせる。
- **出典**: [NaruseJun の ISUCON13（感想戦）](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)、[Go diagnostics](https://go.dev/doc/diagnostics)。

### `http.Client` の Transport（外部 API・内部呼び出し）

- **効く条件**: 同じホストへ大量のリクエストを送る。`ss -s` の TIME_WAIT が増える。外部 API のレイテンシが接続の確立で伸びる。
- **やり方**
  - `http.Client` を毎回作らず、1 つを使い回す。
  - `DefaultTransport` は `MaxIdleConns: 100`・`IdleConnTimeout: 90s` で、**`MaxIdleConnsPerHost` の既定は 2**（`DefaultMaxIdleConnsPerHost`）。同じホストへ同時に 3 本以上流すと、超えた分は使い終わると閉じられ、毎回接続を張り直す。
    ```go
    tr := http.DefaultTransport.(*http.Transport).Clone
    tr.MaxIdleConnsPerHost = 100   // 値は同時に流す本数に合わせる
    client := &http.Client{Transport: tr, Timeout: 10 * time.Second}
    ```
  - `MaxConnsPerHost` を設定すると、超えたぶんはダイヤルで待つ（ブロックする）。
  - **レスポンスの本文は最後まで読んで閉じる。** 読み切らずに閉じると接続が再利用されないことがある（`net/http` のドキュメント）。
- **確かめ方**: `ss -tan | grep <相手のポート> | awk '{print $1}' | sort | uniq -c`、ベンチ前後の接続の数。
- **危険**: 外部 API の許容接続数を超えない。**ベンチ中に外部のサーバーへ処理を委譲するのは禁止**。台間の通信は相手のプライベート IP を宛先にする。
- **出典**: [net/http transport.go（`DefaultTransport`・`DefaultMaxIdleConnsPerHost`）](https://github.com/golang/go/blob/master/src/net/http/transport.go)、[pkg.go.dev/net/http](https://pkg.go.dev/net/http)。実例: （ISUCON9 予選 matsuu。`MaxIdleConnsPerHost` を増やして約 19000）。

### `database/sql` のプール

- **効く条件**: DB の接続の確立が多い（`SHOW PROCESSLIST` で接続の入れ替わりが多い）、`db.Stats` の `WaitCount` が増える、アプリが接続の取得待ちで遅い。
- **やり方**
  - `SetMaxOpenConns(n)` と `SetMaxIdleConns(n)` を同じ値にする（定石集）。`SetConnMaxIdleTime` で余った接続を閉じる。
    ```go
    db.SetMaxOpenConns(30)
    db.SetMaxIdleConns(30)
    db.SetConnMaxIdleTime(5 * time.Minute)
    ```
  - MySQL は `interpolateParams=true` で PREPARE / CLOSE STMT の往復を消す。ISUCON14 の LayerX（23,894 点）、ISUCON12 本選の NaruseJun（11:11）、ISUCON13（NaruseJun、10:00〜11:00）が入れた。スロークエリに `ADMIN PREPARE` が大量に出ていたら効く（定石集）。
  - **`n` は全アプリの本数で割った合計が DB の `max_connections` に収まる値にする。** 値は台のメモリと DB の状態で決める（04・05 章）。
- **確かめ方**: `db.Stats`、DB 側の接続数、`db.acquire` を囲んだトレースの待ち時間。
- **危険**
  - 上限を付けると、接続の取得待ちがロックのように働き、デッドロックしうる（Go 公式）。トランザクションの中で別の接続を取らない。
  - `interpolateParams=true` は、値をクライアント側で埋める。SQL インジェクションに注意し、文字コードの設定（`charset`）を確かめる。

### 正規表現・`fmt`・`reflect` の重さ

- **効く条件**: pprof の上位に `regexp`・`fmt.Sprintf`・`reflect` が出る。
- **やり方**
  - `regexp` は Go では線形時間を保証する実装で、速さが目的ではない。ホットパスでは、単純な文字列の関数（`strings.Index`・`strings.Cut`・`bytes.*`）や、手書きのパーサーに替える。コンパイルは 1 度（上の項）。
  - `fmt.Sprintf` を連ねた文字列の組み立ては、`strings.Builder` や `strconv` に替える（出典未確認。プロファイルで確かめる）。
  - `reflect` は多くが `encoding/json` や DB のスキャン（`sqlx`）の中にある。呼び出し元を pprof の `-peek` で見て、そこを替える。
- **確かめ方**: `go tool pprof -peek 'regexp|reflect'`、前後の割合。
- **危険**: 正規表現を手書きに替えるとき、Unicode・境界の扱いが変わる。互換テストで確かめる。
- **出典**: [Go regexp](https://pkg.go.dev/regexp)。`fmt`・`strings.Builder` の項は出典未確認。

### pprof の使い方と、待ちを含むプロファイル

- **効く条件**: アプリの CPU が高いのに原因が分からない。逆に CPU が低いのに遅い（ロック待ち・DB 待ち）。
- **やり方**
  - CPU: `net/http/pprof` を入れ、`go tool pprof -http=:8000 'http://127.0.0.1:6060/debug/pprof/profile?seconds=30'`。`top`・`list 関数名`・`web` を使う。flat は自分、cum は呼び先を含む。
  - ブロック・ミューテックスは既定で無効。`runtime.SetBlockProfileRate(1)`、`runtime.SetMutexProfileFraction(1)` で有効にする（Go diagnostics）。ロックの競合で CPU が空くときに見る。
  - **CPU 使用中だけでなく待ちも含む**プロファイル（fgprof）を標準にした例がある（ISUCON14 fukkatsuso）。
  - 走行ごとにプロファイルを保存して、変更の前後を比べる。
- **確かめ方**: 前後の差。1 度に 1 つのプロファイルだけ採る。
- **危険**: ブロック・ミューテックスのプロファイルを常時 `1` で入れたままにすると、オーバーヘッドが出る（計測が終わったら外す。出典未確認）。ベンチ中の採取は短くする。pprof のポートは外に公開しない。
- **出典**: [Go diagnostics](https://go.dev/doc/diagnostics)、[Go blog: pprof](https://go.dev/blog/pprof)、[mazrean/isucon-go-tools](https://github.com/mazrean/isucon-go-tools)（ISUCON 用の計測ツール）。

### Go のビルド（PGO・`GOAMD64`・バージョン）

- **効く条件**: CPU の計測が済んで、コードの変更で下がる余地が小さくなった終盤。
- **やり方**
  - **PGO**: 負荷中の CPU プロファイル（30 秒）を `default.pgo` として main パッケージのディレクトリに置くと、`go build` が自動で使う（Go 1.21 以降）。公式の見込みは 2〜14%（Go 1.22 時点）。複数の台のプロファイルは `go tool pprof -proto a b > merged.pprof` で統合する。ビルド時間が増える。ISUCON13 の NaruseJun は、PGO とログ停止で約 64k に上がり、17:00 台にもう一度採り直した。
  - **`GOAMD64`**: 既定は v1。v3 は AVX2・BMI2 などを前提とし、**対応しない CPU では起動に失敗する**。ビルド機と本番の CPU を揃えるか、v1 のままにする（`/proc/cpuinfo` を台ごとに見る。キットの `tools/cpu-features`）。
  - 古い Go は新しい標準ライブラリの改善（`GOMAXPROCS` の cgroup 対応など）を受けられない。上げられるなら、ビルドの手元で新しい版にする（本番の Go のバージョンはマニュアルで確かめる）。
- **確かめ方**: PGO の前後で、同じ負荷のベンチを交互に 2 本ずつ。
- **危険**: プロファイルは「代表的な走行」から採る。ソースを大きく変えるとプロファイルが合わなくなる（関数名と行番号で対応）。他の変更（ログ停止など）と同時に入れると、PGO 単独の効果が分からない。
- **出典**: [Go: Profile-guided optimization](https://go.dev/doc/pgo)、[Go wiki: MinimumRequirements](https://go.dev/wiki/MinimumRequirements)、[NaruseJun の ISUCON13](https://zenn.dev/tohutohu/articles/923bdf5dcd73af)。

---

## Rust

### tokio のランタイム（worker・blocking）

- **効く条件**: CPU が空いているのに遅い。1 コアだけ 100% で、他が空いている。`spawn_blocking` が溜まってメモリが増える。
- **やり方**
  - マルチスレッドのランタイムの `worker_threads` の既定は `available_parallelism`（CPU 数）。環境変数 `TOKIO_WORKER_THREADS` でも変えられる。`#[tokio::main]` は `rt-multi-thread` が要る。
  - `max_blocking_threads` の既定は 512。キューには背圧が無く、無制限に溜まりうる（[tokio Builder](https://docs.rs/tokio/latest/tokio/runtime/struct.Builder.html)）。重い処理は、**自分で同時数を絞る**（`Semaphore`）。
  - `current_thread` のランタイムは 1 スレッドしか使わない。ISUCON の台で 1 コアに寄せたいとき以外は使わない。
- **確かめ方**: `top -H` で worker のスレッドの CPU。`spawn_blocking` の待ちの長さをトレースで見る。
- **危険**: worker を CPU 数より増やしても、CPU が増えるわけではない。`block_in_place` は `current_thread` のランタイムで panic する。
- **出典**: [tokio Builder](https://docs.rs/tokio/latest/tokio/runtime/struct.Builder.html)、[tokio crate のドキュメント](https://docs.rs/tokio/latest/tokio/index.html)、[block_in_place](https://docs.rs/tokio/latest/tokio/task/fn.block_in_place.html)。

### CPU を食う処理を async の worker から外す

- **効く条件**: 画像のデコード・ハッシュ・署名・圧縮・大きな JSON の生成が、async のハンドラの中で直接走っている。他のリクエストの処理が遅れる（p99 が伸びる）。
- **やり方**
  - `tokio::task::spawn_blocking` で別スレッドへ出す（`.await` で結果を待つ）。CPU 専用のスレッドプールが欲しいときは rayon、または CPU 処理専用のランタイムを別に作る（tokio の公式の案内）。
  - `block_in_place` は、同じタスクの他のフューチャーを止める（`join!` の他の枝など）。同じタスクの他の処理を進めたいなら `spawn_blocking`。
  - **`spawn_blocking` は、await を捨ててもキャンセルされない。** 枠（permit）は、await が破棄される時ではなく、実際の処理が終わった時に返す。
- **確かめ方**: ハンドラの p99、`jpeg.decode_wait`（枠の空き待ち）と `decode_run` の比。待ちが桁違いなら CPU が飽和していて、枠を増やしても効かない。
- **危険**
  - `spawn_blocking` の中でさらに `spawn_blocking` を待つと、枠が 1 つのときデッドロックする。
  - ブロッキングのキューは無制限に溜まる。CPU が足りないときは、枠を増やしても逆効果。
  - CPU がボトルネックなら、非同期化・スレッドの分離だけでは速くならない。軽くするほうが先。
- **出典**: [tokio crate（CPU-bound tasks and blocking code）](https://docs.rs/tokio/latest/tokio/index.html)、[block_in_place](https://docs.rs/tokio/latest/tokio/task/fn.block_in_place.html)。

### ハンドラのキャンセルとトランザクション

- **効く条件**: Rust（axum）へ置き換えた後、初期化（`TRUNCATE`）が止まる、`idle in transaction` が増える、整合性の失格。
- **やり方**: axum はクライアントが切れると、ハンドラの future を途中で捨てる。トランザクションの途中で捨てられると、開いたままのトランザクションが接続ごとプールへ戻る。**トランザクションを持つ処理は `tokio::spawn` で最後まで走らせる**。drop のときにロールバックされることをテストで確かめる。PostgreSQL では `idle_in_transaction_session_timeout` で残った接続を回収する手もある。
- **確かめ方**: `pg_stat_activity` の `idle in transaction`、メタデータロックの待ち（MySQL は `SHOW PROCESSLIST`）。
- **出典**: の R-4、（`idle_in_transaction_session_timeout`）。axum の公式ドキュメントでは確認できなかった（出典未確認。実装を読んで確かめる）。

### 共有状態のロック（同期 Mutex・DashMap）

- **効く条件**: メモリ上の表やカウンターを `tokio::sync::Mutex` で守っている。ロックが短い。
- **やり方**
  - `.await` をまたがない短い排他は、`std::sync::Mutex`（または `parking_lot`）にする。tokio のドキュメントは、標準の Mutex を使うのがよく、`.await` をまたぐ必要があるときだけ tokio の Mutex を使うとする。tokio の Mutex は標準のものより高い。
  - 全体を 1 つの `RwLock<HashMap>` で守っているなら、`dashmap` でエントリ単位の排他にする。
  - **標準の Mutex を `.await` の間、握らない。**
- **確かめ方**: perf でロックの関数（`futex`・`parking_lot`）の割合。ベンチ前後。
- **危険**: 複数のキーを更新する処理の順序・ロックの寿命でデッドロックしうる。ロックの交換だけでは伸びず、保護範囲の見直し（書き込みのトランザクション化）で進んだ例がある（ISUCON12 予選 hidekiy）。
- **出典**: [tokio::sync::Mutex](https://docs.rs/tokio/latest/tokio/sync/struct.Mutex.html)、[kawaemon の ISUCON14 感想戦](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)。

### アロケータ（mimalloc・jemalloc）

- **効く条件**: perf で `malloc`・`free`・`realloc` が上位。割り当てが多い（JSON・文字列・`Vec`）。メモリが断片化する。
- **やり方**
  - 既定は glibc の malloc。`mimalloc`（`mimalloc` crate）か `tikv-jemallocator` に替える。
    ```rust
    #[global_allocator]
    static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;
    ```
  - 公式の Rust Performance Book は、jemalloc（Linux・Mac）と mimalloc を、実行速度の改善・メモリの削減の手として挙げる。結果はプログラムとプラットフォームで変わる。
- **確かめ方**: 同じ負荷で、CPU・RSS・レイテンシを比べる。perf の割り当て関数の割合。
- **危険**: 効果が出ないことがある。Ruby 2.1 に LD_PRELOAD で jemalloc を入れても、有意な差が出なかった例がある（ISUCON4 予選）。アロケータの差とメモリのリークを混同しない。musl 向けのビルドでの動作は、出典未確認。mimalloc の公式の benchmark は 2021 年のもので、長時間の高負荷のサーバーは測っていないと README が書く。
- **出典**: [Rust Performance Book: Build Configuration](https://nnethercote.github.io/perf-book/build-configuration.html)、[microsoft/mimalloc](https://github.com/microsoft/mimalloc)、[tikv/jemallocator](https://github.com/tikv/jemallocator)。

### release のプロファイル（lto・codegen-units・opt-level・panic）

- **効く条件**: 最終ビルド・本番のビルド。実行速度が CPU ボトルネックのとき。
- **やり方**
  ```toml
  [profile.release]
  opt-level = 3            # 既定。3 が 2 より遅いことも稀にある
  lto = "thin"             # fat は遅いが速くなりうる。ビルド時間と相談
  codegen-units = 1        # 速くなりうるが、ビルドが遅い
  panic = "abort"          # 使えるなら小さく速い。catch_unwind を使うコードでは使えない
  debug = "line-tables-only"   # perf のシンボルとソース行のため
  ```
  - 既定は、`opt-level = 3`、`lto = false`（crate の中の thin LTO のみ）、`codegen-units = 16`、`panic = "unwind"`、`debug = false`（[Cargo の profile](https://doc.rust-lang.org/cargo/reference/profiles.html)）。
  - LTO は実行速度を 10〜20% 以上上げうる（Rust Performance Book）。`codegen-units = 1` と `panic = "abort"` も速度を上げうる。数字は公式にも無く、問題で変わる。
- **確かめ方**: 同じ commit で、プロファイルを変えたビルドを交互に 2 本ずつ流す。ビルド時間も記録する。
- **危険**
  - **`panic = "abort"` にすると、`tower_http::catch_panic` など、panic を捕まえて 500 を返す層が働かなくなり、プロセスごと落ちる。** systemd の `Restart=` とセットで考える。出典未確認（`catch_panic` との関係は公式の記述を確認していない）。
  - fat LTO と `codegen-units = 1` はビルドが遅く、反映のたびに時間を食う。練習では LTO 付きのビルドが 1 回約 95 秒で、全員が共有 VM で順番待ちになった（`rust-replace`）。開発・e2e 用と本番用でプロファイルを分ける手がある。
- **出典**: [Cargo Book: Profiles](https://doc.rust-lang.org/cargo/reference/profiles.html)、[rustc: Codegen options](https://doc.rust-lang.org/rustc/codegen-options/index.html)、[Rust Performance Book](https://nnethercote.github.io/perf-book/build-configuration.html)。

### `target-cpu=native` の危険と、命令セットの違い

- **効く条件**: ベクトル化が効くループ（SIMD）が重く、本番の CPU が分かっている。
- **やり方**: `RUSTFLAGS="-C target-cpu=native"` は、**ビルド機の CPU に合わせて**命令を出す。ビルド機と本番が違うと、本番で未対応の命令を実行して落ちる（illegal instruction、SIGILL）。手元のビルドは Apple Silicon（ARM）で、本番は x86 なので、`native` は本番向けにならない。使うなら、本番と同じ系統の x86 の機械でビルドするか、`-C target-cpu=x86-64-v3` のように本番の全台が対応している水準を指定する。台ごとに `/proc/cpuinfo` を確かめる（キットの `tools/cpu-features`）。
- **確かめ方**: `rustc --print target-cpus`、`rustc -C target-cpu=native --print cfg`。4 台全部で、バイナリを起動できること。
- **危険**
  - `target-feature` の指定は未定義の動作になりうると rustc のドキュメントも書く。
  - 手元（ARM）で測った SHA・SIMD の結果を、そのまま本番に当てはめない。
  - `-C target-cpu` を使わず、実行時の判定（次の項）で SIMD を選べば、混在に強い。
- **出典**: [rustc: Codegen options（target-cpu・target-feature）](https://doc.rust-lang.org/rustc/codegen-options/index.html)、[Rust Performance Book](https://nnethercote.github.io/perf-book/build-configuration.html)（native は古い・違うプロセッサで動かないことがある）、[Go wiki: MinimumRequirements（GOAMD64。同種の注意）](https://go.dev/wiki/MinimumRequirements)。`isucon-kit/tools/cpu-features/README.md`。SIGILL の実例は手元の記録に無い（出典未確認）。

### SIMD の実行時判定（`is_x86_feature_detected!`）

- **効く条件**: 台によって CPU の機能が違う。SHA・AES・AVX2・AVX-512 で速さが変わる処理が重い。
- **やり方**: 実行時に CPU の機能を調べ、対応する実装へ分岐する。`#[target_feature(enable = "avx2")]` を付けた `unsafe fn` を、判定の後だけ呼ぶ。
  ```rust
  #[cfg(target_arch = "x86_64")]
  fn sign(inputs: &[Input]) -> Vec<Out> {
      if is_x86_feature_detected!("sha") { return sign_sha_ni(inputs); }
      if is_x86_feature_detected!("avx512f") { return unsafe { sign_avx512(inputs) }; }
      if is_x86_feature_detected!("avx2") { return unsafe { sign_avx2(inputs) }; }
      sign_scalar(inputs)
  }
  ```
- **確かめ方**: 台ごとに、どの分岐が選ばれたかをログに 1 度出す。**本番で実際に動いたことを、perf のシンボルで確かめる。**
- **危険**: 判定を忘れて `unsafe fn` を直接呼ぶと未定義の動作。マクロの名前は、`#[target_feature]` と同じ小文字の名前。入力が 1 件のときまで束ねて待たない（1 件の遅延が増える）。出力が 1 ビットでも違うと、署名・ハッシュは全滅する（固定値と旧実装との突き合わせのテストを先に作る）。
- **出典**: [std::is_x86_feature_detected](https://doc.rust-lang.org/std/macro.is_x86_feature_detected.html)・2。

### ハッシュ・署名・暗号（ring・aws-lc-rs・マルチバッファ SHA-256）

- **やり方**（上から順に）
  1. **やめる・使い回す**: 鍵の準備を 1 回にする、同じ入力の署名を使い回す、期限を丸めて同じ署名にする。
  5. **配る**: `sha_ni` のある台へ渡す（上の「軽くする 5 つの手」）。
- **確かめ方**: 固定値の単体テスト、旧実装との 1,024 入力の一致。`perf` で `sha256_block_data_order`・`compress256` の割合。台ごとの `sha_ni`。
- **危険**
  - キャッシュの上限で「満杯なら覚えない」「分が変わると全消去」にすると、当たりが悪い。
  - 暗号を弱める（回数を減らす・アルゴリズムを替える）のは、署名の形式・検証結果が変わるので、マニュアルが許す範囲だけ。
  - 手元（Apple Silicon）の SHA は専用命令があり、本番の x86（`sha_ni` なし）と逆に出る。
- **出典**: [briansmith/ring](https://github.com/briansmith/ring)、[aws/aws-lc-rs](https://github.com/aws/aws-lc-rs)、[intel/isa-l_crypto](https://github.com/intel/isa-l_crypto)（マルチバッファのハッシュ）。`sha2` の実行時検出は、一次ソース（sha2 0.10.9）で確かめた。

### sqlx・deadpool などの接続プール

- **効く条件**: DB の接続の取得待ち（`acquire` の p99 が長い）、`too many connections`、DB の CPU・メモリの逼迫。
- **やり方**
  - sqlx の `PoolOptions::new` の既定（ソースの値）は、`max_connections` が 10、`min_connections` が 0、`acquire_timeout` が 30 秒、`idle_timeout` が 10 分、`max_lifetime` が 30 分、`test_before_acquire` が true。
    ```rust
    let pool = PgPoolOptions::new
        .max_connections(30)
        .min_connections(10)
        .acquire_timeout(Duration::from_secs(5))
        .test_before_acquire(false)   // 取り出しのたびの ping をやめる
        .connect(&url).await?;
    ```
  - **プロセス数 × `max_connections` が DB の `max_connections` に収まる**ようにする。読み・書き・通知で枠を分けると、書き込みが読みに押し出されない。
  - 山の同時の要求から、必要な接続数を逆算する。
- **確かめ方**: `acquire` を囲んだトレース、`pg_stat_activity` の件数、DB のメモリ。
- **出典**: [sqlx の `PoolOptions`（ソース）](https://github.com/launchbadge/sqlx/blob/main/sqlx-core/src/pool/options.rs)、[sqlx-core の PoolOptions](https://docs.rs/sqlx-core/latest/sqlx_core/pool/struct.PoolOptions.html)。deadpool は出典未確認（使う場合は公式のドキュメントの既定値を先に読む）。

### serde と JSON（serde_json・sonic-rs）

- **効く条件**: perf で `serde_json`・`core::fmt`・文字列の生成が上位。
- **やり方**
  - 整形を外す（`to_string_pretty` を使わない）。
  - 同じ応答はバイト列で保持し、`Bytes` で返す。
  - `sonic-rs` は `serde_json` に近い API で、SIMD を使う。x86_64 か aarch64 で速く、それ以外は遅いフォールバックになる。README は `-C target-cpu=native` でのビルドを勧める（**この章の `target-cpu=native` の危険に注意**）。自己申告のベンチで、データによっては `serde_json` のほうが速い。
  - 不要なフィールドをデシリアライズしない（`#[serde(skip)]`、`IgnoredAny`）。出典未確認。
- **確かめ方**: 同じ応答の単体ベンチ（`criterion`）。perf の割合。
- **危険**: 数値の表現（浮動小数の丸め）、キーの順序、エスケープが変わる。応答をバイト列で旧実装と突き合わせる。
- **出典**: [cloudwego/sonic-rs](https://github.com/cloudwego/sonic-rs)。

### axum・actix-web の設定と、フレームワークを薄くする

- **効く条件**: プロファイルで、ルーティング・middleware・ヘッダー・ログの処理が上位。1 リクエストあたりの CPU を下げたい終盤。
- **やり方**
  - actix-web の `HttpServer` の既定: `workers` は `available_parallelism`、`max_connections` は 1 ワーカーあたり 25,000、`backlog` は 1,024、`keep_alive` は 5 秒、`client_request_timeout` は 5 秒。nginx の背後で動かすなら、`keep_alive` と `backlog` を nginx の設定と合わせる。
  - ミドルウェアの層（トレース・ログ・CORS・パニックの捕捉）は、ホットなパスで本当に要るものだけにする。
  - 終盤の手として、`axum`/`tower` を外して `hyper` と `http` で直接書き、JSON のシリアライザも自作した例がある（ISUCON14 感想戦 kawaemon）。ただし、他の大きな変更（DB を外す）とセットで、単独の効果は分からない。
  - Sinatra を外して Rack にしたら 34,265 点になった、という昔の例もある（ISUCON4）。**使っているフレームワークの機能（Cookie・認証・エラーの形）を保つ。**
- **確かめ方**: perf の middleware 関数の割合、ベンチ前後。
- **危険**: 自作のフレームワークは、ヘッダー・エラー・境界の扱いを取りこぼす。互換テスト（16 章）で応答を突き合わせる。
- **出典**: [actix-web HttpServer](https://docs.rs/actix-web/latest/actix_web/struct.HttpServer.html)、[kawaemon の ISUCON14 感想戦](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)。axum の設定項目は出典未確認。

### perf で見えるようにするビルド（フレームポインタ・シンボル）

- **効く条件**: `perf report` が `[unknown]` だらけ、呼び元が分からない（コールグラフが空）。
- **やり方**
  - `[profile.release]` に `debug = "line-tables-only"` を入れる（シンボルとソース行。バイナリは大きくなるが、実行速度は変わらない）。
  - コールグラフは `RUSTFLAGS="-C force-frame-pointers=yes"` でビルドして `perf record -g`、または `perf record --call-graph dwarf`（出典未確認）。
  - `strip` を付けない。
- **確かめ方**: `perf report --no-children` で関数名が出る。
- **危険**: フレームポインタ付きはわずかに遅くなりうる（出典未確認）。計測用のビルドと最終のビルドを分けるなら、最終ベンチは最終のビルドで測る。
- **出典**: [rustc: Codegen options（force-frame-pointers・debuginfo）](https://doc.rust-lang.org/rustc/codegen-options/index.html)、[Cargo Book: Profiles](https://doc.rust-lang.org/cargo/reference/profiles.html)。（「コールグラフが無いので呼び元は推定」）。

### Rust のビルド時間を縮める（sccache・mold・cargo-zigbuild）

- **効く条件**: 反映のたびにビルドが数分かかる。複数の実行役が同じマシンで `cargo build` を並べて、手元の CPU が飽和する。
- **やり方**
  - **sccache**: `RUSTC_WRAPPER=sccache`（または `build.rustc-wrapper`）。ローカルのディスクにキャッシュする。`bin`・`cdylib`・`proc-macro` の crate は、リンクがあるのでキャッシュされない。インクリメンタルビルドの crate もキャッシュされない。`sccache --show-stats` で当たりを見る。
  - **mold**: リンクを速くする。`.cargo/config.toml` に `linker = "clang"` と `-C link-arg=-fuse-ld=mold`、または `mold -run cargo build`（Linux）。
  - **cargo-zigbuild**: Zig をリンカーにして、glibc のバージョンを指定してクロスビルドする（`cargo zigbuild --target x86_64-unknown-linux-gnu.2.17`）。本番の glibc より古い版を指定すれば、本番で動く。静的リンク（`+crt-static`）は glibc では使えず、musl を使う。インストールは `uv tool install cargo-zigbuild` など（`pip` を使わない）。
  - 開発・e2e 用のビルドは LTO を切り、`codegen-units` を増やす（`CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16`）。**本番のビルド設定とは違うので、最終ベンチは本番の設定で測る。**
  - 依存の層を先にビルドして使い回す（コンテナなら Dockerfile の層を分ける）。
- **確かめ方**: ビルド時間（`cargo build --release --timings`）。`sccache --show-stats` の hit 率。
- **出典**: [mozilla/sccache](https://github.com/mozilla/sccache)、[rui314/mold](https://github.com/rui314/mold)、[rust-cross/cargo-zigbuild](https://github.com/rust-cross/cargo-zigbuild)、[Rust Performance Book（リンカー）](https://nnethercote.github.io/perf-book/build-configuration.html)。

---

## 他の言語（要点だけ）

### Ruby（YJIT・Puma）

- **効く条件**: 初期実装が Ruby で、CPU がアプリに寄っている。
- **やり方**
  - YJIT は既定で無効。`--yjit`、環境変数 `RUBY_YJIT_ENABLE`、`RubyVM::YJIT.enable` で有効にする。メモリはインタプリタより増える。`--yjit-mem-size`（既定 128 MiB）で調整する。ワーカーを長く動かしたほうが効く（頻繁に再起動するとコンパイルの費用が上回る）（YJIT のドキュメント）。
  - Puma はクラスタモード（`workers`）で CPU 数に合わせる。MRI は 1 度に 1 スレッドしか Ruby を実行しない（GVL）ので、スレッドは I/O 待ち用。既定のスレッド数は 5。
  - ISUCON12 予選の講評は、ワーカー 32・スレッド 1 の Puma を基準の構成に使った。ISUCON10 予選の kirikiriyamama は、Puma に替えて約 720→840 点。
- **確かめ方**: `top` でプロセスごとの CPU、ベンチ前後。
- **危険**: YJIT は Ruby のバージョンと依存の gem で動かないことがある（出典未確認）。ワーカーを増やすと、DB の接続数が増える。
- **出典**: [YJIT のドキュメント](https://docs.ruby-lang.org/en/master/jit/yjit_md.html)、[Puma deployment](https://github.com/puma/puma/blob/master/docs/deployment.md)、[ISUCON12 予選の講評](https://isucon.net/archives/56850281.html)。

### Python（gunicorn・uvicorn）

- **効く条件**: 初期実装が Python（ISUCON14 の Python の最高は 13,033 点で、Go の 58,153 点との差が大きい。[言語別の結果](https://isucon.net/archives/58847106.html)）。
- **やり方**
  - gunicorn は、既定の `sync` ワーカーが 1 度に 1 リクエストで keep-alive も無い。nginx の背後に置く。`gthread`（ワーカー × スレッド）、`gevent` もある。ワーカー数は `(2 × CPU) + 1` が出発点で、負荷で調整する。重い負荷でも 4〜12 ワーカー程度で足りることが多い（公式）。
  - ASGI なら、gunicorn + uvicorn のワーカー（`uvicorn-worker`）、または `uvicorn --workers`。
  - CPU ボトルネックの処理は、プロセスを増やすか、別の言語（Rust の拡張や 16 章の置き換え）を検討する。
- **確かめ方**: ワーカーごとの CPU、`top`。
- **危険**: ワーカーを増やすとメモリが増える。`gevent` はライブラリの修正（monkey patch）が要ることがある。
- **出典**: [Gunicorn の design](https://gunicorn.org/design/)、[Uvicorn の deployment](https://uvicorn.dev/deployment/)、[ISUCON14 の言語別の結果](https://isucon.net/archives/58847106.html)。

### Node.js（cluster・worker_threads）

- **効く条件**: 初期実装が Node.js。1 プロセスしか CPU を使っていない。
- **やり方**: `node:cluster` で CPU 数だけ `fork` する（`availableParallelism`）。既定は非 Windows ではラウンドロビン。Node はワーカー数を自動で管理しない。プロセスの分離が要らないなら `worker_threads`。CPU 重めの処理（ハッシュ・JSON）は、メインのイベントループを止めないよう、ワーカーに出す。
- **確かめ方**: `top` でプロセスごとの CPU。
- **危険**: プロセスごとにメモリ上の状態が別になる（キャッシュ・セッション）。共有が要るなら Redis か DB。
- **出典**: [Node.js cluster](https://nodejs.org/api/cluster.html)。

### PHP（PHP-FPM・opcache）

- **効く条件**: 初期実装が PHP。
- **やり方**
  - `pm = static` で `pm.max_children` を固定する（負荷が一定のとき）。`pm.max_children` は同時に処理できるリクエスト数の上限。メモリから逆算する（`(PHP に使えるメモリ) ÷ (ワーカー 1 つのメモリ)`。逆算は一般的な目安で、出典未確認）。
  - opcache は既定で有効。`opcache.validate_timestamps=0` にするとファイルの確認が消えるが、デプロイのたびにリセットが要る。JIT は `opcache.jit=tracing` と `opcache.jit_buffer_size`（PHP 8.4 から既定は無効）。
  - PHP-FPM のプロセスは外からサンプリングして調べる。
- **確かめ方**: `pm.status_path`、`top`。
- **危険**: `validate_timestamps=0` のとき、コードの反映の後にリセットを忘れると、古いコードが動き続ける。再起動後に手作業なしで起動する。
- **出典**: [PHP-FPM の設定](https://www.php.net/manual/en/install.fpm.configuration.php)、[OPcache の設定](https://www.php.net/manual/en/opcache.configuration.php)。

### Perl（Plack・Starman）

- **効く条件**: 初期実装が Perl。
- **やり方**: ワーカー数を増やす。ISUCON5 本選の fujiwara は、Plack の worker を 5 から 10 に増やしただけで点が約 2 倍になったと記憶している。Perl は、ISUCON14 の本選で 4 チーム全部が FAIL だった（[言語別の結果](https://isucon.net/archives/58847106.html)）。
- **確かめ方**: `top`、ベンチ前後。
- **危険**: ワーカー数に比例して DB の接続数・メモリが増える。
- **出典**: [fujiwara の ISUCON5 の記録](https://sfujiwara.hatenablog.com/entry/2015/11/02/103440)。Starman 固有の設定は出典未確認。

---

## よくある失敗

| 失敗 | 実例 | 対策 |
| --- | --- | --- |
| プロファイルを見ずに、JSON・アロケータ・GC のライブラリを替える | GoJay に替えても点が変わらず撤回（ISUCON10 予選）。jemalloc を入れても差が出ない（ISUCON4 予選） | 計測の上位に出ている処理だけを替える。前後を同じ負荷で比べる |
| ビルド機と本番の CPU が違う | `target-cpu=native` で別の台が起動しない（一般的な危険。手元の記録は無い） | 実行時の判定、または全台が対応する水準を指定する |
| トランザクションの途中でハンドラが捨てられる | Rust 化の練習で、初期化の `TRUNCATE` が 46 秒止まった | トランザクションの処理を `tokio::spawn` で最後まで走らせる |
| 初期実装の再試行を、置き換えで落とす | デッドロックを 500 のまま返して整合性の失格に（練習） | 同じ回数・同じ条件で再試行する（16 章） |
| ログを切って検証できなくなる | 振り分けが意図どおり動いたか確認できなかった（ISUCON12 予選 daiju） | ログは終盤に切る |
| 検査を省いて不正な入力を通す | NG ワードの判定を移して、期待 400 に 201 を返した | 境界の入力の golden と critical のテスト |
| 再起動後に動かない | 新しいサービスを enable していない、手作業が要る | `systemctl is-enabled`、再起動試験を 4 台同時に（15 章） |

## 出典一覧

### ISUCON 公式

- [ISUCON14 問題の解説と講評](https://isucon.net/archives/58869617.html)、[ISUCON14 利用言語比率と言語別 TOP10](https://isucon.net/archives/58847106.html)
- [ISUCON13 問題の解説と講評](https://isucon.net/archives/58001272.html)
- [ISUCON12 予選 問題の解説と講評](https://isucon.net/archives/56850281.html)、[ISUCON12 本選 問題の解説と講評](https://isucon.net/archives/56959385.html)
- [ISUCON11 予選 問題の解説と講評](https://isucon.net/archives/56044867.html)、[ISUCON11 本選 問題の解説と講評](https://isucon.net/archives/56163308.html)
- [『達人が教えるWebパフォーマンスチューニング』の目次（技術評論社）](https://gihyo.jp/book/2022/978-4-297-12846-3)（章立てのみ。本文は引いていない）

### 参加記

- [NaruseJun の ISUCON13（優勝）](https://zenn.dev/tohutohu/articles/923bdf5dcd73af)、[NaruseJun の ISUCON12 本選（優勝）](https://zenn.dev/tohutohu/articles/8c34d1187e1b21)
- [LayerX の ISUCON14](https://zenn.dev/layerx/articles/3bd55b77e047c4)
- [kawaemon の ISUCON14 感想戦（Rust）](https://zenn.dev/kawaemon/articles/529fd6a12ac43b)
- [Dice801 の ISUCON12 本選（Rust）](https://zenn.dev/dice801/articles/ebc027207257b1)、[daiju の ISUCON12 予選（Rust）](https://zenn.dev/daiju/articles/165da7a5589434)
- [matsuu の ISUCON9 予選](https://matsuu.hatenablog.com/entry/2019/09/08/195735)、[genya0407 の ISUCON9](https://genya0407.github.io/post/isucon9)、[fujiwara の ISUCON5](https://sfujiwara.hatenablog.com/entry/2015/11/02/103440)
- [mazrean/isucon-go-tools](https://github.com/mazrean/isucon-go-tools)、[同 PR #91](https://github.com/mazrean/isucon-go-tools/pull/91)

### 公式ドキュメント

- Go: [GC ガイド](https://go.dev/doc/gc-guide)、[container-aware GOMAXPROCS](https://go.dev/blog/container-aware-gomaxprocs)、[PGO](https://go.dev/doc/pgo)、[診断](https://go.dev/doc/diagnostics)、[pprof の使い方](https://go.dev/blog/pprof)、[接続の管理](https://go.dev/doc/database/manage-connections)、[runtime の環境変数](https://pkg.go.dev/runtime#hdr-Environment_Variables)、[net/http transport.go](https://github.com/golang/go/blob/master/src/net/http/transport.go)、[net/http](https://pkg.go.dev/net/http)、[regexp](https://pkg.go.dev/regexp)、[html/template](https://pkg.go.dev/html/template)、[MinimumRequirements（GOAMD64）](https://go.dev/wiki/MinimumRequirements)、[golang/go#68346](https://github.com/golang/go/issues/68346)
- Go のライブラリ: [goccy/go-json](https://github.com/goccy/go-json)、[bytedance/sonic](https://github.com/bytedance/sonic)
- Rust: [tokio](https://docs.rs/tokio/latest/tokio/index.html)、[tokio Builder](https://docs.rs/tokio/latest/tokio/runtime/struct.Builder.html)、[block_in_place](https://docs.rs/tokio/latest/tokio/task/fn.block_in_place.html)、[tokio::sync::Mutex](https://docs.rs/tokio/latest/tokio/sync/struct.Mutex.html)、[Cargo の profile](https://doc.rust-lang.org/cargo/reference/profiles.html)、[rustc の codegen options](https://doc.rust-lang.org/rustc/codegen-options/index.html)、[is_x86_feature_detected](https://doc.rust-lang.org/std/macro.is_x86_feature_detected.html)、[Rust Performance Book](https://nnethercote.github.io/perf-book/build-configuration.html)、[actix-web HttpServer](https://docs.rs/actix-web/latest/actix_web/struct.HttpServer.html)、[sqlx PoolOptions（ソース）](https://github.com/launchbadge/sqlx/blob/main/sqlx-core/src/pool/options.rs)、[sqlx-core PoolOptions](https://docs.rs/sqlx-core/latest/sqlx_core/pool/struct.PoolOptions.html)
- Rust のライブラリ・道具: [mimalloc](https://github.com/microsoft/mimalloc)、[tikv-jemallocator](https://github.com/tikv/jemallocator)、[sonic-rs](https://github.com/cloudwego/sonic-rs)、[ring](https://github.com/briansmith/ring)、[aws-lc-rs](https://github.com/aws/aws-lc-rs)、[isa-l_crypto](https://github.com/intel/isa-l_crypto)、[sccache](https://github.com/mozilla/sccache)、[mold](https://github.com/rui314/mold)、[cargo-zigbuild](https://github.com/rust-cross/cargo-zigbuild)
- 他の言語: [YJIT](https://docs.ruby-lang.org/en/master/jit/yjit_md.html)、[Puma](https://github.com/puma/puma/blob/master/docs/deployment.md)、[Gunicorn](https://gunicorn.org/design/)、[Uvicorn](https://uvicorn.dev/deployment/)、[Node.js cluster](https://nodejs.org/api/cluster.html)、[PHP-FPM](https://www.php.net/manual/en/install.fpm.configuration.php)、[OPcache](https://www.php.net/manual/en/opcache.configuration.php)、[PHP RFC bcrypt cost](https://wiki.php.net/rfc/bcrypt_cost_2023)

### 手元の資料

- （`Cargo.toml`・`src/jpeg.rs`・`src/image_signature_mb.rs`・`src/server.rs`）
- 、`isucon-kit/tools/cpu-features/README.md`
