# isucon-kit — 何を回すかを決める仕組み

ISUCON の次回用キット。2026-10-31 の本番（ISUCON2026、145,523 点。1 位は 625,857 点）の振り返りから作った。

本番では、変更を入れる速さに問題はなかった（1 時間に 111 件のマージ）。足りなかったのは「何を変えるか」を決める仕組みで、スコアの大半を左右した事実（スコアの式、ピークの CPU の計算、利用者の行動の連鎖、サーバーごとの CPU の機能）は、どれも競技の後に分かった。このキットは、その層を当日の最初の 90 分で作るためのもの。中心は、アプリの特性（いつ・何をきっかけに・一斉に動くか）を見た目の似たサービスで決めつけずに書くこと、スコアのモデルを書くこと、そして序盤のベンチを「傾向を掴む実験」に使うこと。

- 作戦: [docs/strategy.md](docs/strategy.md)（REV 4。メインエージェント 1 本の体制・時間割・決まり）
- チューニングマニュアル: [docs/tuning/](docs/tuning/)（層ごとの手引き。ボトルネックが分かった後に引く）
- AGENTS.md の剪定の記録: [docs/agents-md-pruning.md](docs/agents-md-pruning.md)

## 構成

```
bin/kit-new              競技リポジトリを作る（template/ と tools/ を展開する）
setup/                   事前準備（mac.sh: 母艦に要るもの、aws-preflight.sh: AWS アカウントの確認）
template/                競技リポジトリに入るもの
  AGENTS.md              当日の決まり（isucon2026 の 273 行を剪定したもの）
  docs/README.md         作戦ボード（判断の表・撤回の条件・ボトルネック表・時間割）
  docs/design.md         設計の雛形（A 仕様の振る舞い・B アプリの特性・C スコアのモデル・D ピークのキャパシティ）
  docs/experiments.md    ベンチごとの結果・ぶれの幅・比較の判定・傾向を掴む実験の記録
  .agents/skills/        main-loop・delegate・app-profile・score-model・capacity-plan・probe-experiments・experiment-rules と、流れの手順（isu-kickoff・isu-loop・isu-analyze・isu-rewrite・isu-finalize など）
  bin/isu-*              反映・ロック・計測・ベンチ・スコア・golden・再起動試験・レビュー・タスク
  isu.env                競技環境の定義（IP・役割・サービス名・ビルド方法）
  conf/ golden/ results/ 計測の設定、golden、ベンチごとの要約
  templates/inmemory-rust/  インメモリ全書き換えの雛形（seed＋delta の永続化）
tools/                   競技リポジトリの tools/ に入る道具（Python、uv で実行）
  bench-window/          ピークの窓に時刻を揃えた秒単位の要約（nginx のログ・CPU）
  noise/                 ぶれの幅と比較の判定（SCOREBOARD.md も読める）
  flags/                 フラグの台帳と、コード・設定との突き合わせ。永続化を弱める設定の点検（durability）
  cpu-features/          サーバーごとの CPU の機能と、サーバーで速さが変わる処理
  lab-aws/               本番と同じ系統の CPU の EC2 で手元の再現
docs/                    作戦・チューニングマニュアル（docs/tuning/）
```

## 2 層の作り

| 層 | 中身 | どこから |
|---|---|---|
| 方向 | スコアのモデル → ピークのキャパシティ → ボトルネック表、実験の決まり、メインエージェント（skill `main-loop`） | `template/` と `tools/` |
| 流れ | 反映・ベンチ・計測・要約・スコア・ロック・タスク・再起動試験 | `template/bin/isu-*`（isucon14 で検証済み） |

isucon2026 で作った流れの道具（Plane・`bq`・ベンチの 3 役・monitor・delivery）は持ち込まない。体制は**メインエージェント 1 本**（方向・反映とベンチ・小さな実装・取り込み）と、コンテキストで切れる仕事にだけ呼ぶ subagent（同時に 0〜4）にする。役を分けると、要約を渡すたびに詳細が欠け、方向を決める知識が誰のコンテキストにも揃わなかった（[docs/strategy.md](docs/strategy.md) の REV 4 の変更スコア）。

## 使い方

### 事前
```sh
ghq get -p Marie673/isucon-kit
setup/mac.sh                     # zig・cargo-zigbuild・Rust の linux ターゲット・dot・SSH 鍵
setup/aws-preflight.sh <profile> # 認証・vCPU 上限・キーペア・デフォルト VPC（読み取りだけ）
# ~/.ssh/isucon_ed25519.pub を GitHub に登録する（isu-keys が GitHub の公開鍵を配る）
```

### 当日
```sh
~/workspace/github.com/Marie673/isucon-kit/bin/kit-new ~/workspace/github.com/<org>/<競技リポジトリ>
cd ~/workspace/github.com/<org>/<競技リポジトリ>
# isu.env を埋めてコミット → docs/README.md（作戦ボード）から始める
# main の canonical checkout で agent を 1 本立て、「skill main-loop で始めて」
```

`--dry-run` で何をコピーするかだけ見られる。

### 流れの道具

| やること | コマンド | 何が起きるか |
|---|---|---|
| ベンチ 1 回 | `bin/isu-bench --note "..."` | ロック → ビルド・反映 → golden の確認 → 計測開始 → ベンチの検知 → 回収・要約 → 解放 → `results/<id>/SUMMARY.md` をコミット |
| スコアの記録 | `bin/isu-score <id> <score>` | SUMMARY と SCOREBOARD を更新してコミット |
| 状況の確認 | `bin/isu-lock status`・`bin/isu-score`・`bin/isu-task ready` | 誰がサーバーを使っているか・スコアの推移・着手できるタスク |
| 別のモデルのレビュー | `bin/isu-review plan\|diff` | Claude と Codex で互いにレビュー |
| 再起動試験 | `bin/isu-reboot-test --yes` | 全サーバーを再起動し、サービスとヘルスチェックを確かめる |

### 道具のテスト
```sh
for t in tools/*/; do uv run python -m unittest discover -s "$t/tests"; done
```
