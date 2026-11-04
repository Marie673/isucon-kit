# isucon-kit

まだ見ていない ISUCON の問題で使うキット。
利用者の動き・スコアの式・資源の制約から次の施策を選ぶ。
問題の内容と通しの結果は private の競技リポジトリへ置く。

作戦は [docs/strategy.md](docs/strategy.md)（REV 4）。
施策は [docs/tuning/README.md](docs/tuning/README.md) から引く。
評価の手順は [docs/eval.md](docs/eval.md) にある。

## 構成

| パス | 内容 |
|---|---|
| bin/kit-new | template と tools、手引きを競技リポジトリへ展開する |
| bin/kit-test | 全道具・偽 ssh・shellcheck・Rust のテスト |
| template/bin | 反映・ロック・計測・結果・検査の道具と共通処理 |
| template/.agents/skills | main-loop と実行の手順。Claude Code と Codex で共有する |
| template/docs | 作戦ボード、仕様、設計、判断、比較の雛形 |
| template/isu.env | ホスト・DB・入口・時刻・ポータルの設定 |
| tools/capture | pcap の平文 HTTP と利用者の動線 |
| tools/bench-window | ピークの窓を揃えたログと CPU の集計 |
| tools/noise | 平均の差の95%信頼区間と1本単位のぶれ |
| tools/flags | フラグの台帳と永続化の記録の検査 |
| tools/stop-rule | 最終ベンチの止め時 |
| tools/checkpoint | 終了時刻から計算する区切りの通知。既定で展開する |
| tools/cpu-features | CPU の機能の比較 |
| tools/lab-aws | 事前に決めた費用の範囲で使う練習環境 |
| setup | 母艦の準備と AWS の読み取り検査 |

## 事前準備

`ghq get -p Marie673/isucon-kit` で取得する。
`setup/mac.sh` の内容を確認して母艦を準備する。
Python は標準ライブラリと uv を使う。
メインは Claude Code で動かす。
Claude in Chrome を有効にし、ポータルのサイトの許可を済ませる。
`tabs_context_mcp` と `tabs_create_mcp` が使えることを練習で確認する。
Chrome の道具が無ければ、人間が押して finish に合否と値を渡す。

## 展開

```sh
bin/kit-new <競技リポジトリ>
# isu.env と docs/RULES.md を埋める。
# 競技リポジトリの main で bin/isu-doctor を実行する。
# skill isu-kickoff → main-loop を使う。
```

既定では既存ファイルを上書きしない。
`--update` は AGENTS.md・skill・bin・tools・手引きなどのキット管理のファイルを更新する。
作戦ボード・判断・環境・結果は守る。
`--no-checkpoint` で通知の道具を展開しない。
`--dry-run` で対象を見る。

## ベンチの流れ

```sh
bin/isu-bench start --note "狙いと変更" --label baseline
# skill portal-bench で押す前に待ち行列を確認し、job の結果を読む。
bin/isu-bench finish <id> --score <点> --pass --job <job> --portal-file <本文>
```

start はロック・反映・golden・計測開始までで戻る。
finish は計測停止・回収・窓・SUMMARY・SCOREBOARD・解放・コミットを行う。
途中でやめるときは `abort <id> --reason <理由>` を使う。
`wait <id>` は流量が静かになった合図だけを返す。
結果の正はポータルの job にする。
練習は `run` と `ISU_BENCH_CMD` を使える。
最終ベンチは `start --no-deploy --no-measure` で同じ版を測る。

| やること | コマンド |
|---|---|
| 起動時・最終の検査 | bin/isu-doctor、bin/isu-doctor --final |
| 合否の修正 | bin/isu-score ID SCORE --pass または --fail |
| 取り込みの前の検査 | bin/isu-intake BRANCH --paths PATH... |
| 状況 | bin/isu-lock status、bin/isu-score、docs/README.md |
| 別モデルのレビュー | bin/isu-review plan または diff |
| 再起動試験 | bin/isu-reboot-test --yes。順序を変えるときは --random |

## 検証

```sh
bin/kit-test
```

偽の ssh と一時ディレクトリを使う。
競技サーバーやクラウドには接続しない。
必要なものは uv、shellcheck、cargo。
