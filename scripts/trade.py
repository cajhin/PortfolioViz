#!/usr/bin/env python3
"""Trade a demo account from the command line — live only, JSON in every answer.

    trade.py quote    <instrument>
    trade.py buy      <account> <instrument> (--eur EUR | --shares N) [--reason TEXT] [--id ID]
    trade.py sell     <account> <instrument> (--shares N | --all)      [--reason TEXT] [--id ID]
    trade.py status   <account>
    trade.py history  <account>
    trade.py rules

Accounts themselves — creating one, putting cash in or taking it out — are their owner's to
manage, not this script's.

<instrument> is an ISIN, a ticker or a name; anything ambiguous answers with the candidates.
Every answer is one JSON object; on error {"ok": false, "error": ..., "hint": ...}, exit code 1.

Live only. There is no date to give: a buy or sell executes now, at a live price — on gettex
(weekdays 08:00–22:00 German time) a buy pays the ask and a sale gets the bid; otherwise at the
home exchange while it is open. `quote` shows what a buy and a sale would get right now, or why
no trade is possible. Each trade costs €10 + 1% of its value,
which makes trading in and out at every tick a losing game. Cash can never go below zero: a buy
must be covered, fee included. Prices are in the portfolio currency.

buy --eur is everything that leaves the account, the fee included (--eur 2000 buys €1970.30 of
shares and pays €29.70); buy --shares adds the fee on top. A sale's fee comes off its proceeds,
and so does a 20% tax on its gain, if any. The full rules, with examples: trade.py rules.

Only accounts set up for command-line trading can be traded; any other is refused. --id makes a
call safe to repeat: a second call with the same id answers with the first one's result instead of
trading twice. --reason is kept with the trade.
"""
import argparse, contextlib, csv, fcntl, io, json, os, re, subprocess, sys, time, urllib.parse, uuid
from datetime import date, datetime, timedelta, timezone

import import_tr
import manual_tx
from import_tr import num, read_csv, write_csv

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)                  # the repo — this file lives in scripts/
PROFILES = os.path.join(ROOT, "private-profiles")
ACCOUNT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class Refusal(Exception):
    """A request this script turns down — answered as {"ok": false, ...}, never as a traceback."""
    def __init__(self, error, hint=""):
        super().__init__(error)
        self.error, self.hint = error, hint


def quiet(fn, *args, **kw):
    """Call one of the other scripts' functions: their progress text would break the JSON on
    stdout, and their sys.exit("reason") is a refusal here, not the end of the process."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            return fn(*args, **kw)
    except SystemExit as e:
        raise Refusal(str(e.code) if e.code not in (None, 0) else "failed", out.getvalue().strip()[-300:])


# ---------- accounts ----------

def account_dir(name, must_exist=True):
    if not ACCOUNT.match(name or ""):
        raise Refusal(f"{name!r} is not an account name",
                      "lowercase letters, digits, - and _, starting with a letter or digit")
    d = os.path.join(PROFILES, name)
    if must_exist and not os.path.isdir(d):
        raise Refusal(f"no account {name!r}", "check the name — accounts are set up by their owner")
    return d


def profile(d):
    try:
        with open(os.path.join(d, "profile.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def require_cli(name):
    d = account_dir(name)
    cfg = profile(d)
    if cfg.get("source") != "manual" or cfg.get("allow-cli") is not True:
        raise Refusal(f"account {name!r} is not open to the command line",
                      "only accounts set up for command-line trading can be traded")
    return d


@contextlib.contextmanager
def locked(d):
    """One change at a time per account: two callers at once (two agents, or one and the page)
    would otherwise each read the ledger, add their row, and the second write lose the first."""
    with open(os.path.join(d, ".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def today():
    return date.today().isoformat()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- instruments ----------

def registry():
    rows = read_csv(os.path.join(ROOT, "registry", "instruments.csv"))
    sources = {r["id"]: r for r in read_csv(os.path.join(ROOT, "registry", "price_sources.csv"))}
    return rows, sources


def resolve(text, register=True):
    """An instrument from an ISIN, a ticker, or a name — the registry first. An ISIN the registry
    lacks is registered (name and symbol looked up, prices fetched) when `register` is set; a name
    it lacks is searched, and the candidates come back as the refusal's hint."""
    q = (text or "").strip()
    if not q:
        raise Refusal("no instrument given", "an ISIN, a ticker or a name")
    rows, sources = registry()
    key = q.lower()
    exact = [r for r in rows if key in (r["id"].lower(), r["isin"].lower(), r["slug"].lower(),
                                         (sources.get(r["id"]) or {}).get("symbol", "").lower(),
                                         r["display"].lower(), r["name"].lower())]
    if len(exact) == 1:
        return instrument(exact[0], sources)
    partial = [r for r in rows if key in r["display"].lower() or key in r["name"].lower()]
    if len(exact) > 1 or len(partial) > 1:
        cands = exact or partial
        raise Refusal(f"{q!r} matches several instruments",
                      "use one ISIN: " + "; ".join(f"{r['isin']} {r['display']}" for r in cands[:8]))
    if len(partial) == 1:
        return instrument(partial[0], sources)
    if ISIN.match(q.upper()):
        if not register:
            raise Refusal(f"{q.upper()} is not registered yet", "buying it registers it")
        return register_isin(q.upper())
    import add_instrument
    try:
        found = add_instrument.find(q)
    except Exception as err:
        raise Refusal(f"{q!r} is not a known instrument, and the search failed: {err}",
                      "use its ISIN")
    if not found:
        raise Refusal(f"nothing found for {q!r}", "use its ISIN")
    raise Refusal(f"{q!r} is not registered",
                  "use one ISIN (it is registered on first use): " +
                  "; ".join(f"{c['isin']} {c['name']} ({c['type']})" for c in found[:8]))


def register_isin(isin):
    import add_instrument
    try:
        listings = add_instrument.symbols(isin)
        found = add_instrument.find(isin)
    except Exception as err:
        raise Refusal(f"could not look {isin} up: {err}", "try again later")
    if not listings:
        raise Refusal(f"Yahoo lists no symbol for {isin}", "it cannot be priced, so it cannot be traded")
    name = (found[0]["name"] if found else "") or listings[0]["name"] or isin
    proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "add_instrument.py"), isin,
                           listings[0]["symbol"], name, "Other", "registered by trade.py on a first buy"],
                          capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        raise Refusal(f"could not register {isin}: {(proc.stderr or proc.stdout).strip()[-200:]}")
    rows, sources = registry()
    return instrument(next(r for r in rows if r["id"] == isin), sources, registered=True)


def instrument(row, sources, registered=False):
    src = sources.get(row["id"]) or {}
    if not src.get("symbol"):
        raise Refusal(f"{row['display']} has no price source", "it cannot be traded")
    out = {"isin": row["isin"] or row["id"], "id": row["id"], "slug": row["slug"],
           "name": row["display"] or row["name"], "symbol": src["symbol"]}
    if registered:
        out["registered"] = True
    return out


def series(inst):
    path = os.path.join(ROOT, "gen_prices", f"{inst['id']}-{inst['slug']}.csv")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        body = "".join(l for l in fh if not l.startswith("#"))
    return sorted(((r["date"], num(r["close"])) for r in csv.DictReader(io.StringIO(body))
                   if r.get("date") and num(r.get("close")) > 0))


def fetch_prices(inst):
    subprocess.run([sys.executable, os.path.join(SCRIPTS, "update_prices.py"), inst["id"]],
                   capture_output=True, text=True, timeout=180)


# ---------- trading ----------
# A buy or sell executes at once, at the latest close on file — fetched fresh first — and costs
# FEE_FIXED + FEE_RATE of the trade's value: enough to make trading in and out on every tick a
# losing game. Cash may never go below zero: a buy (fee included) or a withdrawal that would take
# it there is refused.

FEE_FIXED, FEE_RATE = 10.0, 0.01
TAX_RATE = 0.20          # of a sale's gain, if any — see sale_tax


def fee_for(value):
    return round(FEE_FIXED + FEE_RATE * value, 2)


def sale_tax(rows, isin, shares, value, fee):
    """Tax on selling `shares` for `value` less `fee`: TAX_RATE of the gain over what those shares
    cost, oldest bought first, their buy fees included. A loss pays nothing — and is not carried
    forward against a later gain. Returns (tax, gain)."""
    lots = []
    for r in sorted((r for r in rows if r["symbol"] == isin and r["type"] in ("BUY", "SELL")),
                    key=lambda r: r["datetime"]):
        n = abs(num(r["shares"]))
        if r["type"] == "BUY":
            lots.append([n, (-num(r["amount"]) - num(r["fee"])) / n])     # cost per share, fee in
            continue
        while n > 1e-9 and lots:
            take = min(n, lots[0][0])
            lots[0][0] -= take
            n -= take
            if lots[0][0] <= 1e-9:
                lots.pop(0)
    cost, left = 0.0, shares
    for n, per in lots:
        take = min(left, n)
        cost += take * per
        left -= take
        if left <= 1e-9:
            break
    gain = value - fee - cost
    return round(max(gain, 0) * TAX_RATE, 2), round(gain, 2)


def cash_of(d):
    """The account's cash now — every booking's effect, as the last rebuild wrote it."""
    path = os.path.join(d, "cash.csv")
    return round(sum(num(c["amount"]) for c in read_csv(path)), 2) if os.path.exists(path) else 0.0


def held(d, isin):
    """Shares of `isin` in the account now."""
    _, rows = manual_tx.manual_rows(d)
    return round(sum(num(r["shares"]) for r in rows if r["symbol"] == isin and r["type"] in ("BUY", "SELL")), 6)


# A trade needs a price nobody can see past. Outside the listing's regular session the latest price
# is a stale close while the instrument goes on moving elsewhere (after-hours, other venues, an ADR,
# overnight news) — an earnings gap would be a free win. So trades are refused unless the market is
# open, and unless the price is fresh: Yahoo delays some exchanges by ~15 minutes, so MAX_AGE leaves
# room for that and no more.
MAX_AGE_MIN = 30


def market(inst):
    """The listing's session and the age of its latest price, from Yahoo's own answer for it."""
    import add_instrument
    try:
        raw = add_instrument.yahoo("https://query1.finance.yahoo.com/v8/finance/chart/"
                                   + urllib.parse.quote(inst["symbol"], safe="") + "?interval=1d&range=1d")
        meta = raw["chart"]["result"][0]["meta"]
        reg = meta["currentTradingPeriod"]["regular"]
    except Exception as err:
        raise Refusal(f"could not ask Yahoo whether {inst['symbol']}'s market is open: {err}", "try again shortly")
    tz = timezone(timedelta(seconds=meta.get("gmtoffset") or 0))
    hhmm = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M")
    now = time.time()
    price_at = meta.get("regularMarketTime") or 0
    return {"exchange": meta.get("exchangeName") or "", "timezone": meta.get("exchangeTimezoneName") or "",
            "session": f"{hhmm(reg['start'])}–{hhmm(reg['end'])} {meta.get('exchangeTimezoneName') or ''}".strip(),
            "open": reg["start"] <= now < reg["end"],
            "price_age_min": round((now - price_at) / 60, 1) if price_at else None,
            "local_date": datetime.fromtimestamp(now, tz).date().isoformat()}


def tradable(inst):
    """Refuse unless the market is open and its price fresh; the market state when it is."""
    m = market(inst)
    if not m["open"]:
        raise Refusal(f"the market for {inst['name']} ({inst['symbol']}, {m['exchange']}) is closed",
                      f"its regular session is {m['session']}, on trading days; trades are taken only then")
    if m["price_age_min"] is None or m["price_age_min"] > MAX_AGE_MIN:
        raise Refusal(f"{inst['name']}'s latest price is {m['price_age_min']} minutes old",
                      f"trades need a price under {MAX_AGE_MIN} minutes old; try again shortly")
    return m


# Where a trade is priced. First choice: gettex (Munich), a venue open weekdays 08:00–22:00 German
# time that quotes European *and* US stocks and most ETFs live, in euros — a buy pays its ask, a sale
# gets its bid, so the spread is a real cost as at any broker. Its quotes come from onvista's API
# (unofficial). Fallback, when gettex is closed or has no fresh quote: the listing's home exchange
# via Yahoo, under the market-hours rule above.
GETTEX_HOURS = (8, 22)                    # local time, Mon–Fri
GETTEX_MAX_AGE_MIN = 15                   # a market maker's quote is re-stamped only when it changes
BERLIN = None


def gettex_open(now=None):
    global BERLIN
    if BERLIN is None:
        from zoneinfo import ZoneInfo
        BERLIN = ZoneInfo("Europe/Berlin")
    t = (now or datetime.now(timezone.utc)).astimezone(BERLIN)
    return t.weekday() < 5 and GETTEX_HOURS[0] <= t.hour < GETTEX_HOURS[1]


def gettex_quote(isin):
    """gettex's bid and ask for an ISIN, with the age of the quote — or None when onvista has none.
    Stocks and funds (ETFs among them) live under different onvista addresses."""
    import add_instrument
    for kind in ("stocks", "funds"):
        try:
            raw = add_instrument.yahoo(f"https://api.onvista.de/api/v1/{kind}/ISIN:{isin}/snapshot")
        except Exception:
            continue
        for q in (raw.get("quoteList") or {}).get("list", []):
            if (q.get("market") or {}).get("name") != "gettex":
                continue
            bid, ask = num(q.get("bid")), num(q.get("ask"))
            stamp = max(q.get("datetimeBid") or "", q.get("datetimeAsk") or "")
            if not (bid > 0 and ask >= bid and stamp) or q.get("isoCurrency") != "EUR":
                return None
            at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - at).total_seconds() / 60
            return {"bid": bid, "ask": ask, "age_min": round(age, 1),
                    "spread_pct": round((ask - bid) / ((ask + bid) / 2) * 100, 2)}
        return None
    return None


def venue_price(inst, side):
    """The price a buy or a sale gets now, and where — or a Refusal saying why there is none.
    gettex when it is open and its quote fresh; otherwise the home exchange, open and fresh."""
    g = gettex_quote(inst["isin"]) if gettex_open() else None
    if g and g["age_min"] <= GETTEX_MAX_AGE_MIN:
        return {"venue": "gettex", "price": g["ask"] if side == "buy" else g["bid"],
                "bid": g["bid"], "ask": g["ask"], "spread_pct": g["spread_pct"], "price_age_min": g["age_min"]}
    try:
        mkt = tradable(inst)
    except Refusal as r:
        why = ("gettex is closed (weekdays 08:00–22:00 German time)" if not gettex_open() else
               "gettex has no quote for it" if not g else
               f"gettex's quote is {g['age_min']} minutes old")
        raise Refusal(f"{r.error}, and {why}", r.hint)
    day, price = latest_close(inst)
    if day != mkt["local_date"]:
        raise Refusal(f"no price from today's session for {inst['name']} yet (latest is {day})", "try again shortly")
    return {"venue": mkt["exchange"] or "home exchange", "price": price, "price_age_min": mkt["price_age_min"]}


def latest_close(inst):
    """The newest close on file, after asking Yahoo for any newer one."""
    fetch_prices(inst)
    rows = series(inst)
    if not rows:
        raise Refusal(f"no prices for {inst['name']}", "it cannot be traded")
    return rows[-1]


def execute(name, side, inst_text, shares=None, eur=None, all_=False, reason="", oid=None):
    d = require_cli(name)
    with locked(d):
        path, rows = manual_tx.manual_rows(d)
        tid = f"manual-{oid}" if oid else None
        if tid:
            prior = next((r for r in rows if r["transaction_id"] == tid), None)
            if prior:
                return {"repeat": True, **trade_view(prior), "cash": cash_of(d)}
        inst = resolve(inst_text, register=(side == "buy"))
        if not series(inst):
            fetch_prices(inst)           # a first buy of a new instrument: its history, for valuing it later
        px = venue_price(inst, side)
        price = px["price"]
        cash = cash_of(d)
        if side == "buy":
            if ((eur or 0) > 0) == ((shares or 0) > 0):
                raise Refusal("give either --eur or --shares, as a positive number")
            if eur:
                # --eur is everything that leaves the account, the fee included
                value = (eur - FEE_FIXED) / (1 + FEE_RATE)
                if value <= 0:
                    raise Refusal(f"€{eur:g} does not cover the €{FEE_FIXED:g} minimum fee")
                shares = value / price
            shares = round(shares, 6)
            value = shares * price
            fee = fee_for(value)
            if value + fee > cash + 0.005:
                raise Refusal(f"not enough cash: this buy costs €{value + fee:.2f} with its fee, "
                              f"the account holds €{cash:.2f}",
                              f"buy for at most --eur {cash:.2f}, or sell something first")
        else:
            have = held(d, inst["isin"])
            if all_:
                shares = have
            elif not (shares or 0) > 0:
                raise Refusal("give --shares (a positive number) or --all")
            shares = round(shares, 6)
            if shares <= 0 or shares > have + 1e-9:
                raise Refusal(f"only {have:g} shares of {inst['name']} held")
            value = shares * price
            fee = fee_for(value)
            if value <= fee:
                raise Refusal(f"selling €{value:.2f} would not cover its €{fee:.2f} fee")
            tax, gain = sale_tax(rows, inst["isin"], shares, value, fee)
        row, _ = quiet(manual_tx.make_row, rows, today(), inst["isin"], side, shares, price, fee, tid=tid)
        row["description"] = reason or "trade.py"
        if side == "sell" and tax:
            row["tax"] = f"{-tax:.2f}"         # signed as TR books a sale's tax: money going out
        rows.append(row)
        write_csv(path, manual_tx.FIELDS, sorted(rows, key=lambda r: r["datetime"]))
        quiet(import_tr.rebuild, name, d)
        out = {**trade_view(row), "name": inst["name"], "venue": px["venue"],
               "price_age_min": px["price_age_min"], "cash": cash_of(d)}
        if "spread_pct" in px:
            out.update(bid=px["bid"], ask=px["ask"], spread_pct=px["spread_pct"])
        if side == "sell":
            out["gain"] = gain
        if inst.get("registered"):
            out["registered"] = True
        return out


def trade_view(r):
    shares, price = abs(num(r["shares"])), num(r["price"])
    fee, tax = -num(r["fee"]) + 0.0, -num(r["tax"]) + 0.0      # + 0.0: no "-0.0" for an empty column
    value = round(shares * price, 2)
    return {"id": r["transaction_id"].removeprefix("manual-"), "side": r["type"].lower(),
            "isin": r["symbol"], "date": r["date"], "shares": shares, "price": price, "value": value,
            "fee": round(fee, 2), **({"tax": round(tax, 2)} if r["type"] == "SELL" else {}),
            "cash_change": round(-(value + fee) if r["type"] == "BUY" else value - fee - tax, 2),
            **({"reason": r["description"]} if r["description"] not in ("", "trade.py") else {})}


def summary(name, d):
    """The account as it stands: cash, positions at their latest close, and the result against
    the money put in — cash included, the one fair way to set two accounts side by side."""
    positions = [p for p in read_csv(os.path.join(d, "positions.csv")) if p["isSold"] == "0"]
    cash_rows = read_csv(os.path.join(d, "cash.csv")) if os.path.exists(os.path.join(d, "cash.csv")) else []
    cash = sum(num(c["amount"]) for c in cash_rows)
    net_in = sum(num(c["amount"]) for c in cash_rows if c["kind"] in ("deposit", "withdrawal"))
    latest = import_tr.latest_prices()
    holdings = []
    for p in positions:
        # the freshest close on file, not the one the last rebuild happened to see
        q = latest.get(p["identifier"])
        price, day = (num(q["close"]), q["date"]) if q and q["date"] > p["lastPriceDate"] \
            else (num(p["lastPrice"]), p["lastPriceDate"])
        value = num(p["shares"]) * price
        holdings.append({"isin": p["identifier"], "name": p["name"], "shares": num(p["shares"]),
                         "price": price, "price_date": day, "value": round(value, 2),
                         "cost": round(num(p["purchaseValue"]), 2), "gain": round(value - num(p["purchaseValue"]), 2)})
    invested = sum(h["value"] for h in holdings)
    total = invested + cash
    return {"account": name, "label": profile(d).get("label") or name, "date": today(),
            "cash": round(cash, 2), "positions": holdings,
            "value_positions": round(invested, 2), "value_total": round(total, 2),
            "net_deposits": round(net_in, 2),
            "result": round(total - net_in, 2),
            "result_pct": round((total - net_in) / net_in * 100, 2) if net_in > 0 else None}


def status(name):
    return summary(name, require_cli(name))


def history(name):
    d = require_cli(name)
    _, rows = manual_tx.manual_rows(d)
    return {"account": name,
            "transactions": [trade_view(r) if r["category"] == "TRADING" else
                             {"id": r["transaction_id"].removeprefix("manual-"), "date": r["date"],
                              "side": "deposit" if num(r["amount"]) > 0 else "withdrawal",
                              "cash_change": num(r["amount"]),
                              **({"reason": r["description"]} if r["description"] else {})}
                             for r in rows]}


# The rules, for whoever trades: trading-rules.md, served from beside this script so the caller
# needs no path to it — and need know of nothing but this script and its own working folder.
RULES = os.path.join(ROOT, "trading-rules.md")


def rules():
    try:
        with open(RULES, encoding="utf-8") as fh:
            return {"rules": fh.read()}
    except OSError:
        raise Refusal("the rules are not available", "ask the account's owner")


def quote(text):
    """What a buy and a sale would get right now, and where — or why neither can happen."""
    inst = resolve(text, register=False)
    out = dict(inst)
    try:
        buy, sell = venue_price(inst, "buy"), venue_price(inst, "sell")
    except Refusal as r:
        day, last = latest_close(inst)
        return {**out, "tradable_now": False, "last_close": last, "last_close_date": day,
                "why": r.error, "hint": r.hint}
    out.update(tradable_now=True, venue=buy["venue"], buy_price=buy["price"], sell_price=sell["price"],
               price_age_min=buy["price_age_min"])
    if "spread_pct" in buy:
        out["spread_pct"] = buy["spread_pct"]
    out["note"] = (f"a buy now pays {buy['price']:g}, a sale gets {sell['price']:g} ({buy['venue']}); "
                   f"each trade also costs €{FEE_FIXED:g} + {FEE_RATE * 100:g}% of its value")
    return out


# ---------- command line ----------
# Shared with manage-accounts.py: an argparse whose mistakes are answered as JSON like every other
# refusal, and one runner that prints the single JSON object either way.

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal(message, f"{self.prog.split()[0]} --help lists the commands")


def run(doc, prog, configure, dispatch):
    p = Parser(prog=prog, description=doc.split("\n\n")[0],
               formatter_class=argparse.RawDescriptionHelpFormatter, epilog=doc.split("\n\n", 1)[1])
    configure(p.add_subparsers(dest="cmd", required=True, parser_class=Parser))
    try:
        out = dispatch(p.parse_args())
        print(json.dumps({"ok": True, **out}, ensure_ascii=False))
    except Refusal as r:
        print(json.dumps({"ok": False, "error": r.error, **({"hint": r.hint} if r.hint else {})}, ensure_ascii=False))
        sys.exit(1)


def configure(sub):
    sub.add_parser("quote").add_argument("instrument")
    for side in ("buy", "sell"):
        s = sub.add_parser(side); s.add_argument("account"); s.add_argument("instrument")
        s.add_argument("--shares", type=float)
        if side == "buy":
            s.add_argument("--eur", type=float)
        else:
            s.add_argument("--all", action="store_true")
        s.add_argument("--reason", default=""); s.add_argument("--id")
    for cmd in ("status", "history"):
        sub.add_parser(cmd).add_argument("account")
    sub.add_parser("rules")


def dispatch(a):
    if a.cmd == "quote":
        return quote(a.instrument)
    if a.cmd == "buy":
        return execute(a.account, "buy", a.instrument, shares=a.shares, eur=a.eur, reason=a.reason, oid=a.id)
    if a.cmd == "sell":
        return execute(a.account, "sell", a.instrument, shares=a.shares, all_=a.all, reason=a.reason, oid=a.id)
    if a.cmd == "status":
        return status(a.account)
    if a.cmd == "rules":
        return rules()
    return history(a.account)


if __name__ == "__main__":
    run(__doc__, "trade.py", configure, dispatch)
