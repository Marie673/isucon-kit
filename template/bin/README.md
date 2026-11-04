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
時間は300秒、サイズは16MBを2ファイルまでとする。
