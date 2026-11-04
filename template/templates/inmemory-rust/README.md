# inmemory-rust — インメモリ全書き換えのテンプレート

DB を使わず、全状態をメモリに置く axum サーバ。追試（再起動）に耐える永続化を持ち込み済み。
手順は skill `isu-rewrite` を読む。

## 永続化のしくみ（src/store.rs）

| 場面 | 何が起きるか |
|---|---|
| 起動 | seed（`isu-export-seed` が書いた `<table>.jsonl`）から状態を作り、delta を先頭から再生 |
| 書き込み | `store.update` が書き込みロックの中で「検証 → 反映 → delta バッファに積む」 |
| ベンチ中 | ファイルに書かない（負荷を避ける） |
| アイドル 1 秒 | バッファを delta に追記して fsync |
| SIGTERM（stop / reboot） | 即 flush してから終了 |
| initialize | 状態を seed に戻し、delta を空に |
| クラッシュで途中まで書かれた行 | 起動時に捨てて切り詰める |

`cargo test` で、再生・アトミックな拒否・reset・壊れた末尾行の4ケースを確認できる。

## 使い方

```sh
cp -R templates/inmemory-rust webapp/rust-mem
bin/isu-export-seed webapp/rust-mem/seed
# src/model.rs と src/main.rs を問題に合わせて書き換える
cargo test
bin/isu-build      # ISU_LANG=rust, ISU_SRC_DIR=webapp/rust-mem, ISU_BIN_NAME=isu-mem
```

systemd は `isu-mem.service`（delta は `/home/isucon/state/delta.jsonl`）。
