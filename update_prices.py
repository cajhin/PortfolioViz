#!/usr/bin/env python3
"""Fetch daily price history into gen_prices/, driven by registry/price_sources.csv.

Nothing about *how* to fetch an instrument lives in the fetched file any more — the registry is
the single place that maps an instrument to a source, a symbol and a quote currency, so you can
answer "what am I tracking, and from where?" by reading one table instead of opening every series.

    python3 update_prices.py                       # update every instrument in the registry
    python3 update_prices.py roche                 # just this one (slug or id)
    python3 update_prices.py roche --from 2019-01-01   # also backfill, from that date

One row per instrument, keyed by its registry `id` — not by ISIN, since a synthetic instrument
(a benchmark, a second listing kept as its own row for a thin ISIN) may not have one. If a listing
needs a second source, register it as a second instrument (see registry/instruments.csv) rather
than adding a fallback row here; nothing here picks between two sources for one instrument.

Prices are converted to the portfolio currency on write, through the `fx_symbol` the registry
names, and the untouched quote is kept alongside in `close_raw`. Converting here rather than in
the browser keeps the page's arithmetic single-currency, and keeping the raw means a bad FX day
can be recomputed rather than re-fetched.

Written per instrument:  gen_prices/<id>-<slug>.csv   date,close,close_raw,quote_currency,source
Written once per run:    gen_prices/_latest.csv       id,date,close,source
FX series are cached in  gen_fx/<PAIR>.csv            date,rate
"""
import csv, io, json, os, subprocess, sys, time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(ROOT, "gen_prices")
FX_DIR = os.path.join(ROOT, "gen_fx")
REGISTRY = os.path.join(ROOT, "registry")
UA = "Mozilla/5.0"
FALLBACK_START = "2019-08-20"
PLACEHOLDER_RUN = 5   # a shorter identical run is coincidence, not a dormant listing


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
    quote = result[0]["indicators"]["quote"][0]
    stamps = result[0].get("timestamp") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or [None] * len(closes)
    rows = [(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), round(c, 6), v)
            for t, c, v in zip(stamps, closes, volumes) if c is not None]
    return {d: c for d, c, _ in drop_placeholder_lead(rows, symbol)}


def drop_placeholder_lead(rows, symbol=""):
    """Discard the run of quotes Yahoo carries forward before a listing actually starts trading.

    A dormant listing does not return nothing — it returns its last known quote, every day, on
    zero volume. SK Hynix's old Frankfurt line (HY9H.F) reported an identical 17.60 for 509
    straight trading days before real trading began in January 2021, which is not 509 observations.

    Both halves of the test carry weight. Zero volume alone would throw away every FX series,
    since Yahoo reports no volume for those at all — but a rate moves every day, so its run is one
    row long and never reaches PLACEHOLDER_RUN. Requiring the quote to be unchanged *as well*
    keeps the rule pointed at genuinely dead data. Only a *leading* run qualifies: a flat stretch
    later on is a real, if illiquid, market.
    """
    if not rows:
        return rows
    first_close = rows[0][1]
    n = 0
    while n < len(rows) and not rows[n][2] and rows[n][1] == first_close:
        n += 1
    if n < PLACEHOLDER_RUN:
        return rows
    resumes = f"before {rows[n][0]}" if n < len(rows) else "— the whole span is dormant"
    print(f"  {symbol}: dropped {n} placeholder rows {resumes} (no volume, quote never moved)")
    return rows[n:]


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


def update_one(inst, src, backfill=None):
    iid, slug = inst["id"], inst["slug"]
    path = os.path.join(DIR, f"{iid}-{slug}.csv" if slug else f"{iid}.csv")
    rows = read_series(path)

    symbol, ccy = src["symbol"].strip(), (src["quote_currency"] or portfolio_currency()).strip()
    if src["source"] != "yahoo" or not symbol:
        # manual (or unquotable, e.g. an expired warrant) — nothing to fetch, keep what's on disk
        if not rows:
            print(f"  {iid} [{slug}]: nothing stored")
            return None
        last = rows[max(rows)]
        return {"id": iid, "date": last["date"], "close": last["close"],
               "source": last.get("source", "")}

    have_from = max(rows) if rows else None
    # a --from later than what is already stored is not a real backfill request — keep updating
    # the tail instead of jumping the start date forward and silently truncating older history
    start = have_from if (backfill and have_from and backfill > have_from) \
        else backfill or have_from or FALLBACK_START
    try:
        fresh = fetch(symbol, as_stamp(start))
    except LookupError as err:
        print(f"  {iid} [{symbol}]: {err}")
        fresh = {}

    rates = fx_series(src["fx_symbol"], timeline_start()) if src["fx_symbol"] else None
    added = 0
    for date, raw in fresh.items():
        close = raw if ccy == portfolio_currency() else to_portfolio_ccy(raw, ccy, rates, date)
        if close is None:
            continue
        if date not in rows:
            added += 1
        rows[date] = {"date": date, "close": close, "close_raw": raw,
                      "quote_currency": ccy, "source": symbol}

    if not rows:
        print(f"  {iid} [{slug}]: nothing stored")
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
    print(f"  {iid} [{slug}]: {len(rows)} rows, {span} (+{added} new)")
    last = rows[max(rows)]
    return {"id": iid, "date": last["date"], "close": last["close"], "source": last["source"]}


def write_latest(latest):
    """The freshest close per instrument — what the page reads instead of a hand-kept price file."""
    path = os.path.join(DIR, "_latest.csv")
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "date", "close", "source"])
        w.writeheader()
        w.writerows(sorted(latest, key=lambda r: r["id"]))
    print(f"{path}: {len(latest)} instruments")


def main():
    args = sys.argv[1:]
    backfill = None
    if "--from" in args:
        i = args.index("--from")
        backfill = args[i + 1]
        del args[i:i + 2]
    if len(args) > 1:
        sys.exit(f"usage: {sys.argv[0]} [<slug|id>] [--from YYYY-MM-DD]")

    instruments = {r["id"]: r for r in read_registry("instruments.csv")}
    sources = {}
    for s in read_registry("price_sources.csv"):
        if s["id"] in sources:
            sys.exit(f"registry/price_sources.csv: duplicate row for {s['id']!r} — "
                     f"one row per instrument now; register a second instrument instead")
        sources[s["id"]] = s

    wanted = list(instruments.values())
    if args:
        key = args[0].removesuffix(".csv")
        wanted = [i for i in instruments.values() if key in (i["id"], i["slug"])]
        if not wanted:
            sys.exit(f"no instrument matching {key!r} in registry/instruments.csv")

    latest = []
    for inst in wanted:
        src = sources.get(inst["id"])
        if not src:
            continue                                 # cash, or anything with nothing to fetch
        row = update_one(inst, src, backfill)
        if row:
            latest.append(row)
    if not args and latest:
        write_latest(latest)


if __name__ == "__main__":
    main()
