---
name: isu-analyze
description: 計測から次のボトルネックを探す。「結果を分析する」「どこが遅い」と言われたときに使う。
---

# 計測を読む

`results/<id>/SUMMARY.md`、`portal.txt` と作戦ボードを読む。
スコアの式の項ごとに取りこぼしを数える。
`tools/bench-window/bench-window <host>=results/<id>/<host>/access.log` で詰まった窓を見る。
利用者の動きは `tools/capture/capture flows <ログ> --user-key cookie` で読む。
DB の種類に応じ、slow.txt か pg-stat-statements.txt を読む。
CPU・待ち・回線の量を同じ窓で比べる。
`tools/noise/noise -f results/SCOREBOARD.md compare A B` で平均の差を読む。
見たもの → 考え → 決めたことを記録し、作戦ボードを直す。
手引きは `docs/tuning/README.md` から引く。
