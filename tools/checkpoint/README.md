# checkpoint

決まった区切りに、メインエージェントのコンテキストへ「監査・見直し」の促しを差し込む Claude Code の hook。skill に書いた決まり（30 分更新なしで数え直す、二峰を疑う、判断の前提を確かめる）は、メインエージェントが自分で引かないことがある。時刻で必ず割り込み、監査役の subagent（skill `audit`）を出させる。

```sh
tools/checkpoint/checkpoint start 10:00   # 本番。練習は引数なし（今が T）
tools/checkpoint/checkpoint status
```

- **標準では入れない。** メインエージェントを決まりで縛らず、考え方で動かすため。入れるときは `bin/kit-new <dir> --with-checkpoint`。`.claude/settings.json` の `hooks.PostToolUse` に `tools/checkpoint/checkpoint hook` を足し、skill `audit`（`audit/SKILL.md`）も入れる。道具を 1 回使うたびに呼ばれ、期限の来た区切りを 1 回だけ出す。
- 区切りは `checkpoints.tsv`（開始からの分・ID・文）。時刻のほかに、**情報の合図**も 1 つ持つ: 作戦ボード（`docs/README.md` の判断の数字）を最後にコミットしてから `results/` にベンチが 3 本以上たまったら、表の更新と撤回の条件の確かめを促す（直すたびに数え直す）。練習で時間割を縮めるときは分を直す。
- 記録は `.git/checkpoint/`（コミットされない）。開始が未記録なら何もしない。
- 時刻で出すのは監査と見直しの促しだけ。構成の判断は、時刻ではなく情報（制約の表と最初のベンチ、前提が崩れる数字）で出す（skill `main-loop`）。
- Codex には hook が無い。Codex でメインエージェントを回すときは、ベンチの合間に `checkpoint status` を見て、期限済みの区切りを自分で拾う。
