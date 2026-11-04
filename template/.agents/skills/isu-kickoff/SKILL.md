---
name: isu-kickoff
description: ISUCON 開幕（Phase I・最初の60分）の手順。EC2 の IP を isu.env に入れるところから、鍵配布・計測基盤・バックアップ・取り込み・ベースライン計測・タスク化までを順に進める。「開幕」「キックオフ」「環境構築」「初動」「最初のベンチ」と言われたら使う。メインエージェント 用。
---

# isu-kickoff — Phase I（10:00–11:00）

目標: 11:00 までに「壊しても戻せる床」「計測が1コマンドで回る状態」「ベースラインのスコア」「ルール考古学の初版」が揃っていること。
ここは用意したスクリプトを流すだけ。手作業で頑張るところではない。

## 0. 並行して始めること（開始直後）

- 人間: ポータルと当日マニュアルを開き、マニュアルの指示どおり EC2 を起動する（CloudFormation 等）
- メインエージェント: **`isu-rules` skill を subagent で並列に走らせる**（マニュアル・アプリケーションマニュアル・コードの読み込み）。環境構築を待たない

## 1. isu.env を埋める

- `ISU_IP_isu1..3`、`ISU_BOOT_USER` / `ISU_BOOT_KEY`（マニュアルが指定する入口）、`ISU_TEAM_GITHUB`
- 埋めたらコミット（全 worktree で共有するため）

## 2. 入れる・退避する・取り込む

```sh
bin/isu-keys        # isucon ユーザーに鍵を配る（以後は bin/isu-ssh isu1 で入れる）
bin/isu-setup       # alp / pt-query-digest / sysstat / perf、nginx の JSON ログ、slow log の準備
bin/isu-backup      # 初期状態の DB ダンプと /etc を各ホストの ~/backup に退避
bin/isu-inventory   # docs/inventory/ に OS・サービス・ポート・テーブル・スキーマを書き出す
```

`docs/inventory/*.md` を読んで isu.env を実態に合わせる:

- `ISU_SERVICES_<host>`: 稼働中のアプリのサービス名（`systemctl cat <svc>` で ExecStart を見る）
- `ISU_LANG` / `ISU_SRC_DIR` / `ISU_BIN_NAME` / `ISU_REMOTE_BIN`: 使う言語の実装と、ExecStart が指すパス
  - 初期実装の言語を切り替える場合（例: 既定が Go 以外 → Go）は、ここで systemd のサービスも切り替える
- `ISU_TARGET_HOST`（nginx の server_name）、`ISU_INIT_PATH` / `ISU_INIT_ARGS`（マニュアルの initialize 仕様）
- `ISU_DB_NAME`、`ISU_IMPORT_EXCLUDE`（使わない言語の実装を除外）

```sh
bin/isu-import      # webapp/ と hosts/<host>/etc/... を repo に取り込む
git add -A && git commit -m "import initial state"
```

## 3. 計測の準備

- `conf/alp-groups.txt`: ルーティング定義から ID を含むパスを正規表現にする（例: `^/api/chair/rides/[^/]+/status$`）
- `conf/golden.txt`: ベンチが叩く主要 API（ログイン → 認証付き GET）を5〜15本。`bin/isu-golden record` で記録してコミット
  - 毎回変わる値（ID・時刻）は isu.env の `ISU_GOLDEN_JQ` で消す
- Go なら pprof は不要（perf がサーバ全体を取る）。Go の関数単位で見たいときだけ `net/http/pprof` を :6060 で生やし、`ISU_PPROF_URL` を入れる

## 4. ベースライン

```sh
bin/isu-bench --note baseline   # 本番: 「ポータルで実行」の表示が出たら人間がポータルのボタンを押す
bin/isu-score <id> <score>      # ポータルの値を記録
```

ベンチが通らない（初期実装で fail）ときは、まず `results/<id>/` のアプリログとマニュアルを見る。スクリプトを疑う前に環境差分を疑う。

## 5. 共有脳を初期化する

- `docs/ARCH.md`: ホスト・役割・データの置き場所
- `docs/RULES.md`: isu-rules の結果（出典付き）
- `isu-analyze` skill でベースラインからタスクを起こす（`bin/isu-task add ...`）
- `docs/README.md`: フェーズ、攻略順、担当を書く。形を考える材料（isu-rules）への案内を書く

## 6. 陣形

メインエージェントは main の canonical checkout で動く。コンテキストで切れる仕事だけ subagent に渡す（skill `delegate`）。

## 完了の定義

- [ ] `bin/isu-bench` が1コマンドで回り、SUMMARY.md が出る
- [ ] ベースラインのスコアが SCOREBOARD にある
- [ ] RULES.md にスコア式と整合性要件が出典付きで書いてある
- [ ] タスクボードに根拠付きのタスクが5本以上ある
