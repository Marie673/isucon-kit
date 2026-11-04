---
name: isu-rewrite
description: アプリを作り直すと決めた後の手順。仕様書、設計、実装、切り替え、追試対策、ロールバックまで。DB を外してメモリに持つ形のテンプレート（templates/inmemory-rust）の使い方も持つ。「作り直す」「全書き換え」「インメモリ化」と決めたときに使う。作り直すかどうかは、この skill では決めない。
---

# isu-rewrite — 作り直す手順

作り直すかどうか、どんな形にするかは、見たものから理屈で決める（`AGENTS.md` の考え方）。この skill は、決めた後の手順だけを持つ。

データの持ち方・台の使い方は問題ごとに違う。全データをメモリに載せる形や 1 台に集める形は、選択肢の 1 つにすぎない。大きさ・整合性などの制約は、形を変えれば消えることが多い。

## 1. 仕様書を作る（考察のみのターン。並列可）

`docs/spec/` に、エンドポイントごとに次を書く（出典: マニュアル / 初期実装の file:line / openapi）:
リクエスト、レスポンス（フィールド・順序・null の扱い・ステータス）、副作用、エラー条件、認証、ベンチが検証する点。
正はマニュアルと仕様書。初期実装のハンドラは、細かい振る舞いを確かめるために読む。推測で埋めない。

## 2. 設計を先に出す（実装しない）

- メモリ上の構造: 主キーのマップ + 各 API が引く順の索引（ユーザー別リスト・ソート済み構造など）
- Event の一覧（状態を変える操作すべて。ID・時刻はハンドラで決めてイベントに入れる）
- ロック粒度（まず1つの RwLock。競合が計測で見えたら分割）
- 非同期処理（通知・マッチング等）の扱いと再起動時の復旧
- 永続化・追試に触れる設計は、`bin/isu-review plan` で別の目を通せる

## 3. 実装

Rust でメモリに持つ形なら、テンプレートが使える（seed+delta の永続化つき）。別の言語・形なら、同じ考え方で書く。

```sh
cp -R templates/inmemory-rust webapp/rust-mem      # 例。ISU_SRC_DIR をここに
bin/isu-export-seed webapp/rust-mem/seed           # initialize 直後の初期状態で取る
```

- `src/model.rs`: `*Row`（seed の型）→ `State` → `Event` → `apply`
- `src/main.rs`: ルーティングを初期実装と同じ URI で。initialize は `store.reset()` + 仕様の応答
- 書き込み系は `store.update(|s| { 検証; Ok((結果, vec![イベント])) })` だけを使う（検証と反映がアトミックになる）
- 読み取りは `store.read()`。ロックを握ったまま重い処理・await をしない
- 画像などのファイルはメモリでなくディスク + nginx 配信
- テスト: `cargo test`（テンプレートの永続化テストは消さない）

## 4. 切り替え（単独ベンチ）

- isu.env: `ISU_LANG=rust`、`ISU_SRC_DIR=webapp/rust-mem`、`ISU_BIN_NAME=isu-mem`、`ISU_REMOTE_BIN=/home/isucon/webapp/rust-mem/isu-mem`、`ISU_SERVICES_<host>="isu-mem.service"`、`ISU_DEPLOY_PATHS="webapp/rust-mem/seed"`
- `hosts/<host>/etc/systemd/system/isu-mem.service` を置く（templates/inmemory-rust/isu-mem.service）。enable と起動は `bin/isu-deploy` に任せる。
- 旧アプリは `disable` するが消さない（ロールバック用）。nginx の upstream を新サーバのポートへ
- 台を減らすなら、使わない台のサービスを止める。DB を使わないなら ISU_DB_HOST を空に（slow log を取らない）
- `bin/isu-golden check` → `bin/isu-bench start --note "rewrite cutover"`

## 5. 追試対策（切り替えたら必ず）

- `bin/isu-reboot-test --yes` → `bin/isu-bench start --no-deploy --note "再起動後の確認"` でスコアが再現し、ベンチ前に書かれたデータが残ることを確認
- delta は `/home/isucon/state/`（`/tmp` は再起動で消える）
- SIGTERM で flush（テンプレートが実装済み）。`TimeoutStopSec` 内に終わること
- MySQL を使わなくなったら `disable` し、起動していないことを確認（無駄なメモリと I/O を消す）

## ロールバック

`git revert` → isu.env と unit を戻す → 旧サービスを enable → `bin/isu-deploy`。手順は切り替え前に docs/DECISIONS.md に書いておく。
