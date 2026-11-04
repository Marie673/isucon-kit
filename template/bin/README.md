# 競技の道具

共通処理は isu_runtime.py に置く。
Python の標準ライブラリと uv で実行する。
既存のビルド・導入・取り込みは Bash を使う。
テストはキットの `bin/kit-test` でまとめて行う。

`isu-bench start` は計測開始までで戻る。
Chrome の job の結果を `finish --score N --pass|--fail` に渡す。
`abort --reason TEXT` は計測を止めてロックを解放する。
`wait` は合図だけにする。
`run` は練習のコマンド、または明示した `--wait-traffic` のために使う。

ロックの入れ子には `ISU_LOCK_TOKEN` が必要。
持ち主の名前が同じだけでは通さない。
手で acquire したときは出力されたトークンを同じ処理にだけ渡す。
`bench-current` はトークンと id を git の共通ディレクトリへ保存する。
コミット失敗時の記録は同じ場所の commit-pending.json に残る。
マージが終わったら指定したパスだけをコミットする。

`ISU_DB_KIND` は mysql・postgresql・sqlite・none。
`ISU_DB_CMD` は `sudo`、`sudo -u postgres`、`sudo docker exec db` などの接頭辞。
PostgreSQL は pg_stat_statements を事前に有効にする。
計測開始時に reset し、停止時に上位を取る。
sqlite は DB ファイルを ISU_DB_PATH に置く。

`ISU_TARGET_SCHEME` と `ISU_TARGET_PORT` を golden・反映・再起動試験で共有する。
`ISU_END_AT` の60秒前から ssh/scp を拒否する。
`ISU_FREEZE_AT` の後は反映に `--after-freeze` が要る。
`ISU_REMOTE_HOME` と `ISU_LOCK_DIR` は偽 ssh のテストでも差し替えられる。

生の access.log・http.jsonl・pcap はコミットしない。
capture はアプリの平文ポートだけで使う。
時間は ISU_CAPTURE_SECS（既定300秒）、サイズは ISU_CAPTURE_MB（既定1024MiB）までにする。
上限で停止し、先頭を上書きしない。pcap は scp で別に回収する。
サイズ上限の末尾の不完全なパケットは解析で除く。

初回は `isu-doctor --bootstrap` で IP・時刻・ポータル・起動用の鍵だけを検査する。
鍵配布・導入・inventory・設定・import の後で通常の doctor を使う。
`isu-ssh` はコマンドを省略できない。例は `bin/isu-ssh isu1 uptime`。
`isu-intake --allow-static <パス>` は、Node の実装などを明示して検査を通すために使う。
宣言したパスと永続化の検査は省略しない。
ロック消失時も `isu-bench abort` はホストごとに停止を試す。
停止できないホストは記録し、他の処理のロックを解放しない。
force-release の後は、表示された `isu-measure stop <id>` で残った計測を片付ける。

vmstat と pidstat は UTC で採り、pidstat は epoch 秒も使う。
過去のログはホストごとの tz-offset で変換する。
アクセスログの offset を使う表示と、CPU の秒を取り違えない。
