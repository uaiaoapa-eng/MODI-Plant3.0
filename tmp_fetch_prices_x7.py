"""Temporary helper: fetch 6 months of daily OHLCV for 000660 and 229200.

Runs on a GitHub Actions runner (open internet). Tries Naver siseJson first,
falls back to Yahoo chart API. Writes tmp_price_out/<code>.csv with header
date,open,high,low,close,volume and ISO dates ascending.
"""
import ast
import json
import re
import sys
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
START = "20260220"
END = "20260902"


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def from_naver(code):
    url = (
        "https://api.finance.naver.com/siseJson.naver?symbol=%s"
        "&requestType=1&startTime=%s&endTime=%s&timeframe=day" % (code, START, END)
    )
    txt = get(url)
    txt = re.sub(r"\s+", " ", txt).strip()
    data = ast.literal_eval(txt)
    rows = []
    for row in data[1:]:
        d = str(row[0])
        rows.append(
            (
                "%s-%s-%s" % (d[0:4], d[4:6], d[6:8]),
                int(round(float(row[1]))),
                int(round(float(row[2]))),
                int(round(float(row[3]))),
                int(round(float(row[4]))),
                int(round(float(row[5]))),
            )
        )
    return rows


def from_yahoo(code):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/%s.KS"
        "?range=7mo&interval=1d" % code
    )
    j = json.loads(get(url))
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    import datetime

    rows = []
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        d = datetime.datetime.utcfromtimestamp(t + 9 * 3600).strftime("%Y-%m-%d")
        rows.append(
            (
                d,
                int(round(q["open"][i])),
                int(round(q["high"][i])),
                int(round(q["low"][i])),
                int(round(q["close"][i])),
                int(round(q["volume"][i])),
            )
        )
    return rows


def main():
    import os

    os.makedirs("tmp_price_out", exist_ok=True)
    for code in ("000660", "229200"):
        rows = None
        errors = []
        for fn in (from_naver, from_yahoo):
            try:
                rows = fn(code)
                if rows:
                    print("%s: %d rows via %s" % (code, len(rows), fn.__name__))
                    break
            except Exception as e:  # noqa: BLE001
                errors.append("%s: %r" % (fn.__name__, e))
                rows = None
        if not rows:
            print("FAILED %s: %s" % (code, "; ".join(errors)))
            sys.exit(1)
        rows = [r for r in rows if "2026-02-28" <= r[0] <= "2026-09-02"]
        rows.sort(key=lambda r: r[0])
        with open("tmp_price_out/%s.csv" % code, "w") as f:
            f.write("date,open,high,low,close,volume\n")
            for r in rows:
                f.write(",".join(str(x) for x in r) + "\n")


if __name__ == "__main__":
    main()
