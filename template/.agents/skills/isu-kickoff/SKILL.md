---
name: isu-kickoff
description: 競技リポジトリの環境と初回計測を整える。「開始する」「開幕の準備」と言われたときに使う。
---

# 開始する

1. `docs/README.md` の作戦ボードを読む。仕事はボトルネック表へ置く。
2. マニュアルから `docs/RULES.md` を埋める。IP・ポータル・起動用の鍵・終了と凍結の時刻を `isu.env` に書く。サービス名は inventory の後で確かめる。
3. `bin/isu-doctor --bootstrap` を通す。この段階では ssh や計測の道具を要求しない。
4. `bin/isu-keys` で競技用の鍵を配る。
5. `bin/isu-setup` で計測の道具を入れる。DB の種類とホストがまだ分からなければ `ISU_DB_KIND=none` にして、inventory の後に設定して再実行する。
6. `bin/isu-backup initial`、`bin/isu-inventory` の順で元の状態と実体を残す。DB を後から見つけたときは DB の設定後にバックアップも追加する。
7. `isu.env` を実際のサービス・DB・入口・ポートに揃える。必要なら setup を再実行する。
8. `bin/isu-import` で取り込む。秘密を除き、変更したパスだけをコミットする。
9. `bin/isu-doctor` で接続・サービス・計測を検査する。checkpoint を展開した場合は `tools/checkpoint/checkpoint start` で開始を記録する。
10. skill portal-bench で最初のベンチを自分で流す。動線を読む初回は `start --capture` を使う。
11. skill app-profile・score-model・capacity-plan で数字を集める。判断を「見たもの → 考え → 決めたこと」で残し、main-loop へ進む。

Claude Code の Chrome 拡張とポータルのサイトの許可を事前に済ませる。
Chrome の道具が無い場合は skill portal-bench の代わりの手順を使う。
