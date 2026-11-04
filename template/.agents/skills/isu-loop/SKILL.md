---
name: isu-loop
description: ISUCON で1つの施策（仮説）を、タスクの着手から実装・確認・ベンチ依頼・結果記録まで進める手順（9ステップループ）。改善を担当するとき、「このタスクをやって」「改善して」と言われたら使う。
---

# isu-loop — 1施策の進め方

## 0. 準備（セッション開始時）

```sh
git merge main            # worktree は main に追従してから始める
bin/isu-task ready        # 着手可能なタスク
bin/isu-task on <id>      # 着手宣言（触るパスがタスク本文になければ追記する）
```

## 1〜2. 計測を読み、濃いところを読む

- 直近の `results/<id>/SUMMARY.md` を読み、担当する仮説の数字を確認する（`isu-analyze`）
- 該当ハンドラ・クエリ・設定を読む。定石は `isu-playbook`

## 3. 小さく実装

- 1施策 = 1コミット（後で原因を切り分けるため）。宣言したパスの外は触らない
- DB のスキーマ変更（インデックス追加など）は初期化 SQL（`webapp/sql/` の init 系）に入れる。initialize で毎回作られるようにし、手で ALTER しない
- サーバ設定は `hosts/<host>/etc/...` を編集する（デプロイで / に重なる）

## 4. ローカル確認

- ビルドが通る: `bin/isu-build`
- 型・静的検査（Go: `go vet ./...`、Rust: `cargo check`）
- レスポンス互換を壊しうる変更なら、ベンチの前に `bin/isu-golden check` を流す（`isu-bench` が自動で実行する）
- 永続化・追試・整合性に触る変更は `bin/isu-review diff` を通す（`isu-review` skill）

## 5. commit

```sh
git add -A && git commit -m "<何を>: <根拠の results id>"
```

## 6〜8. ベンチ

worker は deploy / bench をしない（委譲されたときを除く）。ターンの最後に次の1行で報告し、待機する:

```
BENCH READY: <branch>@<sha> — <仮説1行>（task <id>）
```

PR を作っておくと、`isu-bench` が結果を自動でコメントする: `gh pr create --base main --title "<仮説>" --body "task <id>"`

## 9. 結果を残す

- 効いた → main にマージする。`bin/isu-task off <id> -r "+N (results/<id>)"`
- 効かなかった / 下がった → **それも成果**。`bin/isu-task off <id> -r "効果なし: <数字> (results/<id>)。理由の推測: ..."`、`docs/RULES.md` の「外れた施策」に1行足す。ブランチは捨てるか revert
- 新しく分かった制約は `docs/RULES.md` に出典付きで書く

## 報告のしかた

- 「できました」は数字と一緒に（ビルドが通った / golden の差分なし / スコア +N）
- 詰まったら、何を試して何が起きたかを3行で書き、人間に聞く
