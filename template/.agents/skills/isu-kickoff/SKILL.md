---
name: isu-kickoff
description: ISUCON 開幕（最初の60分）の手順。EC2 の IP を isu.env に入れるところから、検査・鍵配布・計測基盤・バックアップ・取り込み・ベースライン計測・ボトルネック表の作成までを順に進める。「開幕」「キックオフ」「環境構築」「初動」「最初のベンチ」「開始する」「開幕の準備」と言われたら使う。メインエージェント用。
---

# isu-kickoff — 開幕

目標: 最初の 1 時間で「壊しても戻せる床」「計測が1コマンドで回る状態」「ベースラインのスコア」「ルールの読み解きの初版」が揃っていること。
ここは用意したスクリプトを流すだけ。手作業で頑張るところではない。

## 0. 並行して始めること（開始直後）

- 人間: ポータルと当日マニュアルを開き、マニュアルの指示どおり EC2 を起動する（CloudFormation 等）。Claude Code の Chrome 拡張とポータルのサイトの許可は事前に済ませる
- メインエージェント: **`isu-rules` skill を subagent で並列に走らせる**（マニュアル・アプリケーションマニュアル・コードの読み込み）。環境構築を待たない

## 1. isu.env を埋める

- `ISU_IP_isu1..3`、`ISU_BOOT_USER` / `ISU_BOOT_KEY`（マニュアルが指定する入口）、`ISU_TEAM_GITHUB`
- `ISU_PORTAL_URL`、`ISU_END_AT`（競技終了）、`ISU_FREEZE_AT`（凍結）。`docs/RULES.md` の終盤の時刻表と揃える。サービス名は inventory の後で確かめる
- 埋めたらコミット（全 worktree で共有するため）

```sh
bin/isu-doctor --bootstrap   # この段階では ssh や計測の道具を要求しない
tools/checkpoint/checkpoint start   # 開始を記録する（--no-checkpoint で展開したときは無い）
```

## 2. 入れる・退避する・取り込む

```sh
bin/isu-keys        # isucon ユーザーに鍵を配る（以後は bin/isu-ssh isu1 <コマンド> で読める）
bin/isu-setup       # alp / pt-query-digest / sysstat / perf、nginx の JSON ログ、slow log の準備
bin/isu-backup initial  # 初期状態の DB ダンプと /etc を各ホストの ~/backup に退避
bin/isu-inventory   # docs/inventory/ に OS・サービス・ポート・テーブル・スキーマを書き出す
```

DB の種類とホストがまだ分からなければ `ISU_DB_KIND=none` で setup を流し、inventory の後に設定して setup とバックアップを流し直す。

`docs/inventory/*.md` を読んで isu.env を実態に合わせる:

- `ISU_SERVICES_<host>`: 稼働中のアプリのサービス名（`systemctl cat <svc>` で ExecStart を見る）
- `ISU_LANG` / `ISU_SRC_DIR` / `ISU_BIN_NAME` / `ISU_REMOTE_BIN`: 使う言語の実装と、ExecStart が指すパス
  - 初期実装の言語を切り替える場合（例: 既定が Go 以外 → Go）は、ここで systemd のサービスも切り替える
- `ISU_TARGET_HOST`（nginx の server_name）、`ISU_TARGET_SCHEME` / `ISU_TARGET_PORT`、`ISU_INIT_PATH` / `ISU_INIT_ARGS`（マニュアルの initialize 仕様）
- `ISU_DB_KIND`、`ISU_DB_CMD`、`ISU_DB_HOST`、`ISU_DB_NAME`、`ISU_APP_PORT`、`ISU_IMPORT_EXCLUDE`（使わない言語の実装を除外）

```sh
bin/isu-import      # webapp/ と hosts/<host>/etc/... を repo に取り込む
git add <取り込んだパス> && git commit -m "import initial state"   # 秘密を除く
bin/isu-doctor      # 接続・サービス・計測を検査する。未対応は警告と代わりの手順を読む
```

## 3. 計測の準備

- `conf/alp-groups.txt`: ルーティング定義から ID を含むパスを正規表現にする（例: `^/api/items/[^/]+/status$`）
- `conf/golden.txt`: ベンチが叩く主要 API（ログイン → 認証付き GET）を5〜15本。`bin/isu-golden record` で記録してコミット
  - 毎回変わる値（ID・時刻）は isu.env の `ISU_GOLDEN_JQ` で消す
- Go なら pprof は不要（perf がサーバ全体を取る）。Go の関数単位で見たいときだけ `net/http/pprof` を :6060 で生やし、`ISU_PPROF_URL` を入れる

## 4. ベースライン

skill `portal-bench` で最初のベンチを自分で流す。動線を読む初回は `--capture` を付ける。

```sh
bin/isu-bench start --note baseline --label baseline --capture
# Chrome の道具でポータルの実行のボタンを押し、job のページを終わりまで読む
bin/isu-bench finish <id> --score <点> --pass --job <job> --portal-file <本文のファイル>
```

ベンチが通らない（初期実装で fail）ときは、まず `results/<id>/` のアプリログとマニュアルを見る。スクリプトを疑う前に環境差分を疑う。

## 5. 文書を初期化する

- `docs/ARCH.md`: ホスト・役割・データの置き場所
- `docs/RULES.md`: isu-rules の結果（出典付き）と終盤の時刻表
- `isu-analyze` skill でベースラインから作戦ボードのボトルネック表の行を起こす
- `docs/README.md`: 判断の数字と今のボトルネックを書く。形を考える材料（isu-rules）への案内を書く
- skill `app-profile`・`score-model`・`capacity-plan` で数字を集め、判断を「見たもの → 考え → 決めたこと」で残し、skill `main-loop` へ進む

## 6. 陣形

メインエージェントは main の canonical checkout で動く。コンテキストで切れる仕事だけ subagent に渡す（skill `delegate`）。

## 完了の定義

- [ ] `bin/isu-bench start` と `finish` が回り、SUMMARY.md が出る
- [ ] ベースラインのスコアが SCOREBOARD にある
- [ ] RULES.md にスコア式と整合性要件が出典付きで書いてある
- [ ] 作戦ボードのボトルネック表に根拠付きの行が5本以上ある

Chrome の道具が無い場合は skill `portal-bench` の代わりの手順を使う。
