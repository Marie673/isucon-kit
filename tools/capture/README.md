# capture

利用者の要求を時刻順に並べる。
標準ライブラリだけで pcap の TCP を組み直し、HTTP/1.1 の要求と応答を対にする。

```sh
tools/capture/capture pcap input.pcap --out results/run/host/http.jsonl
tools/capture/capture flows results/run/host/http.jsonl --user-key cookie --groups conf/alp-groups.txt
tools/capture/capture flows results/run/host/access.log --user-key auth
```

flows は認証情報をハッシュにする。
クエリ文字列を要約に出さない。
動線はきっかけからの相対時刻と要求間の間隔で読む。
認証情報の更新で利用者のハッシュも変わるので、必要なら connection・host などを指定する。

pcap はリンク種別0・1・113・276に対応する。
再送・逆順・重なりを除き、欠落の先をつなげない。
本文は4096バイトまでにする。
gzip と chunked に対応する。
TLS・HTTP/2・IP の断片化・IPv6 の拡張ヘッダー・pcapng は対象外。
欠落した接続は完全に復元できないことがある。
完全な証跡が要るときはアプリの記録と照らす。
生の JSONL には認証情報がある。git に入れない。

テストは `uv run --no-project python -m unittest discover -s tools/capture/tests`。
合成した pcap だけを使う。
