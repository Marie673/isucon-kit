# flags

環境変数のフラグの台帳（`flags.md`）と、コード・設定ファイルを突き合わせる道具。

## なぜ要るか

環境変数のフラグは、放っておくと増え続ける。
どれが有効で、何のために入れて、いつ消すのか、誰も追えなくなる。

- フラグを足すとき、台帳に「目的・根拠・消す条件」を書く。
- `flags lint` が、台帳・コード・設定ファイルのずれを出す。
- 試験中のまま放っておいたフラグも出す。採用か削除かを決めさせる。

読み取りだけ（`flags new` だけが台帳に 1 行足す）。サーバーには接続しない。
標準ライブラリだけの Python。`uv` で動く（`pip` は使わない）。

## 台帳

雛形は [flags.template.md](flags.template.md)。プロジェクトの `flags.md` にコピーして使う（`flags new` なら無ければ雛形から作る）。
表の列は次のとおり。見出しは日本語でも英語（`name` `default` `prod` `purpose` `evidence` `added` `remove_when` `status`）でもよい。

| 列 | 書くこと |
| --- | --- |
| 名前 | 環境変数の名前 |
| 既定値 | 環境変数が無いときの値（コードの既定） |
| 本番の値 | 競技サーバーに入れている値。未設定なら `-` |
| 目的 | 何を速く（安全に）するためか |
| 根拠（PR / 計測） | PR 番号と、効果を見た計測 |
| 入れた時刻 | `2026-10-31 14:05` の形（`14:05` だけでも可。今日として読む） |
| 消す条件 | どうなったら消すか。空・`-`・`なし`・`未定` は「空」として指摘する |
| 状態 | `試験中` / `有効` / `無効` / `削除済`（`trial` `removed` なども読む） |

消したフラグは、行を消さずに状態を `削除済` にする。

## 使い方

### lint

```sh
flags lint --ledger flags.md --code webapp/ --env servers/
```

- `--code`: コードのディレクトリ（繰り返し可）。次の形の参照を拾う。
  Rust の `env::var("X")`・`env!`・clap の `env = "X"`、Go の `os.Getenv` / `os.LookupEnv`、Node の `process.env.X` / `process.env["X"]`、
  Python の `os.environ[...]` / `os.environ.get` / `os.getenv`、`getenv("X")`、Ruby の `ENV["X"]`、PHP の `$_ENV`、Java の `System.getenv`、
  シェルの `${X:-既定値}`。
- `--env`: 設定ファイルのディレクトリ（繰り返し可）。systemd の drop-in（`Environment=X=1`、`Environment="A=1" "B=2"`）と、env ファイル（`X=1`、`export X="1"`）を読む。
- `--pattern`: 名前の形の正規表現。既定は `[A-Z][A-Z0-9_]{3,}`。
- `--ignore REGEX`（繰り返し可）: フラグではない設定を無視する（例: `--ignore '^DB_'`）。`PATH`・`HOME`・`RUST_LOG` などは既定で無視する（`--no-default-ignore` で外す）。台帳に載っている名前は無視されない。
- `--exclude NAME`: 走査から外すディレクトリ名。`.git`・`node_modules`・`target`・`vendor` などは既定で外す。`.md` などコードでないものも読まない。
- `--trial-hours N`: 試験中のままで許す時間（既定 2）。
- 問題があれば終了コード 1、無ければ 0。`--code` と `--env` は片方だけでもよい（無いほうの検査は飛ばす）。

出す項目:

| 項目 | 意味 |
| --- | --- |
| 台帳に無いフラグ | コードか設定ファイルにあるのに、台帳に行が無い |
| コードに無いのに台帳にある | 状態が `削除済` でないのに、コードが参照していない（設定ファイルに残っていれば併記） |
| 削除済みなのに残っている | 状態が `削除済` なのに、コードか設定ファイルに残っている |
| 消す条件が空 | 消す条件が空・`-`・`なし`・`未定`・`TBD` |
| 目的・根拠が空 | 目的か根拠が空 |
| 試験中のまま N 時間を超えた | 状態が `試験中` で、入れた時刻から N 時間以上。入れた時刻が読めないものも出す |
| 台帳に同じ名前が複数ある | 重複 |
| 台帳の本番の値と設定ファイルが合わない | 台帳の本番の値（未設定なら既定値）が、設定ファイルの値と違う。台帳が既定値と違う値なのに、設定ファイルに設定が無い |

### list

台帳の 1 行ごとに、設定ファイルの値とコードの参照数を並べる。「今どれが効いているか」を 1 枚で見る。

```sh
flags list --ledger flags.md --code webapp/ --env servers/
```

### new

台帳に行を足す。台帳が無ければ雛形から作る。名前が既にあれば失敗する。

```sh
flags new ISU_SIG_BATCH --default 0 --prod 1 --purpose "計算をまとめる" \
  --evidence "PR #679" --remove-when "効果が無いと分かったら"
```

入れた時刻は既定で今。状態は既定で `試験中`。消す条件・目的・根拠が空なら、注意を出す。
表の後ろの文章は動かさない。

### durability

永続化を弱める DB の設定（`innodb_flush_log_at_trx_commit` 0/2、`sync_binlog=0`、doublewrite を切る、PostgreSQL の `fsync`・`synchronous_commit`・`full_page_writes` を off、Redis の `appendfsync no`、DB のデータを tmpfs に置く）を探し、決まりの記録（既定 `docs/DECISIONS.md`）にその名前が書かれていなければ、終了コード 1 で止める。

```sh
flags durability --conf hosts --conf webapp --decisions docs/DECISIONS.md
```

- 追試（再起動後にデータが残る）に触れる設定は、`docs/DECISIONS.md` の記録と再起動試験が要る。
- 入れるなら、メインエージェントが決める。DECISIONS.md の「永続化を弱める設定」の表に設定・理由・危険・撤回の条件・再起動試験を書く。理由・危険・撤回の条件の空欄は通らない。反映前は試験が「未」でもよい。`durability --final` は成功した試験を要求する。
- `isu-deploy` は、この道具があれば反映の前に流し、止まったら反映しない。
- 設定ファイル・compose・SQL の行を正規表現で見る。`#`・`;`・`-- `・`//` で始まる行は読まない。`.md` は読まない。

## 出力例

`tests/fixtures`（コード 5 ファイル、設定ファイル 2 つ、台帳 9 行）で実行したもの。

```sh
cd tools/flags/tests/fixtures
../../flags lint --ledger flags.md --code code --env env --now 2026-10-31T18:00:00+09:00
```

```markdown
# flags lint

- 台帳: 9 件（有効 5、試験中 2、削除済 2）
- コード: 10 個のフラグを参照
- 設定ファイル: 5 個のフラグを設定

## 台帳に無いフラグ（3）

- `DATABASE_URL` — コード tests/fixtures/code/server.js:2
- `ISU_ORPHAN_ENV` — 設定 tests/fixtures/env/host1/app.conf:4
- `ISU_UNLISTED_FLAG` — コード tests/fixtures/code/src/main.rs:6

## コードに無いのに台帳にある（1）

- `ISU_DEAD_FLAG` — 台帳 10 行目、状態 有効

## 削除済みなのに残っている（1）

- `ISU_PY_OLD` — 状態は 削除済 なのに、コード tests/fixtures/code/tool.py:4

## 消す条件が空（1）

- `ISU_SIG_BATCH` — 台帳 6 行目

## 目的・根拠が空（1）

- `ISU_GO_FLAG` — 目的・根拠が空（台帳 9 行目）

## 試験中のまま 2 時間を超えた（2）

- `ISU_SIG_BATCH` — 試験中のまま 4.7 時間。採用か削除かを決める
- `ISU_NODE_FLAG` — 試験中のまま 4.0 時間。採用か削除かを決める

## 台帳の本番の値と設定ファイルが合わない（2）

- `ISU_NODE_FLAG` — 台帳の本番の値は off、設定ファイルの値は on
- `ISU_DEAD_FLAG` — 台帳の本番の値は 1（既定値は 0）なのに、設定ファイルに設定が無い
```

`flags list` の出力:

```markdown
| 名前 | 状態 | 既定値 | 本番（台帳） | 設定ファイルの値 | コード参照 | 入れた時刻 | 消す条件 |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| `ISU_FAST_FEED` | 有効 | 0 | 1 | 1 | 1 | 2026-10-31 11:05 | 効果が無いと分かったら |
| `ISU_SIG_BATCH` | 試験中 | 0 | 1 | 1 | 1 | 2026-10-31 13:20 | - |
| `ISU_NODE_FLAG` | 試験中 | off | off | on | 1 | 2026-10-31 14:00 | 次の再起動試験で |
| `ISU_PY_FLAG` | 有効 | 1 | 2 | 2 | 1 | 2026-10-31 15:00 | 恒久化したら既定値にする |
（以下略）
```

`flags new` の結果（台帳の末尾）:

```markdown
| 名前 | 既定値 | 本番の値 | 目的 | 根拠（PR / 計測） | 入れた時刻 | 消す条件 | 状態 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ISU_SIG_BATCH` | 0 | 1 | 計算をまとめる | PR #679 | 2026-10-31 13:20 | 効果が無いと分かったら | 試験中 |
```

## 運用のしかた

- フラグを足す PR では、同じ PR で `flags new` を実行して台帳に行を足す。消す条件を必ず書く。
- 反映のたびに（または定期的に）`flags lint` を回す。`台帳に無いフラグ` が出たら、その場で台帳に足す。
- 試験中が出たら、ベンチで決めて、採用なら `有効`、不採用ならコードから消して `削除済` にする。
- 最終候補の前に、`消す条件` が「恒久化したら」のフラグは既定値に固めて、環境変数を消す。

## 限界

- 参照の検出は正規表現。名前を文字列で組み立てている参照（`"ISU_" + name`）は拾えない。
- 設定ファイルは「台ごとの違い」を見ない。値が台で違うときは、台帳の値が 1 つでも一致していれば合うとみなす（`flags list` で台ごとの値を見る）。
- systemd の `EnvironmentFile=` が指す先は、`--env` に含めたときだけ読む。

## テスト

```sh
uv run python -m unittest discover -s tools/flags/tests
```

台帳の既定は docs/flags.md があればそれを使う。
probe も2時間の期限の対象にする。
