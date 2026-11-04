# 15 終盤と追試

**当日のマニュアル・レギュレーションが常に優先する。** 最終スコアの決まり方（最後の走行か、最高か、再起動後か）、追試の手順と閾値、競技終了後の接続の可否、変えてはいけないファイルは、回ごとに違う。この章は、その回のルールで禁止・失格になる手（永続化を捨てる、`fsync` を切る、外部へ委譲する、静的ファイルを変える、ベンチを見分ける）を避けるための、確認の手引き。

## この章で扱う範囲

- 終盤の時刻表（凍結・再起動試験・最終ベンチ・接続の終了）。
- ログ・計測・トレースを切る。
- 再起動試験（全台同時の再起動、サービスの `enable`、起動順と依存、DB より先にアプリが起動したときの再試行）。
- 永続化の確認（メモリ・tmpfs・キャッシュ・Redis）。
- 初期化の時間と再現性。
- 最終ベンチの選び方（最後の走行が最終の点になる回、ぶれ、冷えた状態）。
- コードの凍結。
- 競技終了後に接続しない。
- よくある事故（再起動でアプリが上がらない、メモリだけのデータ、cron・不要なサービス、ディスクの残量）。

扱わないもの: スコアの式と追試の閾値の読み方（[14 章](14-benchmarker-and-scoring.md)）、各サービスの設定の中身（[02 章](02-os-kernel.md)・[04 章](04-mysql.md)・[05 章](05-postgresql.md)・[08 章](08-cache-and-inmemory.md)）。

## 最初に見るもの（当日の 5 分チェックリスト）

終盤は、**点を上げるより、点を失わない**ことが仕事になる。過去の回では、再起動後のチェックの失敗だけで、上位のチームが多数失格になった（ISUCON10 本選は 7 チーム。ISUCON14 は参考スコアのある FAIL が約 150 チーム）。

| # | 確かめること | どうやって |
| --- | --- | --- |
| 1 | 最終スコアがどの走行の点か | マニュアルの「最終スコア」。最後の走行か、再起動後の走行か（[14 章](14-benchmarker-and-scoring.md)の表） |
| 2 | 競技終了時刻と、接続を止める時刻 | 競技終了後は、主催者の告知まで接続・操作しない |
| 3 | 全サービスが `enable` されているか（全台） | `systemctl is-enabled`、`systemctl list-unit-files` |
| 4 | 手作業なしで、再起動後にアプリが上がるか | 全台を同時に再起動して、何も触らずにベンチとブラウザで確かめる |
| 5 | ベンチ中に書いたデータが残るか | 再起動の前後で、件数・最大 ID を比べる |
| 6 | ログ・計測・トレースを切ったか | 切った後に、ベンチを 1 本流す |
| 7 | 変更禁止のファイル・ユーザー・ポートを触っていないか | マニュアルの「変更してはいけないもの」 |
| 8 | 計測用の台・ツール・外向きの接続が残っていないか | 片付ける。環境確認（envcheck）を流す |
| 9 | ディスクの残量と、ログの肥大 | `df -h`、`journalctl --disk-usage` |
| 10 | 最後の走行に使う構成が、1 つの commit に固定されているか | 戻せる commit を先に決める |

- 参加者の例では、再起動試験を競技の終了の約 1 時間前にリマインドしたもの（takonomura、実例）、初動から再起動までを練習で通したものがある。

---

## 1. 終盤の進め方

### 終盤の時刻表を、競技の前に決める

- **効く条件**: 終盤に、再起動試験・最終ベンチ・片付けの時間が足りなくなる。大きな変更を入れて、再起動試験が間に合わない。
- **やり方**: 競技終了の時刻から逆算して、次を決めておく。
  | 時刻（終了 18:00 の例） | やること |
  | --- | --- |
  | 終了の 3 時間前 | 大きな書き換えを始めない（凍結に間に合わない変更は、始めない） |
  | 終了の 2 時間前 | 再起動試験を 1 回（全台同時）。壊れていれば直す時間が残る |
  | 終了の 40〜50 分前 | コード凍結。以後は revert だけ |
  | 終了の 30〜10 分前 | 最終ベンチを数本。最後の走行が最終の点になる回では、最後は必ず通った構成で終える |
  | 終了の 5 分前 | 常駐の計測・監視・自動ジョブを止める |
  | 終了時刻 | 接続を終了する |
- **確かめ方**: 時刻をリマインドに入れる。終了の約 1 時間前に再起動試験のリマインドを流し、複数台の再起動の順と直後の動作を確認した例がある。
- **出典**: （最重要: 時間制限のある競技）。

### 改善から安定化へ、重点を移す

- **効く条件**: 小さな改善が続くだけで、点が伸びなくなった。不確かな挙動が残っている。
- **やり方**: 改善をほぼ止めて、追試の不確かさ（再起動、永続化、整合性、ぶれ）に時間を回す。ISUCON11 予選のあるチームは、15 時過ぎに改善をほぼ止め、不確かな挙動の確認と追試対策に移った。ISUCON14 の参加者（iceman）は、去年（ISUCON13）FAIL で終わったので、終盤は大きくコードをいじらず、ランダムで落ちないことと再起動試験を、何度もベンチを回して確かめた。
- **確かめ方**: ベンチを複数回流して、FAIL が出ないか見る。
- **危険**: 原文の HTTP/2 の不確かな挙動は、後に運営側の問題と分かった。現象を記録して、独断でルールを変えない。
- **出典**: [iceman（うどんきれいなGo）の ISUCON14 参加記](https://zenn.dev/iceman/articles/1780deef84eda8)。

### コードの凍結と、最終候補の固定

- **効く条件**: 終盤に、小さな変更を重ねて、最後に壊す。最後の走行の構成が分からなくなる。
- **やり方**:
  - 凍結の時刻以降は、main に入れない。入れてよいのは revert だけ。
  - コードだけでなく、ミドルウェアの設定（nginx・DB・systemd・sysctl）も、同じデプロイで入れる。サーバー上の手作業の変更を残さない。
- **確かめ方**: サーバー上の設定と、リポジトリの内容の差分が空か見る（反映時に差分を検出したら自動処理を止める手もある。実例）。
- **危険**: 凍結後に気づいた不具合を、直すか戻すか。直すときは、再起動試験からやり直す時間があるか。

---

## 2. ログ・計測・トレースを切る

### 計測の仕込みを、終盤に 1 か所で切れるようにしておく

- **効く条件**: 計測（アクセスログ・スロークエリログ・pprof・OTel・APM）が、最終ベンチの点を下げている。切り忘れが怖い。
- **やり方**: 切る対象を一覧にして、**切る手順を 1 か所（スクリプトか、デプロイの設定）にまとめる**。
  | 対象 | 切り方の例 |
  | --- | --- |
  | nginx のアクセスログ | `access_log off;` か、`buffer=` と `flush=` で軽くする（[01 章](01-measurement.md)） |
  | MySQL のスロークエリログ | `SET GLOBAL slow_query_log = 'OFF';` と、my.cnf の設定を直す |
  | PostgreSQL | `pg_stat_statements` はそのまま残せる（軽い）。`auto_explain`・`log_min_duration_statement` は戻す |
  | アプリのリクエストログ | Go の echo・chi のリクエストロガーなどを止める（定石集に「終盤に止める」） |
  | pprof・fgprof のエンドポイント | ビルドの設定・環境変数で無効にし、ポートも閉じる |
  | OTel のトレース | 採取率を 0 にする、または exporter を無くす（`OTEL_TRACES_EXPORTER=none`）。Rust は feature で外す |
  | 常駐の監視（netdata・exporter） | 止める |
  - ISUCON11 予選のあるチームは、`enable_monitoring` の `require` を外すだけで、計装（APM・例外収集・プロファイラ）を全部止められる構成にした。Rust では feature や設定でそれを行う。
- **危険**:
  - 切るのも変更。ログのレベルを変えて、エラーが握りつぶされることがある。障害ログまで失わない。
  - 切った後の最初のベンチが、最終の点になることがある。切る作業は、凍結より前に済ませる。
  - 切る前に、必要なログ・プロファイル・計測データを保存する。計測用のインスタンスを、バックアップなしで消して、点数とメトリクスを失った例がある（ISUCON14）。
  - 出題者の講評も、高負荷時のログ出力の停止を挙げる（ISUCON10 予選）。
- **出典**: [ISUCON10 予選の講評](https://isucon.net/archives/55025156.html)、（OTel の on/off）、（ログ出力は終盤に止める）。

### 走行中にビルド・再起動・DDL・計測の切り替えをしない

- **効く条件**: ベンチが原因不明で FAIL・減点になる。
- **やり方**: ベンチが走っている間は、競技サーバーを触らない。
  - ISUCON14 の参加者の実例では、ベンチ前後のビルド・再起動・ログの退避を Ansible にまとめ、ベンチ中にそれらを走らせないようにした。
- **確かめ方**: ポータルの走行の状態（待機中・実行中）を、デプロイの前に確かめる。デプロイとベンチを同じ 1 つのコマンドに入れる。
- **危険**: 特になし（守るだけ）。

---

## 3. 計測用の台・ツール・環境の片付け

### 環境確認（envcheck）と、運営用のファイル・ユーザー・ポート

- **効く条件**: 競技終了時に、環境確認や追試が通らない。計測用の台や、実験用のインスタンスを立てた。
- **やり方**: マニュアルの「変更してはいけないもの」を、最終の前に読み直す。回に共通する項目は次のとおり。
  - 運営用のユーザー（`isuadmin` など）と、`isucon` 以外のユーザーの削除・公開鍵の削除。
  - 環境確認（envcheck）のファイルと、運営のサービス。
  - セキュリティグループの SSH（22）と HTTPS（443）。ISUCON12 予選では、「セキュリティグループの TCP:8080 が開いていた」ことで、追試で失格になったチームがある。
  - インスタンスタイプの変更など、テンプレートに沿わない変更。
  - 競技終了時点で、ポータルのサーバー情報に複数のスタックが混在していること。
- **確かめ方**: 各台で `sudo /usr/local/bin/envcheck`のような環境確認を流す。ポータルのサーバー一覧を見る。
- **危険**:
  - ISUCON14 の参加者は、環境確認で失格になった原因を、**pprotein を入れたサーバーを、競技サーバーと同じ VPC に立てっぱなしにしていたこと**だと推測している（運営の説明ではない）。計測用・実験用の台は、競技の最後に止めて片付ける。
  - ただし、計測のデータを消す前に、ログと実装の状態を保存する。
  - 追加で立てた台・外向きの接続・ポートの開放は、ルール（外部資源の利用）に合うかを、最終の前に点検する。
- **出典**: [ISUCON12 予選の結果](https://isucon.net/archives/56838181.html)（追試の失格 5 チームの理由）、[ISUCON13 の当日マニュアル](https://github.com/isucon/isucon13/blob/main/docs/cautionary_note.md)、[ISUCON14 のマニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)、[ponyo877 の ISUCON14 参加記](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)。

### 運営の追加の告知を、最終計測の前に読み直す

- **効く条件**: 競技中に、運営から失格条件の追加や補足の告知が出る。
- **確かめ方**: 告知ごとに、台とコードへの影響を点検して、結果を記録する。
- **危険**: 告知で示された検証以外の接続を、ルーターなど運営のサービスへ試さない。

---

## 4. 再起動試験

### 全台を同時に再起動して、手作業なしで動くか確かめる

- **効く条件**: 追試で、全サーバーを再起動して計測する回。
- **やり方**:
  1. 競技の終了の 2 時間前までに、全台を同時に再起動する（追試は全台をいっせいに再起動する）。
     ```sh
     sudo systemctl reboot            # 競技時間中の試験はこれでよい
     ```
  2. **何も触らずに**、サービスが自動で上がるのを待つ。`sudo systemctl is-system-running --wait`（`running` なら正常、`degraded` なら失敗した unit がある）、`systemctl list-units --failed`。
  3. ブラウザで画面を開き、ベンチを 1 本流す。
  4. 手作業（`systemctl start`・`restart`・手動のコマンド）を挟んだら、成功にしない。
  - 運営の再起動の手順は、回によって違う。ISUCON10 本選・ISUCON11 本選は、運営が SSH で `sudo systemctl poweroff` してから、EC2 の起動 API で起動する（ISUCON11 本選は、すでにシャットダウンしていて `poweroff` を実行できないと失格）。ISUCON9 本選・ISUCON10 本選は、全チームの再起動後に 10 分以上待ってから計測する。**競技時間中に自分で試すときは `sudo systemctl reboot`**（運営は、電源断の起動依頼にはベストエフォートでしか対応できないと書いている）。
- **危険**:
  - 運営の追試は、再起動の後に `POST /initialize` を要求しない回がある（ISUCON10 本選のマニュアル）。**ベンチ中に書いたデータが、initialize なしで残っている**必要がある。
  - ISUCON10 本選では、再起動後のチェックに失敗して失格になったのが 7 チーム（上位のチームを含む）。ISUCON14 は、再起動の NG・負荷走行後の再起動の NG・データ保持の NG・目視チェックの NG を別々に数えた。
  - 1 台ずつ再起動して確かめると、台の間の起動順（DB の台が先か後か）の問題が見えない。**全台同時**でも試す。
- **出典**: [ISUCON10 本選のマニュアル](https://github.com/isucon/isucon10-final/blob/master/docs/manual.md)、[ISUCON11 本選のマニュアル](https://github.com/isucon/isucon11-final/blob/main/docs/manual.md)、[ISUCON9 本選のマニュアル](https://github.com/isucon/isucon9-final/blob/master/docs/MANUAL.md)、[ISUCON14 の結果](https://isucon.net/archives/58837992.html)、[ISUCON10 本選の追試の発表](https://isucon.net/archives/55028786.html)、[systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)。

### サービスの enable と、不要なサービスの disable（全台）

- **効く条件**: 再起動後に、アプリや DB が自動で起動しない。役割を変えた台で、旧サービスが復活する。
- **やり方**:
  ```sh
  systemctl is-enabled <unit>            # enabled / disabled / masked
  systemctl list-unit-files --state=enabled | head -50
  systemctl list-units --failed
  systemctl is-system-running --wait     # 再起動の後に
  sudo systemctl enable --now <unit>     # enable だけでは今は起動しない。--now で同時に
  sudo systemctl disable --now <旧unit>  # 使わなくなったサービス
  ```
  - `enable` は `[Install]` の `WantedBy=` に従って、リンクを作る。**今は起動しない**。`--now` を付けると同時に起動する。
  - 役割を変えた台では、新しいサービスを `enable` し、古いサービスを `disable` する。ISUCON11 予選のあるチームは、使わない参考実装や待機中のサービスを止め、採用した実装だけを有効にした。
  - DB 用に役割を変えた台に、初期アプリと定期ジョブが残っていて、二重に動いた例がある（ISUCON14）。再起動で旧サービスが復活しないか確認する。
  - ISUCON6 本選のあるチームは、1 台ずつ再起動して、元の台にだけ起動設定（Compose の systemd unit）があり、移設先の台に無いことを見つけた。**全台**の自動起動を確認する。
- **確かめ方**: 再起動の後に、期待するサービスだけが動いている（`systemctl list-units --type=service --state=running`）。
- **危険**:
  - systemd の unit に書かれた内容と、サーバー上で手で起動したものが違うと、再起動で消える。
- **出典**: [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)、[systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)。

### 起動順と依存を、systemd で書く

- **効く条件**: 再起動の直後に、アプリが DB につながらず、`failed` のまま上がらない。台の間で起動の順が決まっていない。
- **やり方**:
  - `After=` は順序だけ（依存は作らない）。`Wants=` は弱い要求（相手が失敗しても自分は起動する）。`Requires=` は強い要求。**順序と要求は独立**で、両方を書く（`After=` と `Wants=` を併用）。
  - `Type=simple`（既定）は、fork した直後に「起動した」と見なす。DB の準備を待たない。`Type=notify` は、アプリが準備できたと通知するまで、後続を待たせる（アプリが `sd_notify` を呼ぶ必要がある）。
  - `Restart=on-failure` と `RestartSec=` を付ける。`RestartSec` の既定は 100ms で、DB の起動を待つには短い。数秒にする（値は提案）。
  - 再起動の回数には上限がある（`StartLimitIntervalSec=`・`StartLimitBurst=`）。上限を超えると、自動では再起動されない。`systemctl reset-failed` で解除できる。
  ```ini
  [Unit]
  After=network-online.target postgresql.service
  Wants=network-online.target
  [Service]
  Restart=on-failure
  RestartSec=3s
  [Install]
  WantedBy=multi-user.target
  ```
- **確かめ方**: 全台同時の再起動の後に、`journalctl -u <app> -b` で、起動の失敗と再試行の跡を見る。
- **危険**: 別の台の DB を使う構成では、`After=` は台をまたがない。台をまたぐ起動順は、アプリ側の再試行で吸収する（次項）。`Restart=` の上限に達すると、DB が上がっても、アプリは起動しないまま。
- **出典**: [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)。`RestartSec` の具体的な値は出典未確認（提案）。

### DB より先にアプリが起動したときの再試行

- **効く条件**: 再起動の直後に、PostgreSQL・MySQL への接続エラーで 500 が出る。DB を別の台に置いている。DB だけを再起動しても、アプリが復旧しない。
- **やり方**: アプリの起動時の接続と、実行中の接続の切断の両方を、再試行にする。
  - **Go**: `sql.Open` は接続を確かめない。`db.Ping` で確認し、失敗したら待って再試行する。`SetConnMaxLifetime` を設定すると、古い接続（DB の再起動で切れたもの）を使い続けない。`SetMaxOpenConns` の既定は無制限、`SetMaxIdleConns` の既定は 2。
  - **Rust（sqlx）**: `PoolOptions::connect` は、最初に少なくとも 1 本の接続を開いて確かめる（失敗するとプールが作れない）。`connect_lazy` は、最初は接続を開かない。`test_before_acquire` は既定で `true`（接続を貸す前に生きているか確かめる）。起動時の再試行のループを書くか、`connect_lazy` を使う。
  - 更新の再試行は、二重実行を防ぐ（冪等にする）。
- **確かめ方**: **DB だけを再起動する**試験をする（アプリが先に接続している状態で DB が再起動する順）。アプリが先に起動して DB が後から上がる順（アプリを `stop`・DB を `stop`・アプリを `start`・DB を `start`）も試す。
- **危険**:
  - ISUCON5 本選のあるチームは、全台が起動しきる前の要求で PostgreSQL の接続エラーが出て 500 になったことを再起動の確認で見つけたが、残り時間がなく修正しなかった。同じ年の別のチームは、PostgreSQL の既存接続が切れて次の要求が 500 になる問題を、再起動のテストで見つけた（原文では未修正）。
  - 起動時の再試行が無限ループで、ヘルスチェックを通らないままになると、追試で 502 になる（ISUCON12 予選で、再現スコアが 3 回とも 0 点・nginx が 502（webapp が未起動）で失格になったチームがある）。
- **出典**: [Go: database/sql](https://pkg.go.dev/database/sql#DB.SetConnMaxLifetime)、[sqlx-core: PoolOptions](https://docs.rs/sqlx-core/latest/sqlx_core/pool/struct.PoolOptions.html)、[ISUCON12 予選の結果](https://isucon.net/archives/56838181.html)。（起動時に DB へリトライ接続）。

---

## 5. 永続化の確認

### 「ベンチ中に書いたデータが、再起動後に取得できる」を、再起動の前後で数える

- **効く条件**: アプリに、メモリのキャッシュ・メモリ上のデータ・tmpfs・Redis・独自のファイルがある。DB に書いていないデータが、画面に出る。
- **やり方**:
  1. ベンチを 1 本流す（書き込みが入る）。
  2. 再起動の前に、主要なテーブルの件数・最大 ID（できればチェックサム）を記録する（ベンチの前後のレコード数を比べる手。実例）。
  3. 全台を再起動する。
  4. **`POST /initialize` を叩かずに**、同じ件数を数える。画面（ランキング・一覧・集計）を開いて、値が出るか見る。
  5. その後でベンチを 1 本流す。
- **危険**: 参加者の失敗の例が多い。
  - ISUCON14 の参加者（kawaemon）は、メモリ上にキャッシュする戦略を取っていた。本番では 3 位・学生 2 位まで上がったが、失格の理由は「データ保持違反」（負荷走行で作ったデータが再起動後に取得できなかった）と翌日に分かった。`insert` や `update` には触れていないので、原因は記事の執筆時点でも不明だと書いている。競技中に確認する手段があればよかった、と振り返っている。ポータル上の最終は 37,127 点（834 チーム中 8 位）だった。
  - ISUCON10 本選で、コンテスト情報を initialize のときだけキャッシュに入れていたため、再起動後のブラウザ操作でエラーになった。筆者は失格の原因と推測している。
  - ISUCON12 予選の追試の失格 5 チームのうち 3 チームは、再起動後のブラウザ確認で、`ranking` の表示名が空、billing API が 500、billing API が空の配列、だった。
  - ISUCON3 本選のあるチームは、画像の全件の事前変換でメモリが溢れ、最終構成が FAIL になった。
  - 全面のメモリ化は、永続化を実装しないと本番用にならない（ISUCON14 感想戦）。永続化する場合は、起動時の読み込みと、書き込みの原子性・追記の順・途中で壊れた行・冪等性を設計する。**正常停止のときの退避だけでは、強制停止で消える**。
- **出典**: [kawaemon の ISUCON14 参加記](https://zenn.dev/kawaemon/articles/f40529e0c447f5)、[ponyo877 の ISUCON14 参加記](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)、[ISUCON12 予選の結果](https://isucon.net/archives/56838181.html)、[garasubo の ISUCON10 本選の記録](https://garasubo.com/hexo/2020/10/04/isucon10-final.html)。

### tmpfs・/tmp・/run・/dev/shm に、永続させたいものを置かない

- **効く条件**: SQLite や DB のデータを tmpfs に載せて速くした。画像・一時ファイルを `/tmp`・`/dev/shm`・`/run` に置いた。Unix ドメインソケットを使っている。
- **やり方**:
  - tmpfs はメモリ上のファイルシステムで、再起動で消える。tmpfs に載せるなら、再起動時のデータの復元と、更新の永続化が要る。
  - `/etc/fstab` に tmpfs を足した場合、マウントの失敗で起動しなくなることがある。`nofail` などを検討する（出典未確認）。
  - `/tmp`・`/run`・`/dev/shm` が再起動で消えるかは、環境の既定を `findmnt /tmp` で確かめる（出典未確認）。
  - Unix ドメインソケットは、プロセスが `bind` して作る。親ディレクトリが再起動後に存在するか、所有者と権限、起動順を確認する。古い `.sock` のファイルをコピーしても直らない。
- **確かめ方**: 再起動の後に、同じ入口（nginx → アプリ）から疎通する。
- **危険**:
  - ISUCON12 予選のあるチームは、tmpfs を使った構成で 9,067 点が出たが、OS の再起動に失敗して復旧できず、元の構成に戻した。最終は 4,703 点（9,067 点は提出成功の結果ではない）。原因は確定していない。
  - ISUCON12 予選のあるチームは、nginx とアプリの間を Unix ドメインソケットにした後、終了直前の再起動で `.sock` が消えていることに気づき、復旧できず 0 点になった。TCP に戻す手順を用意しておく。
  - 別のチームは、SQLite を tmpfs に移しても syscall の負荷が残り、採用しなかった。
- **出典**: [kangaechu の ISUCON12 予選記](https://zenn.dev/kangaechu/articles/kangaechu-isucon12-qualify)、[kakira9618 の ISUCON12 予選記](https://kakira96.hatenablog.com/entry/2022/07/25/234252)。

### DB の耐久性の設定を下げるときは、追試が見る「再起動」の種類を確かめる

- **効く条件**: コミットの `fsync` を減らして速くしたい（`innodb_flush_log_at_trx_commit`、`synchronous_commit`、`fsync`）。Redis を主のデータ置き場にしている。
- **やり方**: 各設定の意味を、公式の文書で確かめる。
  - PostgreSQL の `fsync = off` は、クラッシュ後に**復旧できないデータの破損**を起こしうる。文書は、データベース全体を外部のデータから作り直せる場合以外には勧めない。
  - PostgreSQL の `synchronous_commit = off` は、報告済みのコミットを、クラッシュ時に最大で `wal_writer_delay` の約 3 倍（既定で約 600ms）失いうる。ただしデータは壊れない（取り消されたトランザクションのように見える）。トランザクションごとに `SET LOCAL synchronous_commit TO OFF` もできる。
  - PostgreSQL の `full_page_writes = off` も、破損のリスクがある。
  - MySQL の `innodb_flush_log_at_trx_commit`: 1 が既定で完全な耐久性。2 は、OS がクラッシュしたときに約 1 秒分を失いうる。0 は、mysqld のクラッシュでも約 1 秒分を失いうる（取得した文書の本文で確認できず、出典未確認）。定石集には「再起動試験は OS の正常終了なので通る」とある。
  - Redis は、既定では RDB のスナップショット（`dump.rdb`）で、`kill -9` や電源断で直近のデータを失う。AOF（`appendonly yes`）の `appendfsync` は、`everysec`（既定、最大 1 秒失う）・`always`・`no`。AOF を後から有効にするときは、手順（`CONFIG SET` で有効化して、設定ファイルにも書く）を守らないとデータを失う。
- **確かめ方**: 再起動試験の後に、件数が一致するか。設定を戻した場合は、点が変わるか。
- **危険**:
  - **マニュアルが明示的に禁止していなくても、追試は「再起動後にデータが残る」ことを見る。** その再起動が、正常な再起動（ISUCON11 本選・ISUCON10 本選は `poweroff` → 起動）か、電源断かは、回による。電源断を想定する回で、`fsync` を切るのは危険。
  - 永続化を捨てるのは、失格につながりやすい手。「メモリだけにして DB を全廃」は、永続化を実装しない限り使えない。
  - `fsync` を切るかどうかは、競技の趣旨（問題が許す範囲）を、その回のマニュアルとレギュレーションで確かめてから決める。
- **出典**: [PostgreSQL: WAL の設定](https://www.postgresql.org/docs/current/runtime-config-wal.html)、[Redis: Persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)。MySQL の値の意味は出典未確認。

---

## 6. 初期化の時間と再現性

### 終盤にも、初期化の所要と整合性を測る

- **効く条件**: スキーマ・データ構造・キャッシュを何度も変えた。初期化が、競技の序盤より遅くなっている。
- **やり方**: 凍結の前後に、`POST /initialize` の所要を測る。制限の半分以内を目安にする（制限は [14 章](14-benchmarker-and-scoring.md)の表）。再起動の直後の冷えた状態でも測る。
- **確かめ方**: 初期化の所要。初期化後にベンチの整合性チェックが通る。自分で追加したキャッシュ・複製先・メモリの状態が、初期化で初期状態に戻る。
- **危険**:
  - **初期化が長くても、負荷テストの時間は削られない**。縮める価値は、制限の余裕と失敗しないことにある。
  - ISUCON12 予選の講評は、MySQL への移行で `mysqldump` の論理バックアップでは 30 秒に収まらなかったと書く。データのコピーなど、速い復元の手を使う（は、全テーブルの読み直しで約 33 秒かかり、撤回した例）。
  - 新しく作った台は、EBS の初期化が終わっていないと、I/O が遅くタイムアウトしうる。
- **出典**: [ISUCON12 予選の講評](https://isucon.net/archives/56850281.html)。

---

## 7. 最終ベンチ

### 最終スコアが「どの走行の点か」で、最終ベンチの打ち方が変わる

- **効く条件**: 終盤に、最終ベンチを何本流すか決める。最高の点と、最後の点が違う。
- **やり方**: マニュアルを読んで、次のどれかを確かめる（[14 章](14-benchmarker-and-scoring.md)の表）。
  | 型 | 例 | 打ち方 |
  | --- | --- | --- |
  | 再起動後の走行のうち**最高** | ISUCON10 本選・ISUCON11 本選 | 再起動後の走行は運営が行う。最終の構成は、再起動後の冷えた状態で強いこと |
  | 再起動後の**最初**の走行 | ISUCON9 本選 | 同上。最初の 1 本で決まる |
- **確かめ方**: 最終ベンチを、凍結後に数本流して、PASS と点のぶれを見る。
- **危険**:
  - 最後の走行の前に、変更を入れない。最後の走行は、すでに何度も通った構成で行う。

### 最終ベンチの前に、全台を再起動して、構成を変えずに 2 本流す

- **効く条件**: 追試の前に、冷えた状態で閾値（75%・85%）を満たすか確かめたい。
- **確かめ方**: 再起動直後の 1 本目と 2 本目の差が、追試の閾値より小さいか。
- **危険**: 1 本目が閾値を割ると、追試で再実行になる（3 回目で同じなら失格）。冷えた状態の弱さは、起動時のキャッシュの暖機・プールの事前確立で減らせる（ただし永続化の要件の代わりにはならない。実例）。

---

## 8. 競技終了後は接続しない

### 競技終了から、主催者の告知まで、競技サーバーへ接続・操作しない

- **効く条件**: 常に。終了後に、自動の監視・ジョブ・ポート転送が、競技サーバーへ接続し続ける。
- **やり方**:
  - 接続の期限を、設定（環境変数・スクリプト）に持たせ、期限を過ぎると接続しない仕組みにする。
  - 計測用のサーバーを消したくなっても、終了後・告知前の操作は避ける。**削除を含む操作は失格**になる回がある。
- **確かめ方**: 終了時刻の後に、競技サーバーへの接続が残っていないか（`ss -tnp`・`who`）。
- **危険**:
  - 失格になる。ISUCON13 の当日マニュアルは、「競技終了後、別途アナウンスがあるまでの作業や操作（削除を含む）は失格」と書く。ISUCON11 本選・ISUCON10 本選・ISUCON9 本選も同じ趣旨。
  - 追試は、主催者が再起動して計測する。**競技終了時点で、全台が起動している**必要がある。停止しない。
  - 終了後に、出題内容をブログなどに公開するのは、レギュレーションの範囲で、結果の発表後の扱いを確かめる。
- **出典**: [ISUCON13 の当日マニュアル](https://github.com/isucon/isucon13/blob/main/docs/cautionary_note.md)、[ISUCON11 本選のマニュアル](https://github.com/isucon/isucon11-final/blob/main/docs/manual.md)、[ISUCON10 本選のマニュアル](https://github.com/isucon/isucon10-final/blob/master/docs/manual.md)、[ISUCON9 本選のマニュアル](https://github.com/isucon/isucon9-final/blob/master/docs/MANUAL.md)、・。

---

## 9. よくある事故の確認

### cron・タイマー・不要なサービスが、ベンチや再起動に影響する

- **効く条件**: ベンチ中に、原因不明の CPU・I/O の波がある。再起動で、古いジョブが復活する。
- **やり方**:
  ```sh
  systemctl list-timers                    # タイマー（次回・前回の実行）
  sudo crontab -l; ls /etc/cron.d /etc/cron.daily
  systemctl list-units --type=service --state=running
  ps aux --sort=-%cpu | head
  ```
  - 役割を変えた台に残った旧サービスと定期ジョブを止める（ISUCON14 で二重の配車が起きた。実例）。
  - ログイン時に動くスクリプト。
- **確かめ方**: ベンチ中に `top` で、アプリ・DB・nginx 以外が CPU を使っていないか見る。
- **危険**: 運営用・追試用のサービスやタイマーを、止めない・消さない。
- **出典**: [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)（`list-timers`）

### ディスクの残量とログの肥大

- **効く条件**: 競技が進むと、ログ・DB のデータ・ビルドの成果物でディスクが埋まる。ディスクが満杯だと、DB・アプリが書けずに止まる。
- **やり方**:
  ```sh
  df -h                           # 容量
  du -sh /var/log/* /var/lib/* 2>/dev/null | sort -h | tail
  journalctl --disk-usage
  ```
  - ISUCON13 のテンプレートの EBS は gp3 40GB × 3 台だった。
  - journald は、永続の保存で、`SystemMaxUse=`（既定はファイルシステムの 10%、上限 4G）と `SystemKeepFree=`（既定は 15%、上限 4G）の小さいほうに従う。
  - `long_query_time = 0` のスロークエリログ、nginx のアクセスログ、`perf.data` が、急に大きくなる。**ローテートして、不要なものを消す**。
  - `inode` の枯渇・Rust の `target/` の容量（競技サーバー上でビルドする場合）は出典未確認。
- **確かめ方**: 凍結の前と最終ベンチの前に `df -h`。
- **危険**: 競技サーバーの外のファイルの削除は、運営用のファイルを消さないように。ログを消す前に、必要なものを手元に保存する。
- **出典**: [journald.conf](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)、[ISUCON13 の当日マニュアル](https://github.com/isucon/isucon13/blob/main/docs/cautionary_note.md)（EBS）、[MySQL: The Slow Query Log](https://dev.mysql.com/doc/refman/8.0/en/slow-query-log.html)。

### OOM と古いプロセス

- **効く条件**: DB が突然止まる。ベンチの点が、変更していないのに変わる。変更したコードが動いていない。
- **やり方**:
  - 再起動で、古いバイナリが復活しないか（再起動の後のバイナリの版を確かめる）。
- **確かめ方**: 再起動の後に、動いているバイナリの commit・版を、アプリのログかヘルスチェックの応答で確かめる。
- **危険**: プロセスの一括停止（`pkill`）は、無関係なプロセスを巻き込む。

### 静的ファイルの配信と権限

- **効く条件**: nginx で静的ファイルを配信するようにした。画面が崩れる。
- **やり方**: API が通るだけでなく、静的ファイルの応答（200 か）を、再起動の後にも確かめる。nginx の実行ユーザーが、`root` と親ディレクトリを読めるか（`Permission denied` の確認）。
- **確かめ方**: ブラウザで画面を開き、`error.log` に `Permission denied` が無いか見る。
- **出典**: [ISUCON9 本選のマニュアル](https://github.com/isucon/isucon9-final/blob/master/docs/MANUAL.md)。

### 設定が再起動で消える

- **効く条件**: 再起動の前に出た点が、再起動の後に出ない。
- **やり方**: `sysctl -w`・`echo > /proc/...`・`SET GLOBAL`・シェルの `ulimit` で入れた設定は、再起動で消える。`/etc/sysctl.d/`・my.cnf・systemd の `LimitNOFILE=` に書く（[02 章](02-os-kernel.md)）。
- **確かめ方**: **再起動の後に**、値をもう一度読む。
- **危険**: 再起動の前の点は、設定が消えた状態では出ない。最終ベンチの前に、再起動の後の点で確かめる。
- **出典**: [02 章](02-os-kernel.md)。

---

## 最終チェックリスト（再起動試験・最終ベンチの直前）

| 確認 | 済 |
| --- | --- |
| マニュアルの「最終スコア」「追試」「変更禁止」を読み直した | |
| 運営の追加の告知・質問への回答を、全部読み直した | |
| 競技中の最終の 1 つの commit に、全台の構成（コード・設定・unit）を固定した | |
| 戻す commit（revert）を用意した | |
| 全台で、必要なサービスが `enable`、不要なサービスが `disable` | |
| 全台同時の再起動で、手作業なしにアプリが上がった（`list-units --failed` が空） | |
| 再起動の前後で、件数が一致した。initialize を叩かずに画面が出た | |
| DB だけを再起動しても、アプリが復旧した | |
| 再起動直後（冷えた状態）のベンチが、最終スコアの 75%（または 85%）以上 | |
| ログ・計測・トレース・pprof を切った後に、ベンチが PASS した | |
| 計測用の台・ツール・外向きの接続・追加のポートを片付けた | |
| `df -h` に余裕がある | |
| 競技終了の数分前に、SSH を使う常駐のジョブを全部止めた | |
| 競技終了後は、告知まで接続しない | |

## よくある失敗

- **再起動試験を、終了の直前にやる。** 壊れていても、直す時間が無い。2 時間前までに 1 回。
- **1 台ずつだけ再起動する。** 台の間の起動順の問題が見えない。全台同時でも試す。
- **再起動の後に、手作業でサービスを起動して、成功にする。** 追試では手作業は入らない。
- **メモリのキャッシュ・メモリ上のデータ・tmpfs だけにデータがある。** 再起動で消え、追試で失格（ISUCON14・ISUCON10 本選・ISUCON12 予選の例）。
- **initialize のときだけキャッシュを埋める。** 再起動の後は空。キャッシュミス時に DB から読む。
- **Unix ドメインソケットや tmpfs を、再起動で確かめずに終盤に入れる。** 0 点になった例がある。
- **ベンチ中にデプロイ・ビルド・再起動をする。** FAIL・減点になる。
- **最後の走行を、新しい変更の確認に使う。** 最後の走行が最終の点になる回では、通った構成で終える。
- **ログ・計測を切った後に、ベンチで確かめない。** 切るのも変更。
- **計測用の台・ツールを、同じ VPC・同じアカウントに残す。** 環境確認に響いた可能性がある。
- **競技終了後に、自動のジョブが接続し続ける。** 失格。
- **運営の追加の告知を読み直さない。** 失格条件が競技中に追加されることがある。

## 出典一覧

ISUCON 公式のマニュアル・講評・結果

- [ISUCON9 本選のマニュアル](https://github.com/isucon/isucon9-final/blob/master/docs/MANUAL.md)、[ISUCON10 本選のマニュアル](https://github.com/isucon/isucon10-final/blob/master/docs/manual.md)、[ISUCON11 本選のマニュアル](https://github.com/isucon/isucon11-final/blob/main/docs/manual.md)
- [ISUCON13 の当日マニュアル](https://github.com/isucon/isucon13/blob/main/docs/cautionary_note.md)、[ISUCON14 のマニュアル](https://github.com/isucon/isucon14/blob/main/docs/manual.md)
- [ISUCON10 予選の講評](https://isucon.net/archives/55025156.html)、[ISUCON12 予選の講評](https://isucon.net/archives/56850281.html)
- [ISUCON10 本選の追試の発表](https://isucon.net/archives/55028786.html)、[ISUCON12 予選の結果](https://isucon.net/archives/56838181.html)、[ISUCON14 の結果](https://isucon.net/archives/58837992.html)

参加記

- [kawaemon の ISUCON14 参加記](https://zenn.dev/kawaemon/articles/f40529e0c447f5)、[ponyo877 の ISUCON14 参加記](https://zenn.dev/ponyo877/articles/1e29255b9d19bf)
- [iceman（うどんきれいなGo）の ISUCON14 参加記](https://zenn.dev/iceman/articles/1780deef84eda8)、[kangaechu の ISUCON12 予選記](https://zenn.dev/kangaechu/articles/kangaechu-isucon12-qualify)、[kakira9618 の ISUCON12 予選記](https://kakira96.hatenablog.com/entry/2022/07/25/234252)
- [garasubo の ISUCON10 本選の記録](https://garasubo.com/hexo/2020/10/04/isucon10-final.html)

公式文書

- [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)、[systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)、[journald.conf](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)
- [Go: database/sql](https://pkg.go.dev/database/sql#DB.SetConnMaxLifetime)、[sqlx-core: PoolOptions](https://docs.rs/sqlx-core/latest/sqlx_core/pool/struct.PoolOptions.html)
- [PostgreSQL: WAL の設定](https://www.postgresql.org/docs/current/runtime-config-wal.html)、[MySQL: The Slow Query Log](https://dev.mysql.com/doc/refman/8.0/en/slow-query-log.html)、[Redis: Persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)

手元の資料

## 出典未確認の項目（後で人間が確かめる）

- `RestartSec` の具体的な値（数秒）は、提案であり、出典の値ではない。
- `/etc/fstab` の tmpfs の `nofail`、`/tmp`・`/run`・`/dev/shm` が再起動で消えるかどうか、`inode` の枯渇、Rust の `target/` の容量。
- MySQL の `innodb_flush_log_at_trx_commit` の各値の意味（取得した文書の本文で確認できなかった）。
- ISUCON13 のあるチームが「tags のキャッシュのミスで再起動チェックで fail した」という検索結果の要約があったが、本文は読んでおらず、本章に書いていない。
