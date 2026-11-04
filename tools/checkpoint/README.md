# checkpoint

Claude Code の PostToolUse hook から区切りを通知する。
既定で kit-new が展開する。

```sh
tools/checkpoint/checkpoint start
tools/checkpoint/checkpoint status
tools/checkpoint/checkpoint hook
```

start は isu.env の ISU_END_AT を読む。
`--end-at <ISO時刻>` でも指定できる。
設計・CPU の上位・監査は開始から、前提の見直し・再起動試験・終盤は終了時刻から計算する。
記録は git ディレクトリに置く。
通知は1回だけ出す。
決めたことは docs/ に残し、自動 compact の後は作戦ボードから読み直す。

テストは `uv run --no-project python -m unittest discover -s tools/checkpoint/tests`。
