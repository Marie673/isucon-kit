# アプリの定石

## キャッシュ

- 変わらないマスタ（設定・モデル定義・カテゴリ）→ 起動時にメモリへ。initialize でも読み直す
- よく読む・まれに書く（ユーザー情報・セッション→ユーザー）→ メモリのマップ。書き込みパスで更新 / 削除
- 集計値（件数・合計・ランキング）→ 書き込み時に増分更新（DB に問い合わせて数え直さない）
- Go: `sync.RWMutex` + map、またはシャーディング。重い取得の同時実行は `singleflight` で1回にまとめる
- **initialize でキャッシュを必ず作り直す**（ベンチは initialize から始まる）
- 複数台でアプリを動かすとメモリキャッシュは台ごとにずれる → キャッシュするなら1台に寄せるか、無効化が不要なデータに限る

## 重い処理の常連

- パスワードハッシュ（bcrypt のコスト）、JSON エンコード、テンプレート描画、正規表現のコンパイル（毎回 `regexp.MustCompile` していないか）
- 画像の変換・リサイズ・DB からの画像読み出し → ファイルに書き出して nginx で配信（infra.md）
- 外部 API 呼び出し → タイムアウト・並列化・結果のキャッシュ（仕様が許す範囲で）

## 非同期化

- レスポンスに不要な処理（通知の配信、集計、ログ）はキュー（チャネル）に積み、固定数のワーカーで処理する
- ワーカー数は増やしすぎない（実例: 並列 2 → 8 で TCP の再送が増えスコアが下がった）
- 非同期化した処理は、再起動時に取りこぼしてよいものか確認する（追試）

## Go の勘所

- `GOMAXPROCS` は既定でよい。GC が perf 上位なら `GOGC=200〜400`（メモリに余裕があれば）
- `encoding/json` が重ければ `github.com/goccy/go-json` に差し替え（import の置き換えだけで済む）
- HTTP クライアントは使い回す（`http.Client` を毎回作らない）。`MaxIdleConnsPerHost` を上げる
- ログ出力（echo / chi のリクエストロガー）は終盤に止める

## Rust の勘所

- ビルドは手元で `cargo zigbuild`（`bin/isu-build`）。サーバではビルドしない
- `[profile.release]` は `lto = "fat"`, `codegen-units = 1`, `debug = "line-tables-only"`（perf のシンボルのため）
- グローバルアロケータを mimalloc に（`#[global_allocator]`）
- 共有状態は `parking_lot::RwLock`。長く握らない（ロックの中で await しない）
- JSON は `serde_json`。大きなレスポンスは `sonic-rs` も候補
