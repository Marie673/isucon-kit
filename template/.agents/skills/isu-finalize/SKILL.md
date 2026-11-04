---
name: isu-finalize
description: ISUCON の終盤（Phase III 再収束 16:30〜 / Phase IV 凍結 17:15〜）の手順。大技の検証締め切り、再起動試験（順序ランダム）、ログ・計測の停止、最終ベンチ、提出前チェック。「終盤」「凍結」「再起動試験」「追試対策」「最終ベンチ」と言われたら使う。
---

# isu-finalize — 再収束と凍結

最悪のシナリオは「追試落ちで最終スコア 0」。ここだけは保守的に締める。

## Phase III 再収束（16:30–17:15）

1. 大技（書き換え・キャッシュ戦略・台数構成）の単独ベンチはここまでに終える。伸びしろが残っていても深追いしない
2. 残す変更 / 捨てる変更をメインエージェントが決め、docs/DECISIONS.md に残す。main に統合
3. **再起動試験**:
   ```sh
   bin/isu-reboot-test --yes      # 全台をランダムな順序・間隔で再起動し、サービスとヘルスチェックを確認
   bin/isu-bench --no-deploy --note "after reboot"
   ```
   - スコアが再起動前と近いこと（大きく下がっていたら、ウォームアップやキャッシュ再構築を疑う）
   - ベンチ前に書かれたデータが残っていること（ベンチが整合性で落ちないこと）
4. 起動順序に依存していないか:
   - アプリは DB が後から起きても接続をリトライする（systemd: `Restart=always` + `RestartSec=1`）
   - 必要なサービスは `systemctl is-enabled` が enabled、不要なサービスは disabled
   - 手で起動したプロセス・手で変えた設定がない（すべて repo → deploy 経由か）

## Phase IV 凍結（17:15–18:00）

1. **新規変更を全面停止**。エージェントには「PR だけ。deploy / bench はしない」を宣言する（メインエージェント以外）
2. 計測を止める:
   ```sh
   bin/isu-logs off               # nginx のアクセスログ（repo を書き換え → commit → deploy）
   ```
   - アプリのリクエストログ（logger ミドルウェア）や pprof を外す（コードの変更 → commit）
   - MySQL の slow log は計測中しか ON にならないので操作不要。`SELECT @@slow_query_log` が 0 であることだけ確認
   - perf / vmstat は isu-measure stop で止まっている。`pgrep perf` で残っていないことを確認
3. `bin/isu-deploy` → もう一度 `bin/isu-reboot-test --yes`
4. 最終ベンチをポータルから 2〜3 回。スコアが安定していることを確認
5. 18:00 の時点のサーバ状態が審査対象。終了直前にデプロイしない

## 提出前チェックリスト

- [ ] `bin/isu-reboot-test --yes` が通る（最後の deploy の後で）
- [ ] 最終ベンチが pass し、スコアが安定
- [ ] アクセスログ・アプリログ・プロファイラが止まっている
- [ ] 不要なサービスが disabled、必要なサービスが enabled
- [ ] repo の main = サーバの状態（`bin/isu-ssh <host> cat ~/.isu-deployed` の sha が main の HEAD）
- [ ] ポータルに必要事項（あれば）を入力した
