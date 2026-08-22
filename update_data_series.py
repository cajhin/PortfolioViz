#!/usr/bin/env python3
"""Extend daily price series in data_series/ with the closes that are missing.

A series is a CSV of `date,close` rows under a "# key=value" header that names the Yahoo symbol.
The script asks Yahoo only for the days from the last stored close onward and merges. The overlap
day is refetched on purpose: the newest row may have been an intraday value when it was written.

    python3 update_data_series.py                              # update every series in data_series/
    python3 update_data_series.py benchmark                    # update just this one series
    python3 update_data_series.py benchmark --from 2019-01-01  # also backfill, from that date

To start a new series, create data_series/<name>.csv with just the header:

    # symbol=<yahoo symbol>
    # name=<what it is>
    # currency=EUR
    date,close

A brand-new series with no --from given backfills from config.json's "timelineStart" (see that
file, at the repo root, for the current value — it's the same start date the as-of picker honours).
"""
import csv, io, json, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone

DIR = "data_series"
UA = "Mozilla/5.0"


def timeline_start():
    # config.json is maintained by agents, not this script — fall back quietly if it is missing
    # or malformed rather than block a data refresh over a settings file
    try:
        with open("config.json") as fh:
            return json.load(fh).get("timelineStart") or "2019-08-20"
    except (OSError, ValueError):
        return "2019-08-20"


def read_file(path):
    meta, body = {}, []
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                key, _, value = line[1:].partition("=")
                meta[key.strip()] = value.strip()
            else:
                body.append(line)
    rows = {r["date"]: r["close"] for r in csv.DictReader(io.StringIO("".join(body)))}
    return meta, rows


def fetch(symbol, since):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{symbol}?period1={since}&period2={int(time.time())}&interval=1d")
    out = subprocess.run(["curl", "-s", "--max-time", "40", "-H", f"User-Agent: {UA}", url],
                         capture_output=True, text=True).stdout
    result = (json.loads(out).get("chart") or {}).get("result")
    if not result:
        sys.exit(f"no data for {symbol} — response was {out[:200]!r}")
    stamps = result[0]["timestamp"]
    closes = result[0]["indicators"]["quote"][0]["close"]
    return {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"): round(c, 4)
            for t, c in zip(stamps, closes) if c is not None}


def as_stamp(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def update_one(path, backfill=None):
    meta, rows = read_file(path)
    symbol = meta.get("symbol")
    if not symbol:
        print(f"{path}: skipped — no '# symbol=' header")
        return

    last = max(rows) if rows else timeline_start()
    # normally just the tail; with --from, everything back to that day
    start = backfill if backfill and (not rows or backfill < min(rows)) else last
    since = as_stamp(start)

    try:
        fresh = fetch(symbol, since)
    except SystemExit as err:
        print(f"{path}: {err}")
        return

    added = [d for d in fresh if d not in rows]
    changed = [d for d in fresh if d in rows and str(rows[d]) != str(fresh[d])]
    rows.update(fresh)

    with open(path, "w", newline="") as fh:
        for key in ("symbol", "name", "currency"):
            if key in meta:
                fh.write(f"# {key}={meta[key]}\n")
        writer = csv.writer(fh)
        writer.writerow(["date", "close"])
        for date in sorted(rows):
            writer.writerow([date, rows[date]])

    print(f"{path} [{symbol}]: {len(rows)} rows, {min(rows)} → {max(rows)} "
          f"(+{len(added)} new, {len(changed)} corrected; fetched from {start})")


def main():
    args = sys.argv[1:]
    backfill = None
    if "--from" in args:
        i = args.index("--from")
        backfill = args[i + 1]
        del args[i:i + 2]
    if len(args) > 1:
        sys.exit(f"usage: {sys.argv[0]} [<series>] [--from YYYY-MM-DD]   "
                 f"(series files live in {DIR}/; omit <series> to update all of them)")

    if not args:
        paths = sorted(f for f in os.listdir(DIR) if f.endswith(".csv")) if os.path.isdir(DIR) else []
        if not paths:
            sys.exit(f"no series found in {DIR}/")
        for name in paths:
            update_one(os.path.join(DIR, name), backfill)
        return

    path = os.path.join(DIR, args[0] if args[0].endswith(".csv") else args[0] + ".csv")
    if not os.path.exists(path):
        sys.exit(f"no such series: {path}")
    update_one(path, backfill)


if __name__ == "__main__":
    main()
