# noise

スコアの「ぶれの幅」を出し、2 群の差がぶれの中かどうかを判定する道具。

## なぜ要るか

**1 本ずつの比較で判断しないための道具。**

同じ commit でも点はぶれる。
それなのに、1 本の差で設定を何度も切り替えた。ぶれを、改善や悪化と取り違えていた。

- 同じ条件を数本流して、ぶれの幅を先に知る（`band`）。
- 平均の差の95%信頼区間が0を含まないとき、良い・悪いと言う（`compare`）。
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

SCOREBOARD の label 列で選ぶ。古い記録では次の方法も使える。

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

各群2本以上で、Welch の t による平均の差の95%信頼区間を出す。
区間が0を含まなければ向きに応じて良い・悪いとする。
0を含む場合は差なしとする。等しいと証明した意味ではない。
交互に流した同じ本数は `--paired` で入力順に対にする。
自由度の t は表と補間で求める。
1本単位のぶれの幅は参考として残す。
本数に限りがあれば、プロファイルとスコアの項ごとの数字を合わせて暫定の採否を決める。

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

## 参考のぶれと平均の差

band の標準偏差や範囲は1本単位のばらつきを表す。
compare の信頼区間は平均の差の不確かさを表す。
本数を増やすと平均の差を絞れる。
独立した走行を仮定する。
交互の走行は --paired を使い、対を保つ。
失格や未記録を含むときは、使える対を ids= で明示する。

テストは `uv run --no-project python -m unittest discover -s tools/noise/tests`。
