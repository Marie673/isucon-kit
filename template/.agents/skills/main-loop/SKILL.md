---
name: main-loop
description: 利用者とスコアから次の施策を選ぶ。「方向を決める」「次に何をする」「ボトルネックは何」と言われたときに使う。
---

# 主ループ（REV 4）

最初は skill isu-kickoff を使う。
作戦ボードは `docs/README.md` にある。

1. 利用者の要求を時系列で読む。`tools/capture/capture flows results/<id>/<host>/access.log --user-key cookie --groups conf/alp-groups.txt` を使う。平文の通信は `tools/capture/capture pcap results/<id>/<host>/capture.pcap --out results/<id>/<host>/http.jsonl` で対にする。
2. マニュアルと動きを照らし、何がスコアを生むかを説明する。skill app-profile と score-model を使う。
3. 制約から構成を考える。初期構成と比べ、`docs/DECISIONS.md` に「見たもの → 考え → 決めたこと」を書く。
4. 今のボトルネックを作戦ボードの先頭にする。小さな実装は自分で入れる。独立した実装・調べもの・検証は skill delegate で渡せる。
5. 関係するテストを通し、変更したパスだけをコミットする。
6. skill portal-bench で流す。ベンチ中はサーバーを変えず、次の調べものを進める。
7. 結果を読み、判断の数字・取りこぼし・次のボトルネックを作戦ボードへ書く。前提が崩れた案は考え直す。

本数が限られる比較は skill experiment-rules で決める。
時刻は `isu.env` と `docs/RULES.md` の終盤の時刻表から取る。
終盤は skill isu-finalize を使う。

判断は自分で行う。費用は事前に決めた範囲を守る。
自動 compact を前提に、決めたことはその場で `docs/` に書く。
compact の後は作戦ボードから読み直す。
人間が打てる場合の `/compact` は任意。モデルからは呼べない。
Chrome の道具が無い場合は skill portal-bench の代わりの手順を使う。
