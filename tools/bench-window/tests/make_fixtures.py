"""tests/fixtures を作り直す。決まった乱数なので毎回同じ中身になる。

    uv run python tools/bench-window/tests/make_fixtures.py

想定: 2026-10-31 10:03:20 (+09:00) にベンチの受付開始。+0〜+35s は平常、+35〜+60s に利用者が一斉に動く山。
host1 は LTSV、host2 は JSON。CPU は host1 が mpstat、host2 が CSV。
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).parent / "fixtures"
JST = timezone(timedelta(hours=9))
START = datetime(2026, 10, 31, 10, 3, 20, tzinfo=JST)
RUN = 60  # 秒

ENDPOINTS = [  # (method, path 作り, 重み, 平常の中央値 ms, 山での倍率)
    ("GET", lambda r: f"/api/lists/{r.randint(1, 300)}?limit=20", 40, 12, 9.0),
    ("GET", lambda r: f"/api/items/{r.randint(1, 900)}", 30, 6, 1.5),
    ("GET", lambda r: f"/api/files/{r.randint(1, 900)}", 15, 4, 1.2),
    ("POST", lambda r: "/api/items", 8, 25, 6.0),
    ("POST", lambda r: f"/api/items/{r.randint(1, 900)}/notes", 7, 15, 4.0),
]


def gen(host_seed: int, base_rps: int, peak_rps: int):
    r = random.Random(host_seed)
    rows = []
    for sec in range(RUN):
        peak = sec >= 35
        n = (peak_rps if peak else base_rps) + r.randint(-1, 1)
        for _ in range(n):
            m, mk, _, med, mult = r.choices(ENDPOINTS, weights=[e[2] for e in ENDPOINTS])[0]
            t = START + timedelta(seconds=sec + r.random())
            rt = r.lognormvariate(0, 0.5) * med / 1000 * (mult if peak else 1.0)
            status = 200
            if peak and sec >= 50 and m == "GET" and "lists" in mk(r) and r.random() < 0.08:
                status = 503
            if peak and rt > 0.3 and r.random() < 0.1:
                status = 499
            rows.append((t, m, mk(r), status, round(rt, 3)))
    rows.sort(key=lambda x: x[0])
    return rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "host1.ltsv.log", "w") as f:
        for t, m, uri, st, rt in gen(1, 5, 14):
            f.write(f"time:{t.isoformat(timespec='seconds')}\thost:10.0.0.9\tmethod:{m}\turi:{uri}\tstatus:{st}"
                    f"\tsize:512\treqtime:{rt:.3f}\tapptime:{rt * 0.9:.3f}\n")
    with open(OUT / "host2.json.log", "w") as f:
        for t, m, uri, st, rt in gen(2, 4, 9):
            f.write(json.dumps({"time_iso8601": t.isoformat(timespec="seconds"), "remote_addr": "10.0.1.9",
                                "request_method": m, "request_uri": uri, "status": st, "request_time": rt,
                                "upstream_response_time": round(rt * 0.9, 3)}) + "\n")
    r = random.Random(3)
    with open(OUT / "host1.mpstat.txt", "w") as f:
        f.write("Linux 6.8.0-1015-aws (host1) \t10/31/2026 \t_x86_64_\t(4 CPU)\n\n")
        f.write("10:03:00 AM  CPU    %usr   %nice    %sys %iowait    %irq   %soft  %steal  %guest  %gnice   %idle\n")
        for s in range(-20, RUN + 10):
            t = START + timedelta(seconds=s)
            busy = 18 + r.random() * 6 if s < 35 else 70 + r.random() * 28
            if s < 0 or s >= RUN:
                busy = 3 + r.random() * 2
            idle = 100 - busy
            f.write(f"{t:%I:%M:%S %p}  all {busy * .7:7.2f}    0.00 {busy * .25:7.2f}    0.10    0.00 {busy * .05:7.2f}    0.00    0.00    0.00 {idle:7.2f}\n")
    with open(OUT / "host2.cpu.csv", "w") as f:
        f.write("ts,host,cpu\n")
        for s in range(0, RUN):
            t = START + timedelta(seconds=s)
            busy = 12 + r.random() * 5 if s < 35 else 40 + r.random() * 25
            f.write(f"{t.isoformat(timespec='seconds')},host2,{busy:.1f}\n")
    (OUT / "groups.txt").write_text(
        "# URI をまとめる正規表現（alp-groups.txt と同じ形。# から行末はコメント）\n"
        "^/api/lists/[0-9]+$\n^/api/items/[0-9]+$\n^/api/items/[0-9]+/notes$\n^/api/files/[0-9]+$\n")


if __name__ == "__main__":
    main()
