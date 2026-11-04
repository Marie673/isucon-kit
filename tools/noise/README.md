# noise

スコアの「ぶれの幅」を出し、2 群の差がぶれの中かどうかを判定する道具。

## なぜ要るか

**1 本ずつの比較で判断しないための道具。**

同じ commit でも点はぶれる。
それなのに、1 本の差で設定を何度も切り替えた。ぶれを、改善や悪化と取り違えていた。

- 同じ条件を数本流して、ぶれの幅を先に知る（`band`）。
- 差がぶれの幅より大きいときだけ、良い・悪いと言う（`compare`）。
- 本数が足りなければ「本数不足」と返す。判断を保留して、もう 1 本流す。

標準ライブラリだけの Python。`uv` で動く（`pip` は使わない）。

## 入力

スコアの記録は 3 つの形を読める（中身で自動判別）。`--file`（`-f`）で渡す。
既定は `scores.csv`（環境変数 `NOISE_FILE` でも指せる）。`-` は標準入力。

| 形 | 例 |
| --- | --- |
| CSV（ヘッダ行つき） | `commit,score,pass,label` |
| JSON Lines | `{"commit": "a1b2c3d", "score": 10120, "pass": true, "label": "baseline"}` |
| Markdown の表 | `bin/isu-score` が作る `results/SCOREBOARD.md`（`\| id \| score \| Δ \| commit \| branch \| who \| note \|`） |

- 列は `commit`・`score`・`pass`・`label`・`id`・`note`。別名も読む（`sha`、`points`、`passed`、`result`、`name` など）。`pass` が無ければ全部通ったものとして扱う。`pass` が `false` / `fail` / `ng` / `失格` の行は、既定では統計に入れない（`--include-failed` で入れる）。
- 点が `?` や空の行（SCOREBOARD の「点の記録待ち」）は、統計に入れない。件数は表に出る。
- SCOREBOARD の `id` 列の `[<id>](<id>/SUMMARY.md)` は、リンクの文字列（`<id>`）だけを読む。

### SCOREBOARD.md を読むとき

SCOREBOARD には label 列が無い。次のどちらかで行を選ぶ。

- `--label-from-note <正規表現>`: note 列にこの正規表現が最初に合った部分を label にする（グループ 1 があればそれ）。note を「`条件名 run1`」のように書く決まりにしておけば、`'^(\S+)'` で条件名が取れる。
- `ids=`: id で行を選ぶ。`ids=A+B+C`、または `ids=A..B`（ファイルの順の範囲、両端を含む）。

## 使い方

### band: ぶれの幅を出す

```sh
noise band --label baseline                 # 同じ条件の n 本から
noise band --commit a1b2c3d                 # commit（前方一致）で選ぶ
noise band --ids 20261031-100500..20261031-102000   # id の範囲で選ぶ
noise band                                  # 選ばなければ、label ごとの一覧
```

平均・標準偏差（n-1 で割る標本標準偏差）・最小・最大・幅・変動係数（標準偏差 ÷ 平均）を出す。1 本だけのときは標準偏差が出せないので、本数不足と書く。

### compare: 2 群を比べる

```sh
noise compare baseline tune-b
noise compare commit=a1b2c3d commit=9c8d7e6
noise compare ids=A..B ids=C+D+E
```

A と B は「セレクタ」。`label=NAME`、`commit=SHA`、`ids=...` を `,` でつなぐと AND になる。
素の語は、label にあればそれ、無ければ commit の前方一致。

判定は単純にしてある。

1. 各群が `--min-n` 本（既定 2）未満なら、**本数不足**。
2. ぶれの幅 = 次の 4 つの最大。A の `k`σ、B の `k`σ（`--k`、既定 2）、A の範囲（最大−最小）、B の範囲。
3. 平均の差（B − A）の絶対値がぶれの幅以内なら、**差なし**。
4. それを超えたら、差が正なら**良い**、負なら**悪い**（小さいほど良い値は `--lower-is-better`）。

平均の差を、1 本のぶれ（標準偏差）と比べているので、判定は保守的になる。
見逃し（本当は少し良いのに「差なし」）は増えるが、ぶれによる誤判定は減る。
小さな改善は、本数を増やして確かめる。

終了コード:

| コード | 意味 |
| --- | --- |
| 0 | 差なし |
| 10 | 良い |
| 11 | 悪い |
| 12 | 本数不足 |
| 1 | 入力が読めない・該当する行が無い |
| 2 | 使い方の誤り |

```sh
noise compare baseline tune-b; case $? in 0) echo 保留;; 10) echo 採用;; 11) echo 戻す;; 12) echo もう1本;; esac
```

## 出力例

`tests/fixtures` のサンプルで実行したもの。baseline は 5 本（失格 1 本を除く）で、変動係数 5.3%。

```sh
cd tools/noise/tests/fixtures
../../noise -f scores.csv band --label baseline
```

```markdown
# noise band: label=baseline

| 本数 | 平均 | 標準偏差 | 最小 | 最大 | 幅（最大−最小） | 変動係数 | 平均 ± 2σ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | 10,088 | 537.09 | 9,340 | 10,690 | 1,350 | 5.3% | 9,014 〜 11,162 |
（このあとに 1 本ずつの表と、除いた行の注記が続く）
```

平均で +1.2% 上がった tune-a は、ぶれの中。+29% 上がった tune-b は、ぶれの外。

```sh
../../noise -f scores.csv compare baseline tune-a   # 終了コード 0
../../noise -f scores.csv compare baseline tune-b   # 終了コード 10
```

```markdown
# noise compare: A = baseline / B = tune-b

| 群 | セレクタ | 本数 | 平均 | 標準偏差 | 最小〜最大 | 幅 | 変動係数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | baseline | 5 | 10,088 | 537.09 | 9,340〜10,690 | 1,350 | 5.3% |
| B | tune-b | 3 | 13,037 | 565.18 | 12,480〜13,610 | 1,130 | 4.3% |

| 平均の差（B − A） | 差の割合 | ぶれの幅 | 差 ÷ 幅 |
| ---: | ---: | ---: | ---: |
| +2,948.7 | +29.2% | 1,350.0 | 2.18 |

ぶれの幅 = 2σ（A 1,074.2、B 1,130.4）と範囲（A 1,350、B 1,130）の最大。

**判定: 良い**（平均の差がぶれの幅を超え、B のほうがよい向き）

- A: 失格（pass でない）1 本は除いた
```

tune-a の判定は `差なし`（差 ÷ 幅 = 0.09）。1 本しかない `single` と比べると `本数不足`（終了コード 12）。

### SCOREBOARD.md を直接読む

```sh
../../noise -f SCOREBOARD.md --label-from-note '^(\S+)' band
```

```markdown
# noise band（ラベルごと）

| label | 本数 | 平均 | 標準偏差 | 最小〜最大 | 幅 | 変動係数 | 失格 | 未記録 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 3 | 9,980 | 582.75 | 9,340〜10,480 | 1,140 | 5.8% | 0 | 0 |
| nginx-keepalive | 3 | 10,287 | 637.36 | 9,620〜10,890 | 1,270 | 6.2% | 0 | 0 |
| index-feeds | 3 | 13,037 | 565.18 | 12,480〜13,610 | 1,130 | 4.3% | 0 | 1 |
```

`index-feeds` の「未記録 1」は、点が `?` の行（記録待ち）。統計には入らない。

```sh
../../noise -f SCOREBOARD.md --label-from-note '^(\S+)' compare baseline nginx-keepalive   # 差なし
../../noise -f SCOREBOARD.md compare ids=20261031-100500..20261031-102000 ids=20261031-105400..20261031-111000   # 良い
```

## 運用のしかた

- 「条件」ごとに label を決め、同じ条件を 3 本以上流す。baseline は最初に 3〜5 本。
- 条件を変えたら、新しい label で 3 本流し、`compare` にかける。
- 1 本だけ良かったからといって、採用も切り替えもしない。`本数不足` か `差なし` が返ったら、保留する。
- 本数が増えても同じ幅のままなら、そのぶれは環境のもの。幅は縮まらない前提で、差が大きい改善を狙う。

## 注意

- 点が高いほど良い前提。レイテンシなど小さいほど良い値は `--lower-is-better`。
- 2 本の群の標準偏差は不安定なので、幅は目安（注意が出る）。
- 統計の検定ではない（正規性や等分散を仮定していない）。「大きな差だけ拾う」ための目安。

## テスト

```sh
uv run python -m unittest discover -s tools/noise/tests
```
