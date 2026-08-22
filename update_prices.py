#!/usr/bin/env python3
"""Fetch daily price history into prices/, driven by registry/price_sources.csv.

Nothing about *how* to fetch an instrument lives in the fetched file any more — the registry is
the single place that maps an ISIN to a source, a symbol and a quote currency, so you can answer
"what am I tracking, and from where?" by reading one table instead of opening every series.

    python3 update_prices.py                       # update every instrument in the registry
    python3 update_prices.py roche                 # just this one (slug or ISIN)
    python3 update_prices.py roche --from 2019-01-01   # also backfill, from that date

One instrument may list several sources, ordered by `priority`. Priority 1 is the truth; a lower
one is only ever consulted for dates the higher one does not have. That is what makes a thin
listing usable — Roche's RHO.DE has a five-year hole that RO.SW fills — and it is why every row
records the symbol it came from, so a filled stretch stays visible as such.

Prices are converted to the portfolio currency on write, through the `fx_symbol` the registry
names, and the untouched quote is kept alongside in `close_raw`. Converting here rather than in
the browser keeps the page's arithmetic single-currency, and keeping the raw means a bad FX day
can be recomputed rather than re-fetched.

Written per instrument:  prices/<isin>-<slug>.csv   date,close,close_raw,quote_currency,source
Written once per run:    prices/_latest.csv         isin,date,close,source
FX series are cached in  fx/<PAIR>.csv                   date,rate
"""
import csv, io, json, os, subprocess, sys, time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(ROOT, "prices")
FX_DIR = os.path.join(ROOT, "fx")
REGISTRY = os.path.join(ROOT, "registry")
UA = "Mozilla/5.0"
FALLBACK_START = "2019-08-20"


def config():
    try:
        with open(os.path.join(ROOT, "config.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def timeline_start():
    # config.json is maintained by agents, not this script — fall back quietly if it is missing
    # or malformed rather than block a data refresh over a settings file
    return config().get("timelineStart") or FALLBACK_START


def portfolio_currency():
    return config().get("currency") or "EUR"


def read_registry(name):
    path = os.path.join(REGISTRY, name)
    if not os.path.exists(path):
        sys.exit(f"missing {path} — the registry is the source of truth for what to fetch")
    with open(path) as fh:
        return list(csv.DictReader(fh))


def read_series(path):
    """Existing rows as {date: row}. Tolerates the old two-column format."""
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        body = "".join(l for l in fh if not l.startswith("#"))
    return {r["date"]: r for r in csv.DictReader(io.StringIO(body)) if r.get("date")}


def as_stamp(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def fetch(symbol, since):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{symbol}?period1={since}&period2={int(time.time())}&interval=1d")
    out = subprocess.run(["curl", "-s", "--max-time", "40", "-H", f"User-Agent: {UA}", url],
                         capture_output=True, text=True).stdout
    try:
        result = (json.loads(out).get("chart") or {}).get("result")
    except ValueError:
        result = None
    if not result:
        raise LookupError(f"no data for {symbol} — response was {out[:160]!r}")
    stamps = result[0].get("timestamp") or []
    closes = result[0]["indicators"]["quote"][0].get("close") or []
    return {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"): round(c, 6)
            for t, c in zip(stamps, closes) if c is not None}


FX_CACHE = {}


def fx_series(pair, since):
    """Daily rates for a Yahoo FX symbol (EURUSD=X), cached on disk and in memory."""
    if pair in FX_CACHE:
        return FX_CACHE[pair]
    os.makedirs(FX_DIR, exist_ok=True)
    path = os.path.join(FX_DIR, pair.replace("=X", "") + ".csv")
    rows = {d: float(r["rate"]) for d, r in read_series(path).items()}
    start = max(rows) if rows else since
    try:
        rows.update(fetch(pair, as_stamp(start)))
    except LookupError as err:
        print(f"  fx {pair}: {err}")
    if rows:
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "rate"])
            for d in sorted(rows):
                w.writerow([d, rows[d]])
    FX_CACHE[pair] = rows
    return rows


def rate_at(rates, date):
    """Last rate at or before `date` — FX and equity calendars do not line up exactly."""
    if not rates:
        return None
    if date in rates:
        return rates[date]
    earlier = [d for d in rates if d <= date]
    return rates[max(earlier)] if earlier else None


def to_portfolio_ccy(close, quote_ccy, rates, date):
    """Quote → portfolio currency. GBp is pence, a hundredth of the GBP the pair quotes."""
    if not rates:
        return None
    rate = rate_at(rates, date)
    if not rate:
        return None
    if quote_ccy == "GBp":
        close = close / 100.0
    return round(close / rate, 6)


def update_one(inst, srcs, backfill=None):
    isin, slug = inst["id"], inst["slug"]
    path = os.path.join(DIR, f"{isin}-{slug}.csv" if slug else f"{isin}.csv")
    rows = read_series(path)
    have_from = {}                                   # symbol -> latest date already stored for it
    for d, r in rows.items():
        s = r.get("source", "")
        have_from[s] = max(have_from.get(s, ""), d)
    base = portfolio_currency()
    added = filled = 0

    for src in sorted(srcs, key=lambda s: int(s["priority"])):
        symbol, ccy = src["symbol"].strip(), (src["quote_currency"] or base).strip()
        if src["source"] != "yahoo" or not symbol:
            continue                                 # manual (or unquotable) — leave its rows be
        first = int(src["priority"]) == 1
        # the primary only needs its tail; a fallback exists to fill holes, so it reads the whole
        # span the first time and only its tail once we already hold rows from it
        start = backfill or have_from.get(symbol) or (timeline_start() if not first
                                                      else FALLBACK_START)
        if backfill and have_from.get(symbol) and backfill > have_from[symbol]:
            start = have_from[symbol]
        try:
            fresh = fetch(symbol, as_stamp(start))
        except LookupError as err:
            print(f"  {isin} [{symbol}]: {err}")
            continue
        rates = fx_series(src["fx_symbol"], timeline_start()) if src["fx_symbol"] else None
        for date, raw in fresh.items():
            close = raw if ccy == base else to_portfolio_ccy(raw, ccy, rates, date)
            if close is None:
                continue
            prior = rows.get(date)
            if prior and prior.get("source") not in (None, "", symbol):
                # a higher-priority symbol already answered for this day; never overwrite it
                if prior["source"] in [s["symbol"] for s in srcs
                                       if int(s["priority"]) < int(src["priority"])]:
                    continue
            if not prior:
                added += 1
                if not first:
                    filled += 1
            rows[date] = {"date": date, "close": close, "close_raw": raw,
                          "quote_currency": ccy, "source": symbol}

    if not rows:
        print(f"  {isin} [{slug}]: nothing stored")
        return None
    os.makedirs(DIR, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "close", "close_raw", "quote_currency", "source"])
        w.writeheader()
        for date in sorted(rows):
            r = rows[date]
            w.writerow({k: r.get(k, "") for k in
                        ["date", "close", "close_raw", "quote_currency", "source"]})
    span = f"{min(rows)} → {max(rows)}"
    note = f", {filled} filled from a fallback" if filled else ""
    print(f"  {isin} [{slug}]: {len(rows)} rows, {span} (+{added} new{note})")
    last = rows[max(rows)]
    return {"isin": isin, "date": last["date"], "close": last["close"], "source": last["source"]}


def write_latest(latest):
    """The freshest close per instrument — what the page reads instead of a hand-kept price file."""
    path = os.path.join(DIR, "_latest.csv")
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["isin", "date", "close", "source"])
        w.writeheader()
        w.writerows(sorted(latest, key=lambda r: r["isin"]))
    print(f"{path}: {len(latest)} instruments")


def main():
    args = sys.argv[1:]
    backfill = None
    if "--from" in args:
        i = args.index("--from")
        backfill = args[i + 1]
        del args[i:i + 2]
    if len(args) > 1:
        sys.exit(f"usage: {sys.argv[0]} [<slug|isin>] [--from YYYY-MM-DD]")

    instruments = {r["id"]: r for r in read_registry("instruments.csv")}
    sources = {}
    for s in read_registry("price_sources.csv"):
        sources.setdefault(s["isin"], []).append(s)

    wanted = list(instruments.values())
    if args:
        key = args[0].removesuffix(".csv")
        wanted = [i for i in instruments.values() if key in (i["id"], i["slug"])]
        if not wanted:
            sys.exit(f"no instrument matching {key!r} in registry/instruments.csv")

    latest = []
    for inst in wanted:
        srcs = sources.get(inst["id"])
        if not srcs:
            continue                                 # cash, or anything with nothing to fetch
        row = update_one(inst, srcs, backfill)
        if row:
            latest.append(row)
    if not args and latest:
        write_latest(latest)


if __name__ == "__main__":
    main()
