# レビュー指摘への対応（REV 4）

作業ブランチは fix/kit-review。
push と PR 作成は行わない。
動作確認は偽の ssh と合成した通信を使う。

## 1. ベンチの流れ

start・finish・abort・wait・run に分けた。
start はトークンと id を bench-current に残して戻る。
終了の正は Chrome で読んだ job にする。
finish で本文・合否・label・スコア・窓・要約を残す。
--no-measure は計測を起動しない。

検証: BinTests.test_portal_bench_roundtrip_noise_and_code_sha、test_no_measure_skips_profilers_and_abort、test_no_log_window_is_unrecorded、test_run_requires_explicit_mode_and_preserves_current。
待ちの判定は RuntimeTests.test_wait_padded_wc_does_not_write_result。
Chrome 自体は接続していない。
手順は portal-bench に置いた。

## 2. 道具のバグ

| 番号 | 直したこと | テスト |
|---|---|---|
| 1 | Bash の GROUPS を廃止。正規表現を引数で渡す | BinTests.test_comment_only_groups_and_alp_group_argument |
| 2 | コメントだけの設定を空のリストとして扱う | 同上 |
| 3 | diff と指示を codex exec -s read-only の標準入力へ渡す。stderr を表示する | BinTests.test_review_stdin_and_visible_failure |
| 4 | 手引きと strategy を展開する。リンクを変換する。playbook は入口だけにする | BinTests.test_installed_document_links_and_tool_paths_exist |
| 5 | DB の種類と接頭辞を共有。PostgreSQL は reset 後の pg_stat_statements。sqlite と none は MySQL 計測を呼ばない。空ダンプを拒否する | BinTests.test_postgresql_measurement_and_docker_prefix、test_postgresql_setup_preserves_preloads_and_seed、test_backup_rejects_empty_or_failed_dump |
| 6 | scheme と port を共通化する | BinTests.test_health_http_and_5xx_failure_4xx_warning、test_golden_uses_shared_http_port、RuntimeTests.test_iso_offsets_and_http_defaults |
| 7 | 起動失敗を失敗として返し、journal を出す。ヘルスチェックは再試行する | BinTests.test_restart_failure_fails_deploy_and_shows_journal、test_health_http_and_5xx_failure_4xx_warning |
| 8 | ホストごとの manifest にある旧ファイルだけを退避する | BinTests.test_manifest_removes_only_previous_files_and_common_restart |
| 9 | common を重ねた内容のハッシュで DB の再起動を決める | 同上 |
| 10 | トークンが一致する入れ子だけを許す | BinTests.test_token_prevents_same_owner_nested_deploy |
| 11 | docs と results を除いたコードの commit を使う。no-deploy はサーバーの記録を読む。dirty を印にする | BinTests.test_result_commit_does_not_change_code_sha、test_portal_bench_roundtrip_noise_and_code_sha |
| 12 | Welch と対応ありの t による平均の差の95%信頼区間。1本単位の幅は参考にする | IntervalTests.test_eight_runs_detect_twelve_point_shift、test_paired_difference_and_mismatched_counts |
| 13 | 空の IP を飛ばす。既定は同時の再起動。random だけ順序と間隔を変える | BinTests.test_reboot_all_hosts_skips_empty_last_ip |
| 14 | setup の GNU sed はリンクをたどる。ローカルの sed -i と tar の OS 固有のオプションは Python へ置き換える | BinTests.test_setup_json_log_preserves_symlink、test_portal_bench_roundtrip_noise_and_code_sha |
| 15 | wc の空白を整数として読む。Python のループに移し、stdin の無い ssh に -n を付ける | RuntimeTests.test_wait_padded_wc_does_not_write_result、test_remote_ssh_has_n_without_script_stdin |
| 16 | コミット失敗を警告する。マージ中も記録を残し、再試行できる | BinTests.test_commit_failure_warns_and_preserves_pending、test_merge_pending_is_retryable |
| 17 | group_concat の上限を増やす。SQL 成功と JSON の確認後に出力を置き換える | BinTests.test_export_seed_limit_and_failure_no_empty_file |
| 18 | no-measure を用意。ログ off を doctor が警告する | BinTests.test_no_measure_skips_profilers_and_abort、test_missing_env_stops_doctor_and_logs_off_warns |
| 19 | docs/flags.md を優先する。期限を2時間にし、probe も数える | RevisionTests.test_default_ledger_and_probe_expiry |
| 20 | checkpoint と stop-rule の README と tests を揃える。kit-test を入口にする | CheckpointTests、StopRuleTests、bin/kit-test |
| 21 | 既定は既存を守る。update は管理したファイルだけを更新する。checkpoint は既定で入れる | BinTests.test_kit_new_preserves_records_update_refreshes_managed、test_no_checkpoint_option_and_default_preserve_settings |

## 3. 道具が守る条件

時刻・checkout・HOLD・書き込み・凍結を道具で検査する。
ControlPersist は60秒にする。
watch は短い接続を繰り返す。
ssh と scp の実行時間も接続停止時刻で切る。
isu-task と dots を廃止し、intake と作戦ボードに集めた。
永続化は名前だけでなく、理由・危険・撤回の条件のある表を要求する。
最終検査は再起動試験の成功も要求する。
Claude Code の allow と deny を揃えた。

検証: BinTests.test_deadline_and_freeze、test_main_checkout_only_and_probe_exception、test_hold_and_write_ssh_guards、test_intake_rejects_outside_static_and_durability。
RuntimeTests.test_deadline_last_minute_and_scp、RevisionTests.test_durability_needs_complete_table_and_final_reboot。

## 4. 新しい道具

capture は平文 HTTP の要求と応答を対にする。
リンク種別0・1・113・276、再送・逆順、gzip、chunked を合成 pcap で確かめる。
要約は認証情報をハッシュにする。
採取の時間とサイズを制限する。
生のログと pcap は git に入れない。
doctor は埋め忘れ・入口・サービス・DB・計測・最終状態を検査する。
nginx 以外は止めて代わりの手順を出す。

検証: CaptureTests、BinTests.test_capture_is_bounded_and_not_committed、test_setup_json_log_preserves_symlink、test_missing_env_stops_doctor_and_logs_off_warns、test_final_doctor_checks_deployment_and_logs。

## 5. 偽の ssh の結合テスト

tests/test_bin.py と tests/fake_remote.py を追加した。
展開から結果の読み取り、反映の失敗、取り込み、境界の拒否までを検査する。
道具の単体テストと同じ kit-test で実行する。
テストの説明は tests/README.md にある。

## 6. 文書と skill

portal-bench を入口に、kickoff・main-loop・finalize を揃えた。
時刻は isu.env と RULES の終盤の表へ集めた。
判断は「見たもの → 考え → 決めたこと」の3段にした。
自動 compact の後は作戦ボードから読み直す。
特定の回の値と例をテンプレートから外した。
AGENTS の剪定基準と、通しの評価の記録形式を追加した。
公開された過去問の例は手引きの中にだけ残す。

検証: BinTests.test_installed_document_links_and_tool_paths_exist、test_no_checkpoint_option_and_default_preserve_settings。

## 実装上の選択

既存の Bash の主処理は Python の共通処理へ移した。
標準ライブラリで manifest、時刻、メタデータ、排他を共有するため。
ビルド・導入・取り込み・golden の既存の Bash は必要な修正を入れて残した。
計測停止に失敗した場合はロックを残す。
再試行せずに別のベンチへ進ませないため。
対応ありの比較は、失格や未記録で対を崩さないよう、使う対を ids= で明示する。

未対応は capture の TLS・HTTP/2・pcapng・IP の断片化と nginx 以外の自動導入。
要求された平文 HTTP/1.1 と nginx の範囲は実装した。
外の環境の実運用はこの作業では行っていない。
