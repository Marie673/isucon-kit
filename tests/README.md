# 道具の結合テスト

`bin/kit-test` で全体を検証する。
`uv run --no-project python -m unittest discover -s tests` で結合テストだけを実行できる。

kit-new が作った一時リポジトリを使う。
ssh・scp・sudo・DB・サービス・計測のコマンドを偽物へ差し替える。
偽サーバーは tests/ の一時ディレクトリに置く。
/etc への反映は同じ一時領域へ写す。
実際の ssh・sudo・AWS・ポータルは呼ばない。

SCOREBOARD は実際の noise で読む。
manifest の削除と退避は実際のファイルで確かめる。
生のログと pcap が git に入らないことも確かめる。
