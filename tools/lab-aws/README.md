# lab-aws

手元の再現を、本番と同じ CPU の系統の EC2 で回すための道具。本番の台と同じ命令セットで、ベンチの負荷や microbench を測る。

## なぜ要るか

台によって CPU の機能が違うことがあり、手元の別系統の CPU では効き目が本番と逆に出ることがある。本番と同じ系統の CPU で測れば、この差が出ない。

先に [../cpu-features](../cpu-features/README.md) で、各台の CPU 機能を表にする。差があった系統（例: c5.large と c7i.large）を、ここで 1 台ずつ借りる。

## 使う前に（人間の承認）

台を作ると費用がかかる。**`up` を流す前に、人間の承認を取る。** 承認してもらうのは次の 4 つ。

1. 同時数の上限（`LAB_MAX_INSTANCES`）
2. インスタンスの種類
3. 自動停止までの時間（`--ttl`、上限 `LAB_MAX_TTL_MIN`）
4. 費用の上限（台数 x 時間 x 単価。目安は `lab-aws price`）

- `LAB_MAX_INSTANCES`、`LAB_KEY_NAME`、`LAB_SG_ID` のどれかが空だと `up` は動かない。空のままにしておくことが、承認前の安全装置。
- `up` には `--yes` が要る。承認を得てから付ける。先に `--dry-run` で内容を見せる。
- 使い終わったら `down`。忘れても `--ttl` で台が自分で止まって消える。念のため `sweep` で残りを確かめる。

## 事前に用意するもの

この道具は、キーペア・セキュリティグループ・IAM を作らない（AWS の作成系の操作を `up` の `run-instances` と `down` / `sweep` の `terminate-instances` だけに絞るため）。作業者が先に用意する。

- キーペア（`LAB_KEY_NAME`）と、その秘密鍵のパス（`LAB_SSH_KEY`）。鍵をコピー・出力・コミットしない。
- セキュリティグループ（`LAB_SG_ID`）。インバウンドは自分の IP からの 22 番だけ。タグ `purpose=isucon-lab` を付けておくと片付けやすい。
- パブリック IP が付くサブネット（既定の VPC のサブネットでよい）。付かないサブネットなら `LAB_HOST_FIELD=private` で、VPN などから届く経路を使う。

作成のコマンド例は AWS の公式手順（`aws ec2 create-key-pair` など）を見る。これも人間の承認が要る。

## 設定

環境変数か `.env`（雛形は `.env.example`）。`LAB_*`、`AWS_PROFILE`、`AWS_REGION` だけ読む。環境変数が先。プロファイルとリージョンの既定値は持たない（未設定なら `aws` 自身の設定に任せる）。例: プロファイル `my-profile`、リージョン `ap-northeast-1`。

| 変数 | 意味 | 既定 |
| --- | --- | --- |
| `LAB_MAX_INSTANCES` | 同時数の上限（必須） | なし |
| `LAB_KEY_NAME` / `LAB_SG_ID` | キーペア名 / セキュリティグループ（up に必須） | なし |
| `LAB_SSH_KEY` | 秘密鍵のパス（ssh・push・run・fetch・cpuinfo に必須） | なし |
| `LAB_SUBNET_ID` | サブネット | 既定の VPC |
| `LAB_SSH_USER` | ssh のユーザー | `ubuntu` |
| `LAB_MAX_TTL_MIN` | `--ttl` の上限（分） | 240 |
| `LAB_ALLOWED_FAMILIES` | 使ってよい系統 | `c5,c5n,c6i,c6a,c7i,c7a,m5,m6i,m7i` |
| `LAB_SPOT` | 1 でスポット | オンデマンド |
| `LAB_VOLUME_GB` | ディスク（gp3） | 30 |
| `LAB_AMI` | AMI を固定する | Ubuntu 24.04（SSM 経由で最新） |
| `LAB_HOST_FIELD` | ssh の宛先 `public` / `private` | `public` |
| `LAB_STATE_DIR` | 状態・持ち帰り先 | `.cache/lab-aws` |

サイズは medium〜4xlarge だけ。metal や大きなサイズは作れない。

## 使い方

```sh
lab-aws up --type c5.large --count 1 --ttl 2h --dry-run   # 作るコマンドを + で表示するだけ
lab-aws up --type c5.large --count 1 --ttl 2h --yes       # 承認のあとだけ
lab-aws list                                              # 残り（タグ purpose=isucon-lab）
lab-aws cpuinfo                                           # /proc/cpuinfo を持ち帰る → cpu-features に渡す
lab-aws push ./webapp --dest work/webapp                  # 作業木を全台へ送る
lab-aws run --dir work/webapp -- 'cargo bench'            # 全台で実行（--node 2 で 1 台だけ）
lab-aws fetch work/webapp/target/criterion --to ./result  # 持ち帰る（台ごとに node<N>/）
lab-aws ssh --node 1                                      # ログイン
lab-aws down                                              # この実験室の台を終了
lab-aws sweep                                             # 期限を過ぎた残りを終了
lab-aws price --type c5.large --count 2 --hours 2         # 時間単価と目安（読み取りだけ）
```

- 1 回の `up` が 1 つの実験室（`lab-YYYYMMDD-HHMMSS-xxxx`）。台は 1 から番号が付く。実験室が 1 つなら `--lab` は省略できる。
- `push` は `.git`・`.env`・`*.pem`・`*.key`・`id_*`・`.cache`・`target`・`node_modules`・`.venv` を送らない（`--exclude` で足せる）。台でビルドする。
- `run` の `--` の後ろが 1 つなら shell の文字列として、複数なら引数として渡す。
- 全部のサブコマンドに `--dry-run` がある。aws・ssh・rsync を実行せず、コマンドを `+` で表示する。
- 台の ssh のホスト鍵は実験室ごとの `known_hosts`（`LAB_STATE_DIR` の下）に置く。IP が使い回されても衝突しない。

## 安全装置

- タグ `purpose=isucon-lab` の付いたものだけを作る・見る・消す。`terminate-instances` に渡す ID は、タグ付きで describe して確かめたものだけ。タグが違う台は、フィルターが効かなくても捨てる。
- 変更系の aws 操作は `run-instances`（`up`）と `terminate-instances`（`down`・`sweep`）だけ。ほかを呼ぶ道は、道具の中で拒否する。
- 台は user-data の最初に `shutdown -h +N` を予約し、終了動作は terminate。道具が途中で死んでも N 分で消える。IMDSv2 必須。ルートボリュームは終了時に削除。
- 同時数の上限（起動中の台 + 今回の台）、`--ttl` の範囲（10 分〜`LAB_MAX_TTL_MIN`）、系統とサイズの許可リスト、`--yes`。
- `sweep` は、タグの `lab-expires`（起動時に書いた期限）を猶予（既定 10 分）過ぎた台だけを終了する。期限のタグが無い台は、起動から `LAB_MAX_TTL_MIN` を過ぎたもの。
- `run-instances` の client-token は実験室と台の番号から決める。同じ呼び出しの再送で二重に作らない。途中で失敗したら、作れた台の ID と `down --lab` の案内を出す。
- 秘密（鍵、`.env`）を送らない・表示しない。

## 費用の目安

- 費用 = 台数 x 時間 x 時間単価。上限は台数 x `--ttl` x 単価。ディスク（gp3）と転送は別で、短時間なら僅少。
- 時間単価は `lab-aws price --type <種類>`（Pricing API。読み取りだけ）で調べる。引けなければ「要確認」と出る。
- 2026-11-01 に東京リージョンのオンデマンド・Linux を引いた値: c5.large は $0.1070 / 時間、c7i.large は $0.1124 / 時間。単価は変わるので、承認を取る前に `price` で引き直す。スポットの単価は未確認（要確認）。
- 2 台 x 2 時間なら約 $0.5。ただし台を消し忘れない。`list` で残りを確かめる。

## 手元の ARM と結果が変わる典型

Apple Silicon（ARM）の手元と、本番の x86 では、同じコードでも次が変わる。手元の結果をそのまま本番に当てはめない。

| 処理 | 手元（ARM） | 本番 x86（c5 系など） |
| --- | --- | --- |
| SHA-1 / SHA-256 / HMAC | 専用命令（sha2）で速い | c5 系（Xeon 8124M）は `sha_ni` が無く、ソフト実装で遅い。c7i 系は有る |
| AES-GCM・CRC | aes / pmull / crc32 で速い | aes と pclmulqdq で速いが、`vaes` は新しい系統だけ |
| SIMD | NEON（128 ビット） | AVX2 / AVX-512 が別の経路・別の速さになる |
| 周波数・メモリ帯域 | 高い・広い | クラウドの vCPU は 1 コアの SMT の片方であることが多い |

`sha_ni` が無い台で、署名のような SHA の呼び出しが多いなら、複数をまとめて AVX2 / AVX-512 で計算する（マルチバッファ）実装が効く。microbench を、本番と同じ系統の台で測ってから決める。

## テスト

AWS・ssh・rsync は呼ばない。組み立てたコマンドと安全装置だけを確かめる。

```sh
cd tools/lab-aws && uv run --no-project python -m unittest discover -s tests
```

読み取りだけの呼び出し（`list`・`price`・`--dry-run`）は、実際の AWS でも確かめた。`up`・`down`・`sweep` の実行は、一度も流していない。
