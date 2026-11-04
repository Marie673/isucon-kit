---
name: isu-finalize
description: 凍結・再起動試験・最終ベンチを確かめる。「提出前」「最後の確認」と言われたときに使う。
---

# 提出前の確認

時刻は `isu.env` と `docs/RULES.md` の終盤の時刻表から取る。

1. 試験中のフラグを採るか消すか決める。`tools/flags/flags --ledger docs/flags.md lint --code <ソース> --env hosts` を通す。
2. 計測が済んだら `bin/isu-logs off` でログを止める。変更したパスをコミットし、凍結前に `bin/isu-deploy` で反映する。
3. `bin/isu-reboot-test --yes` で全サーバーを同時に再起動する。順序を変える試験は `--random` を使う。続けて skill portal-bench の `start --no-deploy --no-measure` で書いたデータとスコアを確かめる。
4. 永続化の表に再起動試験の成功と証跡を書く。
5. 最終ベンチは skill portal-bench の `start --no-deploy --no-measure` で同じコードの版を測る。
6. 最終点が最後の1本の回は `tools/stop-rule/stop-rule --scores-file results/final-scores.txt --remaining <残り本数> --last <直近の点>` で止め時を考える。失敗は0点に含める。
7. `bin/isu-doctor --final` で反映した sha・enable・ログと計測の停止・ディスク・永続化を確かめる。結果のコミットで動いた HEAD とコードの sha を取り違えない。
8. 終了の60秒前までに長い監視と接続を終える。最終結果を作戦ボードへ書く。

凍結後に再反映が必要なら理由を記録し、`bin/isu-deploy --after-freeze` を明示する。
Chrome の道具が無い場合は skill portal-bench の代わりの手順を使う。
