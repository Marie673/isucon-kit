# DB（MySQL）の定石

詳しくは `docs/tuning/README.md`。

## インデックス

- スロークエリの WHERE / ORDER BY / JOIN 条件の列に複合インデックス。順序は「等価条件 → 範囲 / ORDER BY」
  - 例: `WHERE user_id = ? ORDER BY created_at DESC LIMIT 1` → `INDEX(user_id, created_at)`
- 「最新の1件」系はインデックスで `LIMIT 1` が効く形にする。降順インデックス `INDEX(a, b DESC)` も使える
- `EXPLAIN` で `type: ALL`（全件走査）、`Using filesort`、`Using temporary` を潰す
- 関数を噛ませた列（`DATE(created_at)`、`LOWER(name)`）にはインデックスが効かない → generated column + index
- 位置検索（緯度経度の範囲）は複合インデックスより、アプリ側のグリッド分割やメモリ上の検索が効くことが多い
- インデックスは初期化 SQL（schema / init）に `ALTER TABLE ... ADD INDEX` で入れ、initialize で作られるようにする

## N+1

- ループ内の `SELECT ... WHERE id = ?` → `WHERE id IN (?)` で一括取得してマップで引く（Go: `sqlx.In`）
- 1件ごとの集計（件数・合計・最新）→ `GROUP BY` で一括、または集計値を別テーブル / メモリで持つ
- JOIN できるなら JOIN。ただし巨大な JOIN は逆効果のことがある（計測で判断）

## 書き込み

- 1件ずつの INSERT → バルク INSERT（`INSERT ... VALUES (...),(...)`）
- 書き込み直後に同じ行を読み直す処理は、書いた値をそのまま使う
- ロック待ち（`FOR UPDATE` の多用）はトランザクションを短く。不要な `FOR UPDATE` を外す

## my.cnf（hosts/<db>/etc/mysql/mysql.conf.d/ に置く）

```ini
[mysqld]
innodb_buffer_pool_size = 1G          # メモリの 50〜70%（同居するアプリの分を残す）
innodb_flush_log_at_trx_commit = 2    # コミットごとの fsync をやめる（再起動試験は OS 正常終了なので通る。docs/DECISIONS.md の「永続化を弱める設定」の表に書く）
innodb_doublewrite = 0
disable_log_bin                       # binlog を止める（MySQL 8.0）
innodb_log_file_size = 512M           # 8.0.30+ は innodb_redo_log_capacity
max_connections = 1000
skip_name_resolve
```

- デプロイ時、my.cnf の内容が変わったときだけ mysql が再起動される
- アプリ側の接続プール: Go なら `db.SetMaxOpenConns(…)` と `SetMaxIdleConns` を同じ値に。`interpolateParams=true` で PREPARE / CLOSE STMT の往復を消す（スロークエリに `ADMIN PREPARE` が大量に出ていたらこれ）

## DB を別ホストに出すとき

- `bind-address = 0.0.0.0`、ユーザーを `'isucon'@'%'` で作成、アプリの env（`env.sh` 等）の DB ホストを書き換え
- 片方だけ再起動されても復旧するように、アプリは起動時に DB へリトライ接続する（起動順序が保証されない追試対策）
