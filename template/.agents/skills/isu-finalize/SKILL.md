---
name: isu-finalize
description: ISUCON の終盤（再収束〜凍結）の手順。大技の検証締め切り、再起動試験（全サーバー同時と順序ランダム）、ログ・計測の停止、最終ベンチ、提出前チェック。「終盤」「凍結」「再起動試験」「追試対策」「最終ベンチ」「提出前」「最後の確認」と言われたら使う。
---

# isu-finalize — 再収束と凍結

最悪のシナリオは「追試落ちで最終スコア 0」。ここだけは保守的に締める。
時刻は `isu.env`（`ISU_END_AT`・`ISU_FREEZE_AT`）と `docs/RULES.md` の終盤の時刻表から取る。凍結は時刻表の時刻に行い、前倒ししない。凍結の前までは、ボトルネックを取り続ける。

## 再収束（凍結の前）

1. 大技（書き換え・キャッシュ戦略・台数構成）の単独ベンチはここまでに終える。通らなければ戻せる版で終える
2. 残す変更 / 捨てる変更をメインエージェントが決め、docs/DECISIONS.md に残す。main に統合
3. 試験中のフラグを採るか消すか決める。`tools/flags/flags --ledger docs/flags.md lint --code <ソース> --env hosts` を通す
4. **再起動試験**:
   ```sh
   bin/isu-reboot-test --yes      # 全サーバーを同時に再起動し、サービスとヘルスチェックを確認
   bin/isu-reboot-test --yes --random   # 順序と間隔をばらす試験
   bin/isu-bench start --no-deploy --note "after reboot"   # 続けて skill portal-bench で流す
   ```
   - スコアが再起動前と近いこと（大きく下がっていたら、ウォームアップやキャッシュ再構築を疑う）
   - ベンチ前に書かれたデータが残っていること（ベンチが整合性で落ちないこと）
   - 永続化を弱めていれば、`docs/DECISIONS.md` の表に再起動試験の結果と results の id を書く
5. 起動順序に依存していないか:
   - アプリは DB が後から起きても接続をリトライする（systemd: `Restart=always` + `RestartSec=1`）
   - 必要なサービスは `systemctl is-enabled` が enabled、不要なサービスは disabled
   - 手で起動したプロセス・手で変えた設定がない（すべて repo → deploy 経由か）

## 凍結

1. **新規変更を止める**。subagent・別のエージェントには「PR だけ。deploy / bench はしない」を宣言する。凍結のバージョンを決めた後も待たず、ブランチで差を詰め、間に合わなければ捨てる
2. 計測を止める:
   ```sh
   bin/isu-logs off               # nginx のアクセスログ（repo を書き換え → commit → deploy）
   ```
   - アプリのリクエストログ（logger ミドルウェア）や pprof を外す（コードの変更 → commit）
   - MySQL の slow log は計測中しか ON にならないので操作不要
3. `bin/isu-deploy` → もう一度 `bin/isu-reboot-test --yes`。凍結の後に再反映が要るなら、理由を記録して `bin/isu-deploy --after-freeze` を明示する
4. 最終ベンチは skill `portal-bench` の `bin/isu-bench start --no-deploy --no-measure` で、同じコードの版を測る
5. 最終のスコアが最後の 1 本で決まる回は、本数を決めずに止め時を期待値で決める。`tools/stop-rule/stop-rule --scores-file results/final-scores.txt --remaining <残り本数> --last <直近の点>`。fail は 0 点として一覧に入れる
6. 終了の時点のサーバ状態が審査対象。終了直前にデプロイしない。終了の 60 秒前までに長い監視と接続を終える（`bin/isu-ssh` などは 60 秒前から拒否する）

## 提出前チェックリスト

`bin/isu-doctor --final` が、反映した sha・enable・ログと計測の停止・ディスク・永続化の記録を確かめる。結果のコミットで動いた HEAD とコードの sha を取り違えない。

- [ ] `bin/isu-reboot-test --yes` が通る（最後の deploy の後で）
- [ ] 最終ベンチが pass している
- [ ] アクセスログ・アプリログ・プロファイラが止まっている
- [ ] 不要なサービスが disabled、必要なサービスが enabled
- [ ] `bin/isu-doctor --final` が通る
- [ ] ポータルに必要事項（あれば）を入力した
- [ ] 最終結果を作戦ボードに書いた

Chrome の道具が無い場合は skill `portal-bench` の代わりの手順を使う。
