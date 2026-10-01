#!/usr/bin/env python3
"""Trade a demo account from the command line — live only, JSON in every answer.

    trade.py create   <account> --cash EUR [--label TEXT]
    trade.py quote    <instrument>
    trade.py buy      <account> <instrument> (--eur EUR | --shares N) [--reason TEXT] [--id ID]
    trade.py sell     <account> <instrument> (--shares N | --all)      [--reason TEXT] [--id ID]
    trade.py cancel   <account> <order id>
    trade.py deposit  <account> --eur EUR [--reason TEXT] [--id ID]
    trade.py withdraw <account> --eur EUR [--reason TEXT] [--id ID]
    trade.py status   <account>
    trade.py history  <account>

<instrument> is an ISIN, a ticker or a name; anything ambiguous answers with the candidates.
Every answer is one JSON object; on error {"ok": false, "error": ..., "hint": ...}, exit code 1.

Live only. There is no date to give: cash moves today, and a buy or sell becomes an order that
fills at the first close dated *after* the day it was placed — so it can never use a price that was
already known when it was placed. Until then it is pending; any later call settles what can be
settled, fetching the closes it needs. Prices are in the portfolio currency, and there are no fees.

Only an account whose profile.json says "allow-cli": true can be touched — `create` sets it on the
accounts it makes; profiles made on the page, or controlled by Parqet, are refused. --id makes a
call safe to repeat: a second call with the same id answers with the first one's result instead of
trading twice. --reason is kept with the trade.
"""
import argparse, contextlib, csv, fcntl, io, json, os, re, subprocess, sys, uuid
from datetime import date, datetime, timezone

import import_tr
import manual_tx
from import_tr import num, read_csv, write_csv

ROOT = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(ROOT, "private-profiles")
ACCOUNT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
ORDERS = "orders.csv"
ORDER_FIELDS = ["id", "placed", "placed_date", "side", "isin", "shares", "eur", "all", "reason",
                "status", "fill_date", "fill_price", "fill_shares", "note"]


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
        raise Refusal(f"no account {name!r}", "create it first: trade.py create <account> --cash EUR")
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
        raise Refusal(f"account {name!r} is not open to trade.py",
                      'only accounts whose profile.json has "allow-cli": true — those trade.py create makes')
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
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "add_instrument.py"), isin,
                           listings[0]["symbol"], name, "Other"],
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
    subprocess.run([sys.executable, os.path.join(ROOT, "update_prices.py"), inst["id"]],
                   capture_output=True, text=True, timeout=180)


# ---------- orders ----------

def orders(d):
    path = os.path.join(d, ORDERS)
    return read_csv(path) if os.path.exists(path) else []


def save_orders(d, rows):
    write_csv(os.path.join(d, ORDERS), ORDER_FIELDS, rows)


def held(d, isin):
    """Shares of `isin` in the account now, settled trades only."""
    _, rows = manual_tx.manual_rows(d)
    return sum(num(r["shares"]) for r in rows if r["symbol"] == isin and r["type"] in ("BUY", "SELL"))


def settle(name, d):
    """Fill every pending order whose fill close exists — the first close dated after the day it
    was placed — fetching closes for the instruments still waiting. Returns what was filled."""
    book = orders(d)
    pending = [o for o in book if o["status"] == "pending"]
    if not pending:
        return []
    rows, sources = registry()
    by_id = {r["id"]: r for r in rows}
    filled, fetched = [], set()
    for o in sorted(pending, key=lambda o: o["placed"]):
        inst = instrument(by_id[o["isin"]], sources)
        pick = lambda: next(((dd, c) for dd, c in series(inst) if dd > o["placed_date"]), None)
        fill = pick()
        if not fill and today() > o["placed_date"] and inst["id"] not in fetched:
            fetch_prices(inst)
            fetched.add(inst["id"])
            fill = pick()
        if not fill:
            continue
        day, price = fill
        shares = (num(o["eur"]) / price if o["eur"] else num(o["shares"])) if o["side"] == "buy" \
            else (held(d, o["isin"]) if o["all"] == "1" else num(o["shares"]))
        shares = round(shares, 6)
        if shares <= 0:
            o.update(status="cancelled", note="nothing to sell when it came to fill")
            continue
        try:
            quiet(add_trade, d, day, o, shares, price)
        except Refusal as r:
            o.update(status="cancelled", note=r.error)
            continue
        o.update(status="filled", fill_date=day, fill_price=f"{price:.6f}", fill_shares=f"{shares:.6f}")
        filled.append(order_view(o, inst))
    save_orders(d, book)
    if filled:
        quiet(import_tr.rebuild, name, d)
    return filled


def add_trade(d, day, o, shares, price):
    path, rows = manual_tx.manual_rows(d)
    row, _ = manual_tx.make_row(rows, day, o["isin"], o["side"], shares, price, 0,
                                tid=f"manual-{o['id']}")
    row["description"] = o["reason"] or "trade.py"
    rows.append(row)
    manual_tx.never_short(rows, o["isin"])
    write_csv(path, manual_tx.FIELDS, sorted(rows, key=lambda r: r["datetime"]))


def order_view(o, inst=None):
    out = {"order": o["id"], "side": o["side"], "isin": o["isin"], "status": o["status"],
           "placed": o["placed"]}
    if inst:
        out["name"] = inst["name"]
    if o["status"] == "pending":
        out["requested"] = ({"eur": num(o["eur"])} if o["eur"] else
                            {"shares": "all"} if o["all"] == "1" else {"shares": num(o["shares"])})
        out["fills_at"] = f"the first close after {o['placed_date']}"
    if o["status"] == "filled":
        out.update(date=o["fill_date"], price=num(o["fill_price"]), shares=num(o["fill_shares"]),
                   eur=round(num(o["fill_price"]) * num(o["fill_shares"]), 2))
    if o.get("note"):
        out["note"] = o["note"]
    if o["reason"]:
        out["reason"] = o["reason"]
    return out


def place(name, side, inst_text, shares=None, eur=None, all_=False, reason="", oid=None):
    d = require_cli(name)
    with locked(d):
        book = orders(d)
        if oid and any(o["id"] == oid for o in book):
            prior = next(o for o in book if o["id"] == oid)
            return {"repeat": True, **order_view(prior)}
        inst = resolve(inst_text, register=(side == "buy"))
        if not series(inst):
            fetch_prices(inst)
            if not series(inst):
                raise Refusal(f"no prices for {inst['name']}", "it cannot be traded")
        if side == "buy" and not ((eur or 0) > 0) ^ ((shares or 0) > 0):
            raise Refusal("give either --eur or --shares, as a positive number")
        if side == "sell":
            if not all_ and not (shares or 0) > 0:
                raise Refusal("give --shares (a positive number) or --all")
            free = held(d, inst["isin"]) - sum(num(o["shares"]) for o in book
                                               if o["status"] == "pending" and o["side"] == "sell"
                                               and o["isin"] == inst["isin"] and o["all"] != "1")
            if free <= 0 or (not all_ and shares > free + 1e-9):
                raise Refusal(f"only {max(free, 0):g} shares of {inst['name']} free to sell",
                              "pending sells count against what is held")
        o = {"id": oid or uuid.uuid4().hex[:12], "placed": now(), "placed_date": today(), "side": side,
             "isin": inst["isin"], "shares": f"{shares:g}" if shares else "", "eur": f"{eur:g}" if eur else "",
             "all": "1" if all_ else "", "reason": reason or "", "status": "pending",
             "fill_date": "", "fill_price": "", "fill_shares": "", "note": ""}
        book.append(o)
        save_orders(d, book)
        return {**order_view(o, inst), **({"registered": True} if inst.get("registered") else {})}


def move_cash(name, kind, eur, reason="", oid=None):
    d = require_cli(name)
    if not (eur or 0) > 0:
        raise Refusal("give --eur as a positive number")
    with locked(d):
        path, rows = manual_tx.manual_rows(d)
        tid = f"manual-{oid}" if oid else None
        if tid and any(r["transaction_id"] == tid for r in rows):
            return {"repeat": True, "id": oid}
        row = quiet(manual_tx.make_cash_row, rows, today(), kind, eur, tid=tid)
        row["description"] = reason or "trade.py"
        rows.append(row)
        write_csv(path, manual_tx.FIELDS, sorted(rows, key=lambda r: r["datetime"]))
        quiet(import_tr.rebuild, name, d)
        return {kind: eur, "date": today(), "id": row["transaction_id"].removeprefix("manual-")}


def status(name):
    d = require_cli(name)
    with locked(d):
        filled = settle(name, d)
    positions = [p for p in read_csv(os.path.join(d, "positions.csv")) if p["isSold"] == "0"]
    cash_rows = read_csv(os.path.join(d, "cash.csv")) if os.path.exists(os.path.join(d, "cash.csv")) else []
    cash = sum(num(c["amount"]) for c in cash_rows)
    net_in = sum(num(c["amount"]) for c in cash_rows if c["kind"] in ("deposit", "withdrawal"))
    invested = sum(num(p["currentValue"]) for p in positions)
    total = invested + cash
    out = {"account": name, "date": today(), "cash": round(cash, 2),
           "positions": [{"isin": p["identifier"], "name": p["name"], "shares": num(p["shares"]),
                          "price": num(p["lastPrice"]), "price_date": p["lastPriceDate"],
                          "value": round(num(p["currentValue"]), 2), "cost": round(num(p["purchaseValue"]), 2),
                          "gain": round(num(p["unrealizedGainNet"]), 2)} for p in positions],
           "value_positions": round(invested, 2), "value_total": round(total, 2),
           "net_deposits": round(net_in, 2),
           "result": round(total - net_in, 2),
           "result_pct": round((total - net_in) / net_in * 100, 2) if net_in > 0 else None,
           "pending": [order_view(o) for o in orders(d) if o["status"] == "pending"]}
    if filled:
        out["just_filled"] = filled
    return out


def history(name):
    d = require_cli(name)
    with locked(d):
        settle(name, d)
    _, rows = manual_tx.manual_rows(d)
    return {"account": name,
            "transactions": [{"date": r["date"], "type": r["type"].lower(), "isin": r["symbol"] or None,
                              "shares": abs(num(r["shares"])) or None, "price": num(r["price"]) or None,
                              "eur": num(r["amount"]), "reason": r["description"]} for r in rows],
            "orders": [order_view(o) for o in orders(d)]}


def cancel(name, oid):
    d = require_cli(name)
    with locked(d):
        settle(name, d)
        book = orders(d)
        o = next((o for o in book if o["id"] == oid), None)
        if not o:
            raise Refusal(f"no order {oid!r}", "trade.py status <account> lists the pending ones")
        if o["status"] != "pending":
            raise Refusal(f"order {oid} is {o['status']}, not pending")
        o.update(status="cancelled", note="cancelled")
        save_orders(d, book)
        return order_view(o)


def create(name, eur, label=""):
    d = account_dir(name, must_exist=False)
    if os.path.exists(d):
        raise Refusal(f"account {name!r} exists", "pick another name, or trade it as it is")
    if not (eur or 0) >= 0:
        raise Refusal("give --cash as zero or more")
    os.makedirs(d)
    with open(os.path.join(d, "profile.json"), "w") as fh:
        json.dump({"label": label or name, "source": "manual", "allow-cli": True, "watchlist": []}, fh, indent=2)
        fh.write("\n")
    quiet(import_tr.rebuild, name, d)
    if eur:
        move_cash(name, "deposit", eur, "opening balance")
    return {"account": name, "cash": eur or 0, "date": today()}


def quote(text):
    inst = resolve(text, register=False)
    rows = series(inst)
    if not rows:
        fetch_prices(inst)
        rows = series(inst)
    if not rows:
        raise Refusal(f"no prices for {inst['name']}")
    day, close = rows[-1]
    return {**inst, "close": close, "date": day,
            "note": "a buy or sell placed now fills at the first close after today, not at this one"}


# ---------- command line ----------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal(message, "trade.py --help lists the commands")


def main():
    p = Parser(prog="trade.py", description=__doc__.split("\n\n")[0],
               formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    c = sub.add_parser("create"); c.add_argument("account"); c.add_argument("--cash", type=float, required=True)
    c.add_argument("--label", default="")
    q = sub.add_parser("quote"); q.add_argument("instrument")
    for side in ("buy", "sell"):
        s = sub.add_parser(side); s.add_argument("account"); s.add_argument("instrument")
        s.add_argument("--shares", type=float)
        if side == "buy":
            s.add_argument("--eur", type=float)
        else:
            s.add_argument("--all", action="store_true")
        s.add_argument("--reason", default=""); s.add_argument("--id")
    x = sub.add_parser("cancel"); x.add_argument("account"); x.add_argument("order")
    for kind in ("deposit", "withdraw"):
        m = sub.add_parser(kind); m.add_argument("account"); m.add_argument("--eur", type=float, required=True)
        m.add_argument("--reason", default=""); m.add_argument("--id")
    for cmd in ("status", "history"):
        sub.add_parser(cmd).add_argument("account")

    try:
        a = p.parse_args()
        if a.cmd == "create":
            out = create(a.account, a.cash, a.label)
        elif a.cmd == "quote":
            out = quote(a.instrument)
        elif a.cmd == "buy":
            out = place(a.account, "buy", a.instrument, shares=a.shares, eur=a.eur, reason=a.reason, oid=a.id)
        elif a.cmd == "sell":
            out = place(a.account, "sell", a.instrument, shares=a.shares, all_=a.all, reason=a.reason, oid=a.id)
        elif a.cmd == "cancel":
            out = cancel(a.account, a.order)
        elif a.cmd in ("deposit", "withdraw"):
            out = move_cash(a.account, "deposit" if a.cmd == "deposit" else "withdrawal", a.eur, a.reason, a.id)
        elif a.cmd == "status":
            out = status(a.account)
        else:
            out = history(a.account)
        print(json.dumps({"ok": True, **out}, ensure_ascii=False))
    except Refusal as r:
        print(json.dumps({"ok": False, "error": r.error, **({"hint": r.hint} if r.hint else {})}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
