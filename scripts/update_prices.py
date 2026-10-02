#!/usr/bin/env python3
"""Fetch daily price history into gen_prices/, driven by registry/price_sources.csv.

Nothing about *how* to fetch an instrument lives in the fetched file any more — the registry is
the single place that maps an instrument to a source, a symbol and a quote currency, so you can
answer "what am I tracking, and from where?" by reading one table instead of opening every series.

    python3 scripts/update_prices.py                       # update every instrument in the registry
    python3 scripts/update_prices.py --profile main        # just what that profile holds/watches/benchmarks
    python3 scripts/update_prices.py roche                 # just this one (slug or id)
    python3 scripts/update_prices.py roche --from 2019-01-01   # also backfill, from that date

The registry and gen_prices/ are shared by every profile, so --profile narrows the run rather
than redirecting it: the instruments in private-profiles/<name>/'s positions and activities (open
and closed alike — a closed position's series keeps extending), its profile.json watchlist, and
its benchmark. _latest.csv is then merged into, not rewritten, so other profiles' rows survive.

One row per instrument, keyed by its registry `id` — not by ISIN, since a synthetic instrument
(a benchmark, a second listing kept as its own row for a thin ISIN) may not have one. If a listing
needs a second source, register it as a second instrument (see registry/instruments.csv) rather
than adding a fallback row here; nothing here picks between two sources for one instrument.

Prices are converted to the portfolio currency on write, through the `fx_symbol` the registry
names, and the untouched quote is kept alongside in `close_raw`. Converting here rather than in
the browser keeps the page's arithmetic single-currency, and keeping the raw means a bad FX day
can be recomputed rather than re-fetched.

A live price fresher than any close is kept apart from the closes, in _live.csv (see live_price):
a US stock's pre-market price from Yahoo, a European one's gettex mid while its home session is
open (Yahoo's European prices run ~15 minutes late). It is never written into a series — a
pre-market price is not a close — and the next run outside those windows drops it again.

Written per instrument:  gen_prices/<id>-<slug>.csv   date,close,close_raw,quote_currency,source
Written once per run:    gen_prices/_latest.csv       id,date,close,source
                         gen_prices/_live.csv         id,date,at,price,price_raw,quote_currency,source
FX series are cached in  gen_fx/<PAIR>.csv            date,rate
"""
import csv, io, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)                  # the repo — this file lives in scripts/
PROFILES = os.path.join(ROOT, "private-profiles")
DIR = os.path.join(ROOT, "gen_prices")
FX_DIR = os.path.join(ROOT, "gen_fx")
REGISTRY = os.path.join(ROOT, "registry")
UA = "Mozilla/5.0"
FALLBACK_START = "2019-08-20"
PLACEHOLDER_RUN = 5   # a shorter identical run is coincidence, not a dormant listing
MAX_WORKERS = 4


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


def profile_instruments(name):
    """Every id a profile has any use for a series of: held ever, watched, or benchmarked."""
    d = os.path.join(PROFILES, name)
    if not os.path.isfile(os.path.join(d, "positions.csv")):
        sys.exit(f"no profile {name!r} — expected {d}/positions.csv")
    ids = set()
    for f in ("positions.csv", "activities.csv"):
        path = os.path.join(d, f)
        if os.path.exists(path):
            with open(path) as fh:
                ids |= {r["identifier"] for r in csv.DictReader(fh) if r.get("identifier")}
    try:
        with open(os.path.join(d, "profile.json")) as fh:
            own = json.load(fh)
    except (OSError, ValueError):
        own = {}
    ids |= set(own.get("watchlist") or [])
    bench = own.get("benchmarkIsin") or config().get("benchmarkIsin")
    if bench:
        ids.add(bench)
    return ids


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


def chart(symbol, query):
    """Yahoo's chart answer for a symbol: its one result, meta and bars."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"
    out = subprocess.run(["curl", "-s", "--max-time", "40", "-H", f"User-Agent: {UA}", url],
                         capture_output=True, text=True).stdout
    try:
        result = (json.loads(out).get("chart") or {}).get("result")
    except ValueError:
        result = None
    if not result:
        raise LookupError(f"no data for {symbol} — response was {out[:160]!r}")
    return result[0]


def fetch(symbol, since):
    return fetch_with_meta(symbol, since)[0]


def fetch_with_meta(symbol, since):
    """Daily closes by date, and Yahoo's meta for the listing (its sessions, among others)."""
    result = chart(symbol, f"period1={since}&period2={int(time.time())}&interval=1d")
    quote = result["indicators"]["quote"][0]
    stamps = result.get("timestamp") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or [None] * len(closes)
    rows = [(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), round(c, 6), v)
            for t, c, v in zip(stamps, closes, volumes) if c is not None]
    return {d: c for d, c, _ in drop_placeholder_lead(rows, symbol)}, result.get("meta") or {}


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


GETTEX_MAX_AGE_MIN = 15                   # a market maker's quote is re-stamped only when it changes
PRE_MARKET_MAX_AGE_MIN = 30               # a thin pre-market can go quiet for a while


def gettex_quote(isin):
    """gettex's bid and ask for an ISIN, with the age of the quote — or None when onvista has none.
    One venue's quote, picked by its market code (_TRO), from onvista's unofficial API; the STOCK
    path serves funds and ETFs too, and an unknown market answers 403."""
    url = f"https://api.onvista.de/api/v1/instruments/STOCK/ISIN:{isin}/quote?codeMarket=_TRO"
    out = subprocess.run(["curl", "-sf", "--max-time", "15", "-H", f"User-Agent: {UA}", url],
                         capture_output=True, text=True).stdout
    try:
        q = json.loads(out)
        bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
    except (ValueError, TypeError, AttributeError):
        return None
    if (q.get("market") or {}).get("name") != "gettex":
        return None
    stamp = max(q.get("datetimeBid") or "", q.get("datetimeAsk") or "")
    if not (bid > 0 and ask >= bid and stamp) or q.get("isoCurrency") != "EUR":
        return None
    at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - at).total_seconds() / 60
    return {"bid": bid, "ask": ask, "age_min": round(age, 1), "at": at,
            "spread_pct": round((ask - bid) / ((ask + bid) / 2) * 100, 2)}


def live_price(inst, src, meta, rates):
    """A price fresher than the stored closes, or None when the closes are as fresh as it gets.

    A US listing in its pre-market (Yahoo's 1-minute bars with includePrePost): its daily bars
    have nothing for today until the regular session opens. A European listing in its regular
    session: gettex's mid, live where Yahoo's price for it is ~15 minutes old. Outside those
    windows the newest close is the price, and there is no live row. gettex quotes in euros, so
    it stands in only for a euro portfolio."""
    now = time.time()
    period = meta.get("currentTradingPeriod") or {}
    inside = lambda k: bool(period.get(k)) and period[k]["start"] <= now < period[k]["end"]
    symbol, ccy = src["symbol"].strip(), (src["quote_currency"] or portfolio_currency()).strip()
    if meta.get("hasPrePostMarketData") and inside("pre"):
        r = chart(symbol, "interval=1m&range=1d&includePrePost=true")
        bars = [(t, c) for t, c in zip(r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or [])
                if c is not None and t >= period["pre"]["start"]]
        if not bars or now - bars[-1][0] > PRE_MARKET_MAX_AGE_MIN * 60:
            return None
        at, raw = datetime.fromtimestamp(bars[-1][0], timezone.utc), round(bars[-1][1], 6)
        day = at.strftime("%Y-%m-%d")
        price = raw if ccy == portfolio_currency() else to_portfolio_ccy(raw, ccy, rates, day)
        source = f"{symbol} pre-market"
    elif (meta.get("exchangeTimezoneName") or "").startswith("Europe/") and inside("regular") \
            and portfolio_currency() == "EUR" and inst.get("isin"):
        g = gettex_quote(inst["isin"])
        if not g or g["age_min"] > GETTEX_MAX_AGE_MIN:
            return None
        at, raw, ccy, source = g["at"], round((g["bid"] + g["ask"]) / 2, 6), "EUR", "gettex mid"
        price = raw
    else:
        return None
    if price is None:
        return None
    return {"id": inst["id"], "date": at.strftime("%Y-%m-%d"), "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price": price, "price_raw": raw, "quote_currency": ccy, "source": source}


def update_one(inst, src, backfill=None):
    iid, slug = inst["id"], inst["slug"]
    path = os.path.join(DIR, f"{iid}-{slug}.csv" if slug else f"{iid}.csv")
    rows = read_series(path)

    symbol, ccy = src["symbol"].strip(), (src["quote_currency"] or portfolio_currency()).strip()

    have_from = max(rows) if rows else None
    # a --from later than what is already stored is not a real backfill request — keep updating
    # the tail instead of jumping the start date forward and silently truncating older history
    start = have_from if (backfill and have_from and backfill > have_from) \
        else backfill or have_from or FALLBACK_START
    try:
        fresh, meta = fetch_with_meta(symbol, as_stamp(start))
    except LookupError as err:
        print(f"  {iid} [{symbol}]: {err}")
        fresh, meta = {}, {}

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
    try:
        live = live_price(inst, src, meta, rates) if meta else None
    except (LookupError, KeyError, IndexError, TypeError) as err:
        print(f"  {iid} [{symbol}]: no live price — {err}")
        live = None
    if live:
        print(f"  {iid} [{slug}]: live {live['price']} ({live['source']}, {live['at']})")
    return {"id": iid, "date": last["date"], "close": last["close"], "source": last["source"]}, live


def write_latest(latest, merge=False):
    """The freshest close per instrument — what the page reads instead of a hand-kept price file.

    merge keeps the rows of instruments this run did not touch — a --profile run covers only a
    slice of the registry, and the other profiles still read this same file."""
    path = os.path.join(DIR, "_latest.csv")
    if merge and os.path.exists(path):
        with open(path) as fh:
            kept = {r["id"]: r for r in csv.DictReader(fh) if r.get("id")}
        kept.update({r["id"]: r for r in latest})
        latest = list(kept.values())
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "date", "close", "source"])
        w.writeheader()
        w.writerows(sorted(latest, key=lambda r: r["id"]))
    print(f"{path}: {len(latest)} instruments")


def write_live(live, done, merge=False):
    """The live price per instrument, where there is one fresher than its close (see live_price).

    Every instrument this run fetched (`done`) either gets its new row or loses its old one — a
    pre-market price must not outlive the session that superseded it. With merge, instruments
    this run did not touch keep theirs, as in write_latest."""
    path = os.path.join(DIR, "_live.csv")
    kept = {}
    if merge and os.path.exists(path):
        with open(path) as fh:
            kept = {r["id"]: r for r in csv.DictReader(fh) if r.get("id") and r["id"] not in done}
    kept.update({r["id"]: r for r in live})
    fields = ["id", "date", "at", "price", "price_raw", "quote_currency", "source"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(kept.values(), key=lambda r: r["id"]))
    print(f"{path}: {len(kept)} live prices")


def main():
    args = sys.argv[1:]
    backfill = None
    if "--from" in args:
        i = args.index("--from")
        backfill = args[i + 1]
        del args[i:i + 2]
    profile = None
    if "--profile" in args:
        i = args.index("--profile")
        profile = args[i + 1]
        del args[i:i + 2]
    if len(args) > 1 or (args and profile):
        sys.exit(f"usage: {sys.argv[0]} [<slug|id> | --profile NAME] [--from YYYY-MM-DD]")

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
    if profile:
        relevant = profile_instruments(profile)
        wanted = [i for i in instruments.values() if i["id"] in relevant or i["isin"] in relevant]
        print(f"profile {profile}: {len(wanted)} of {len(instruments)} instruments")

    # "manual" with no symbol is the registry's way of marking an instrument dead — an expired
    # warrant, mainly — nothing to fetch, so it never earns a place in the pool or the log
    jobs, dead = [], []
    for inst in wanted:
        src = sources.get(inst["id"])
        if not src:
            continue
        (jobs if src["source"] == "yahoo" and src["symbol"] else dead).append((inst, src))
    for inst, src in dead:
        print(f"  {inst['id']} [{inst['slug']}]: {src['note'] or 'manual, no symbol'} — skipped")

    # every fx pair the jobs below will need, fetched here and not in the pool: fx_series()
    # caches in memory and rewrites gen_fx/<PAIR>.csv on every call, and two worker threads
    # racing the same pair would duplicate the fetch and could interleave the write
    for pair in sorted({src["fx_symbol"] for _, src in jobs if src["fx_symbol"]}):
        fx_series(pair, timeline_start())

    latest, live = [], []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(update_one, inst, src, backfill) for inst, src in jobs]
        for future in as_completed(futures):
            row, now = future.result() or (None, None)
            if row:
                latest.append(row)
            if now:
                live.append(now)
    # a partial run — one profile, or one instrument — merges into _latest.csv rather than
    # replacing it, so every other instrument keeps its row
    if latest:
        write_latest(latest, merge=bool(profile or args))
    write_live(live, {inst["id"] for inst, _ in jobs}, merge=bool(profile or args))


if __name__ == "__main__":
    main()
