---
name: isu-review
description: 別モデルレビュー（smart-friend）。Claude の設計や差分を Codex に、Codex のものを Claude にレビューさせる。永続化・追試・全書き換えの設計・整合性まわりなど「間違えると失格」の不可逆な変更のときに使う。「レビューして」「設計を見てもらって」「別モデルで確認」と言われたら使う。
---

# isu-review — 別モデルレビュー

## いつ使うか（全部の diff には使わない）

- 永続化・再起動後の復旧・データ保持に触る変更
- インメモリ全書き換えの設計（実装前）と、切り替え前の最終差分
- 整合性・トランザクション境界・キャッシュ無効化の変更
- 台数構成の変更（DB の分離、アプリの複数台化）

インデックス追加や単純な N+1 解消には使わない（ベンチで分かる）。

## 使い方

```sh
bin/isu-review plan docs/DECISIONS.md     # 実装前の設計（ファイル）
echo "<設計>" | bin/isu-review plan -     # 標準入力でも可
bin/isu-review diff                      # main...HEAD の差分
bin/isu-review diff <base> --with claude # レビュアーを明示
```

- Claude から呼ぶと Codex が、Codex から呼ぶと Claude がレビューする
- 返り値は BLOCKER / MAJOR / MINOR 付きの箇条書きか「LGTM」

## 結果の扱い

- BLOCKER: 直すまでベンチに出さない
- MAJOR: 直すか、直さない理由を docs/DECISIONS.md に 1 行で書く
- MINOR: 時間があれば
- レビュアーの主張にも出典を求める。根拠のない指摘で手を止めない
