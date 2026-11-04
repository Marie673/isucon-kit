# cpu-features

各台の CPU 機能（`/proc/cpuinfo` か `lscpu` の出力）を比べて、Markdown の表にする道具。サーバーには接続しない。

## なぜ要るか

台によって CPU の機能が違うことがあり、それが処理の優先度につながらないことがある。台によって速さが変わる処理を、計測の前に表で見えるようにする。

## 使い方

取得は別の手順（この道具は読むだけ）。台ごとにファイルへ落とす。

```sh
ssh host1 cat /proc/cpuinfo > host1.cpuinfo      # lscpu の出力でもよい
ssh host2 cat /proc/cpuinfo > host2.cpuinfo
tools/cpu-features/cpu-features host1=host1.cpuinfo host2=host2.cpuinfo
```

- ラベルは `LABEL=FILE`。省略するとファイル名の最初の部分。`-` で標準入力。
- 入力の形式は自動で見分ける: `/proc/cpuinfo`、`lscpu`、macOS の `sysctl`（`machdep.cpu` / `hw.optional`）。
- `--local` で手元の機械を足す。macOS は `sysctl machdep.cpu hw.optional ...`、Linux は `/proc/cpuinfo` を読む。手元が ARM で台が x86 だと「手元との差」の警告が出る。
- `--json` で JSON。
- `uv run --script` で動く（標準ライブラリだけ。pip は使わない）。
- EC2 の実験室（[../lab-aws](../lab-aws/README.md)）を作ったら、`lab-aws cpuinfo` が各台の `/proc/cpuinfo` を持ち帰り、この道具に渡すコマンドを表示する。

## 読み方

- 先頭の警告が最優先。「台によって速さが変わる処理」は、同じコードでも台で遅さが違う。遅い台がボトルネックになる。
- 「全部の台で専用命令が無い」処理は、台の差ではなく、全体が遅い。
- `n/a` はその ISA に無い機能（x86 の AVX は ARM に無い、など）。`no` は「ISA にあるが、この CPU には無い」。
- 専用命令が無い処理への手: SHA-256 は、複数の署名をまとめて計算する（AVX2 / AVX-512 のマルチバッファ）と、専用命令なしでも速くなる。警告にその台の avx2 / avx512f の有無が出る。
- 「速い・遅い」は命令の有無だけの判定。実際の差は microbench で測る（[../lab-aws](../lab-aws/README.md) の実験室で、本番と同じ系統の CPU で測る）。

## 手元（Apple Silicon）との違い

SHA・AES・SIMD は、手元の ARM（SHA 命令あり）と本番の x86 で効き目が逆になりうる。手元で測った SHA や SIMD の結果は、そのまま本番に当てはめない。

## 出力例

`tests/fixtures` の入力（Xeon 8124M 風・Sapphire Rapids 風・Apple M 系。実機の出力に似せた合成データで、実機そのものではない）で実行した結果。

```sh
cpu-features host1=tests/fixtures/xeon-8124m.cpuinfo host2=tests/fixtures/sapphire-rapids.cpuinfo mac=tests/fixtures/apple-m.sysctl
```

````markdown
# CPU 機能の比較

## 警告: 台によって速さが変わる処理

- **SHA-1 / SHA-256 / HMAC（署名・ハッシュ・JWT）**: host1 は sha_ni が無く遅い。host2, mac は専用命令で速い。同じ処理でも台によって速さが変わる。 複数の署名をまとめて計算する（マルチバッファ SHA-256）と速くなる。

## 台

| 台 | ISA | モデル | 論理 CPU | コア | 周波数 |
| --- | --- | --- | ---: | ---: | ---: |
| host1 | x86_64 | Intel(R) Xeon(R) Platinum 8124M CPU @ 3.00GHz | 2 | 1 | 3400 MHz |
| host2 | x86_64 | Intel(R) Xeon(R) Platinum 8488C | 2 | 1 | 2400 MHz |
| mac | arm64 | Apple M2 | 8 | 8 | 不明 |

## 機能

n/a はその ISA に無い機能（x86 の AVX は ARM に無い、など）。

| 機能 | host1 | host2 | mac | 意味 |
| --- | :---: | :---: | :---: | --- |
| sha_ni | **no** | yes | yes | SHA-1/SHA-256 の専用命令（ARM は sha2） |
| aes | yes | yes | yes | AES の専用命令（AES-NI） |
| pclmulqdq | yes | yes | yes | キャリーなし乗算。GCM・CRC に使う（ARM は pmull） |
| vaes | **no** | yes | n/a | AES を 256/512 ビット幅で（vaes） |
| vpclmulqdq | **no** | yes | n/a | pclmulqdq の幅広版 |
| gfni | **no** | yes | n/a | ガロア体の命令。バイト列処理・暗号の一部 |
| sse4_2 | yes | yes | n/a | crc32c 命令・文字列比較 |
| avx2 | yes | yes | n/a | 256 ビットの整数 SIMD |
| avx512f | yes | yes | n/a | 512 ビット SIMD の基本 |
| avx512bw | yes | yes | n/a | 512 ビットのバイト・ワード演算（文字列・画像） |
| avx512vl | yes | yes | n/a | AVX-512 を 128/256 ビット幅でも使う |
| avx512vbmi | **no** | yes | n/a | バイトの並べ替え（base64・UTF-8 の SIMD 実装） |
| bmi2 | yes | yes | n/a | ビット操作（pdep/pext） |
| neon | n/a | n/a | yes | ARM の SIMD（asimd） |
| crc32 | n/a | n/a | yes | ARM の CRC32 命令 |

## 機能が無いと重くなる処理

| 処理 | host1 | host2 | mac | 備考 |
| --- | :---: | :---: | :---: | --- |
| SHA-1 / SHA-256 / HMAC（署名・ハッシュ・JWT） | **遅い**（sha_ni なし） | 速い | 速い | sha_ni が無いとソフト実装になり、1 回あたりが数倍遅い。 |
| AES-GCM / AES-CBC（TLS・暗号化 Cookie・暗号化列） | 速い | 速い | 速い | aes か pclmulqdq が無いとソフト実装。vaes があると幅が広がってさらに速い。 |
| 画像処理・SIMD 全般（リサイズ・エンコード・バイト列の検索） | 速い | 速い | 速い | avx2 / avx512 が無いと SIMD 化されたライブラリが遅い経路に落ちる。 |
| 圧縮・CRC（gzip・zlib・crc32c・Brotli） | 速い | 速い | 速い | pclmulqdq / sse4_2（ARM は pmull / crc32）が無いと CRC と圧縮が遅い。 |
| JSON・UTF-8 検証・base64 の SIMD 実装（simdjson 系） | 速い | 速い | 速い | avx2 が無いと SSE 系に落ちる。avx512bw / avx512vbmi があるとさらに速い。 |
````

## 機能と処理の対応を直すとき

`cpu-features` の `FEATURES`（機能）と `WORK_ITEMS`（機能が無いと重くなる処理）を編集する。ISA ごとの必要な機能は `needs` に書く。

## テスト

```sh
cd tools/cpu-features && uv run --no-project python -m unittest discover -s tests
```
