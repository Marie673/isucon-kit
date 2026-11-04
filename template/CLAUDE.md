@AGENTS.md

## Claude Code 固有

- ベンチの実行と結果の確認は Claude in Chrome で行う（skill `portal-bench`）
- 自動 memory はこのプロジェクトでは無効（`.claude/settings.json` の `autoMemoryEnabled: false`）。memory に書きたくなったら `docs/` に書く
- 並列にできる調査（マニュアルの読み込み、エンドポイントごとのコード読み、スロークエリごとの EXPLAIN）は subagent に分けてよい。独立した実装も分けてよい（skill `delegate`）
- 長い待ち（ベンチ・他エージェントの作業）に入るときは `herdr-wait set "<何を待っているか>"`、解けたら `herdr-wait clear`
