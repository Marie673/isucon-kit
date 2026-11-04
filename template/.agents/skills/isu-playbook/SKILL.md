---
name: isu-playbook
description: ISUCON のチューニング定石集。DB（インデックス・N+1・my.cnf）、アプリ（キャッシュ・まとめ取り・非同期化・Go/Rust の勘所）、インフラ（nginx・カーネル・複数台構成）、過去問の頻出パターン（ポーリング間隔・通知・画像配信・ランキング）を引く。改善の実装方法を決めるとき、「どう直す」「定石」と言われたら使う。
---

# isu-playbook — 定石集

計測で場所を特定してから引くこと（`isu-analyze`）。症状 → 参照先:

| 症状（SUMMARY.md で見えるもの） | 読む |
|---|---|
| mysqld の CPU が支配的、スロークエリの R/Call が大きい | [references/db.md](references/db.md) インデックス |
| スロークエリの Calls が桁違い | [references/db.md](references/db.md) N+1 / まとめ取り |
| COMMIT / INSERT が上位、wa が高い | [references/db.md](references/db.md) my.cnf（flush・binlog） |
| アプリの CPU が支配的、perf に JSON / 正規表現 / GC | [references/app.md](references/app.md) |
| 同じ GET が大量（ポーリング・一覧） | [references/patterns.md](references/patterns.md) ポーリング / キャッシュ |
| 画像・静的ファイルが alp 上位 | [references/infra.md](references/infra.md) nginx 配信 |
| 1台が飽和し他が遊んでいる | [references/infra.md](references/infra.md) 複数台構成 |
| idle が多いのにスコアが伸びない | [references/patterns.md](references/patterns.md) 待ち・ペーシング |

## 鉄則

- 一度に1つ。効果を測ってから次へ
- スキーマ変更は初期化 SQL に入れる（initialize で毎回作られるように）
- キャッシュは「無効化の条件」を先に書いてから実装する。整合性で落ちたら元も子もない
- 「仕様が許す余地」（遅延・省略）は docs/RULES.md に出典があるものだけ使う
