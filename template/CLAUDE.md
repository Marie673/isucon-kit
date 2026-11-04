@AGENTS.md

## Claude Code 固有

- 自動 memory はこのプロジェクトでは無効（`.claude/settings.json` の `autoMemoryEnabled: false`）。memory に書きたくなったら `docs/` に書く
- 並列にできる調査（マニュアルの読み込み、エンドポイントごとのコード読み、スロークエリごとの EXPLAIN）は subagent に分けてよい。実装は分けない
- 長い待ち（ベンチ・他エージェントの作業）に入るときは `herdr-wait set "<何を待っているか>"`、解けたら `herdr-wait clear`（isu-bench は自動でやる）
