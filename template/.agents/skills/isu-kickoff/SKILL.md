---
name: isu-kickoff
description: 競技リポジトリの環境と初回計測を整える。「開始する」「開幕の準備」と言われたときに使う。
---

# 開始する

1. `docs/README.md` の作戦ボードを読む。仕事はボトルネック表へ置く。
2. マニュアルから `docs/RULES.md` を埋める。終盤の時刻表と `isu.env` の `ISU_END_AT`・`ISU_FREEZE_AT` を揃える。ホスト・サービス・DB の種類・入口・ポータルも埋める。
3. `tools/checkpoint/checkpoint start` で開始を記録する。終盤は `ISU_END_AT` から計算する。
4. `bin/isu-doctor` で不足と未対応を確かめる。初回の計測道具が未導入なら `bin/isu-setup` を使う。代わりの計測が必要なら先に作戦ボードへ書く。
5. `bin/isu-backup initial`、`bin/isu-inventory`、`bin/isu-import` で元の状態を残す。秘密を除き、変更したパスだけを stage してコミットする。
6. `bin/isu-doctor` を再実行する。skill portal-bench で最初のベンチを自分で流す。利用者の動きを読む初回は `start --capture` を使う。
7. skill app-profile・score-model・capacity-plan で数字を集める。判断を「見たもの → 考え → 決めたこと」で書く。skill main-loop へ進む。

Claude Code の Chrome 拡張とポータルのサイトの許可を事前に済ませる。
Chrome の道具が無い場合は skill portal-bench の代わりの手順を使う。
