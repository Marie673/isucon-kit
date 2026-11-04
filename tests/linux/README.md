# ローカル Linux の通し

競技サーバーや AWS は使わない。
専用の Ubuntu コンテナだけで実行する。
Podman の VM が稼働し、イメージを取得できることを前提にする。

```sh
podman build -t localhost/isu-kit-review-linux -f tests/linux/Containerfile tests/linux
podman run -d --name isu-kit-review-test --privileged --systemd=always --tmpfs /run --tmpfs /run/lock -p 127.0.0.1::22 localhost/isu-kit-review-linux
uv run --script tests/linux/run.py
podman stop isu-kit-review-test
```

sshd・sudo・nginx・MySQL・sysstat を使う。
bootstrap → keys → setup → backup → inventory → import → doctor を通す。
bench start → curl の負荷 → finish → deploy で SUMMARY を確かめる。
CPU の表・プロセス別 CPU・alp・窓・Profile を確認する。
新規の /etc ファイルが root 所有であることを確かめる。
capture は1MiBで止め、先頭の要求を残して別の scp で回収する。
上限の末尾が不完全でも、先頭の HTTP を復元できることを確かめる。

作業用の鍵・結果・競技リポジトリは tests/linux-work/ に置く。
git には入れない。
通常の kit-test はコンテナを作らない。

perf は VM のカーネルに合う実行ファイルが必要。
使えなければ perf.out と SUMMARY に警告を残す。
tcpdump にはコンテナ内の権限が必要。
