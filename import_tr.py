#!/usr/bin/env python3
"""Import a Trade Republic transaction export into a profile, in place of a Parqet refresh.

    python3 import_tr.py <profile> <transactions*.csv> [...]
    python3 import_tr.py <profile>                  # just rebuild from what is already in

Manual profiles only — profile.json's "source": "manual". A Parqet profile (the default when the
key is missing) is refused, since its CSVs belong to the Parqet refresh.

Trade Republic's export (app → Profile → Transaction export) is one CSV of every booking, each
with a `transaction_id`. Every row ever imported is kept, untouched, in

    private-profiles/<profile>/tr_ledger.csv

keyed by that id — so importing the same file twice, or two exports whose date ranges overlap,
adds only the rows not seen before. The two files the page reads, positions.csv and
activities.csv, are then rebuilt from the whole ledger on every run, in exactly the schema
REFRESH_PARQET_DATA.md writes from Parqet. Rows entered by hand (manual_tx.py — a virtual demo
portfolio, say) live in manual_ledger.csv beside it, in the same TR row format, and the rebuild
reads both. The two CSVs are output, never input: a fix to the conversion
below is applied by re-running this, not by repairing data.

The conventions are Parqet's, matched against Parqet's own import of the same account:
  buy/sell   `amount` gross and positive, `fee`/`tax` positive, `amountNet` = what actually moved
             (gross + fee on a buy, gross − tax − fee on a sale), `price` = amount / shares
  dividend   `amount` before withholding, `tax` the withholding, `amountNet` what was paid out
  split      no row of its own — the trades stay at the scale they were booked at, and only the
             position's share count is restated (the page detects the ratio, see splitFactor)
  warrant    an exercise (WARRANT_EXERCISE + its TILG payout) is a sale of everything left, at
             whatever TILG paid — usually a few cents, or nothing
Two things Parqet does not have, done here the closest equivalent way:
  TAX_OPTIMIZATION   TR's tax refund (loss offsetting), booked as an account-level `fees_taxes`
                     row with a negative tax — it belongs to no position
  cash               deposits, transfers, interest: ignored, as for Parqet (see build())

Realised gains are FIFO, gross and net. Net subtracts the sale's own tax and fee *and* the buy
fees of the lots it retires — that is how Parqet's realizedGainsNet comes out. Checked against
Parqet's import of the same account: every trade agrees, and every position to within a euro,
except where Parqet is itself inconsistent (a closed position whose realised gain is not
proceeds − cost − tax − fees) or drops a warrant's few-cent payout.
"""
import csv, json, os, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO = "Trade Republic"
MANUAL_PORTFOLIO = "Manual"              # where hand-entered rows (account_type MANUAL) book
LEDGER = "tr_ledger.csv"
MANUAL_LEDGER = "manual_ledger.csv"      # hand-entered rows, same format — see manual_tx.py
TR_DELETED = "tr_deleted.csv"            # imported rows deleted on the page — kept out of the rebuild
                                         # but left in the ledger, so a re-import does not revive them
REQUIRED = {"transaction_id", "datetime", "date", "category", "type", "asset_class", "name",
            "symbol", "shares", "price", "amount", "fee", "tax", "currency"}

POSITIONS_FIELDS = ["portfolio", "name", "identifier", "assetType", "isSold", "shares", "currency",
                    "currentValue", "purchaseValue", "lastPriceDate", "lastPrice",
                    "realizedGainNet", "unrealizedGainNet", "earliestActivityDate", "activityCount"]
# Parqet's schema, plus the ledger row each activity came from — what the Transactions tab's delete
# names. A Parqet profile's activities.csv has no such column, and nothing there is deletable.
ACTIVITIES_FIELDS = ["portfolio", "name", "identifier", "type", "datetime", "shares", "price",
                     "amount", "amountNet", "fee", "tax", "realizedGains", "realizedGainsNet",
                     "currency", "transactionId"]
DIVIDEND_TYPES = {"DIVIDEND", "DISTRIBUTION", "EARNINGS"}

# cash.csv: every booking's effect on its account's cash, tagged by what moved it. The page sums it
# into a balance on any date; only "deposit"/"withdrawal"/"interest" are listed as transactions of
# their own — the rest already are, as the trade, dividend or tax row they came with.
CASH_FIELDS = ["portfolio", "datetime", "date", "kind", "amount", "transactionId"]
CASH_KIND = {"CUSTOMER_INBOUND": "deposit", "TRANSFER_INBOUND": "deposit",
             "TRANSFER_INSTANT_INBOUND": "deposit", "CUSTOMER_OUTBOUND_REQUEST": "withdrawal",
             "TRANSFER_OUTBOUND": "withdrawal", "TRANSFER_INSTANT_OUTBOUND": "withdrawal",
             "INTEREST_PAYMENT": "interest", "TAX_OPTIMIZATION": "tax",
             "BUY": "trade", "SELL": "trade", "TILG": "trade"}


def cash_rows(ledger):
    """What each booking did to its account's cash: amount + fee + tax, signed as TR signs them
    (a buy's amount and fee are negative, a refund's tax positive). Checked against Parqet's own
    record of the same account's balance: equal to the cent, but for the TAX_OPTIMIZATION
    refunds Parqet leaves out."""
    out = []
    for t in ledger:
        delta = num(t["amount"]) + num(t["fee"]) + num(t["tax"])
        if abs(delta) < 0.005:
            continue
        kind = "income" if t["type"] in DIVIDEND_TYPES else CASH_KIND.get(t["type"], "other")
        out.append({"portfolio": MANUAL_PORTFOLIO if t.get("account_type") == "MANUAL" else PORTFOLIO,
                    "datetime": t["datetime"], "date": t["date"], "kind": kind,
                    "amount": fmt(delta, 2), "transactionId": t["transaction_id"]})
    return out
EPS = 1e-9


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fmt(v, places=6):
    """Plain decimal, no exponent, no trailing zeros — the way the Parqet files read."""
    s = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, fields, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


# ---------- the ledger: every TR row ever imported, deduplicated by transaction_id ----------

def merge_into_ledger(ledger_path, files):
    rows = read_csv(ledger_path) if os.path.exists(ledger_path) else []
    seen = {r["transaction_id"] for r in rows}
    fields = list(rows[0].keys()) if rows else []
    for f in files:
        new = read_csv(f)
        missing = REQUIRED - set(new[0].keys() if new else REQUIRED)
        if missing:
            sys.exit(f"{f}: not a Trade Republic transaction export — missing {sorted(missing)}")
        added = 0
        for r in new:
            tid = r["transaction_id"]
            if not tid or tid in seen:
                continue
            seen.add(tid)
            rows.append(r)
            added += 1
        for k in (new[0].keys() if new else []):
            if k not in fields:
                fields.append(k)
        print(f"{os.path.basename(f)}: {len(new)} rows — {added} new, {len(new) - added} already imported")
    rows.sort(key=lambda r: (r["datetime"], r["transaction_id"]))
    if files:
        write_csv(ledger_path, fields, rows)
    return rows


# ---------- conversion ----------

def registry_names():
    path = os.path.join(ROOT, "registry", "instruments.csv")
    return {r["id"]: r for r in read_csv(path)} if os.path.exists(path) else {}


def latest_prices():
    path = os.path.join(ROOT, "gen_prices", "_latest.csv")
    return {r["id"]: r for r in read_csv(path)} if os.path.exists(path) else {}


def convert(ledger):
    """Ledger rows → (activities, positions, notes). Walks each position's history once, FIFO.

    A position is (portfolio, ISIN), as on the page: TR rows book under "Trade Republic", rows
    entered by hand under "Manual" — so a virtual position never mixes with a real one in the
    same instrument."""
    registry = registry_names()
    names = {}                                  # ISIN → the name to print, registry first
    activities, notes = [], defaultdict(int)
    lots = defaultdict(list)                    # position → [[shares, gross price, fee per share]]
    realized = defaultdict(float)               # position → realised gain net, summed
    first = {}                                  # position → earliest activity date
    count = defaultdict(int)
    last_trade = {}                             # position → (date, price) of the last buy/sell
    payouts = {(r["symbol"], r["date"]): num(r["amount"]) for r in ledger if r["type"] == "TILG"}

    def row(t, k, kind, shares, amount, fee=0.0, tax=0.0, net=None, rg=0.0, rgn=0.0):
        if net is None:
            net = amount + fee if kind == "buy" else amount - tax - fee
        price = amount / shares if shares else 0.0
        pf, isin = k
        activities.append({
            "portfolio": pf, "name": names.get(isin, t["name"]), "identifier": isin,
            "type": kind, "datetime": t["datetime"], "shares": fmt(shares), "price": fmt(price),
            "amount": fmt(amount, 2), "amountNet": fmt(net, 2), "fee": fmt(fee, 2),
            "tax": fmt(tax, 2), "realizedGains": fmt(rg), "realizedGainsNet": fmt(rgn),
            "currency": t["currency"] or "EUR", "transactionId": t["transaction_id"]})
        if isin:
            first.setdefault(k, t["date"])
            count[k] += 1

    def retire(k, shares):
        """Take `shares` off the oldest lots; return (gross cost, buy fees) of what was taken."""
        cost = fees = 0.0
        left = shares
        q = lots[k]
        while left > EPS and q:
            take = min(left, q[0][0])
            cost += take * q[0][1]
            fees += take * q[0][2]
            q[0][0] -= take
            left -= take
            if q[0][0] <= EPS:
                q.pop(0)
        if left > 1e-6:
            notes[f"{k[1]}: sold {left:g} more shares than were bought — gain computed on the rest"] += 1
        return cost, fees

    for t in ledger:
        kind, isin = t["type"], t["symbol"]
        k = (MANUAL_PORTFOLIO if t.get("account_type") == "MANUAL" else PORTFOLIO, isin)
        if isin:
            names.setdefault(isin, (registry.get(isin) or {}).get("name") or t["name"])
        if kind == "BUY":
            shares, amount, fee = num(t["shares"]), -num(t["amount"]), -num(t["fee"])
            lots[k].append([shares, amount / shares, fee / shares])
            row(t, k, "buy", shares, amount, fee=fee, tax=-num(t["tax"]))
            last_trade[k] = (t["date"], amount / shares)
        elif kind == "SELL":
            shares, amount = -num(t["shares"]), num(t["amount"])
            fee, tax = -num(t["fee"]), -num(t["tax"])
            cost, buy_fees = retire(k, shares)
            rg = amount - cost
            rgn = rg - tax - fee - buy_fees
            realized[k] += rgn
            row(t, k, "sell", shares, amount, fee=fee, tax=tax, rg=rg, rgn=rgn)
            last_trade[k] = (t["date"], amount / shares)
        elif kind == "WARRANT_EXERCISE":
            shares = -num(t["shares"])
            amount = payouts.get((isin, t["date"]), 0.0)
            cost, buy_fees = retire(k, shares)
            rg = amount - cost
            realized[k] += rg - buy_fees
            row(t, k, "sell", shares, amount, rg=rg, rgn=rg - buy_fees)
            last_trade[k] = (t["date"], amount / shares if shares else 0.0)
        elif kind == "TILG":
            pass                                # folded into its WARRANT_EXERCISE above
        elif kind in DIVIDEND_TYPES:
            amount, tax = num(t["amount"]), -num(t["tax"])
            row(t, k, "dividend", num(t["shares"]) or 1.0, amount, tax=tax)
        elif kind == "SPLIT":
            # TR states the shares the split *added* (9 held, 4:1 → "27"), not the new total. The
            # lots are rescaled so later sales match up with them; the trades themselves keep the
            # scale they were booked at (Parqet's way).
            held = sum(l[0] for l in lots[k])
            added = num(t["shares"])
            if held > EPS and added > EPS:
                ratio = (held + added) / held
                for l in lots[k]:
                    l[0] *= ratio
                    l[1] /= ratio
                    l[2] /= ratio
            first.setdefault(k, t["date"])
            count[k] += 1
        elif kind == "TAX_OPTIMIZATION":
            refund = num(t["tax"])
            names[""] = "Tax optimisation"      # the one row with no ISIN, so this is its name
            row(t, k, "fees_taxes", 0.0, 0.0, tax=-refund, net=-refund)
        elif t["category"] == "CASH":
            notes["cash bookings — deposits, transfers, interest — go to cash.csv only"] += 1
        else:
            notes[f"unknown type {kind} ({t['category']}) ignored"] += 1

    latest = latest_prices()
    positions = []
    for k in sorted(first):
        pf, isin = k
        shares = sum(l[0] for l in lots[k])
        if shares < 1e-6:
            shares = 0.0
        # what the shares still held cost, buy fees included — as Parqet's purchaseValue does
        cost = sum(l[0] * (l[1] + l[2]) for l in lots[k]) if shares else 0.0
        quote = latest.get(isin)
        if quote and num(quote["close"]) > 0:
            price_date, price = quote["date"], num(quote["close"])
        else:
            price_date, price = last_trade.get(k, (first[k], 0.0))
            if shares:
                notes[f"{isin}: no price in gen_prices/_latest.csv — valued at its last trade"] += 1
        value = shares * price
        positions.append({
            "portfolio": pf, "name": names.get(isin, isin), "identifier": isin,
            "assetType": "security", "isSold": "0" if shares else "1", "shares": fmt(shares),
            "currency": "EUR", "currentValue": fmt(value, 4), "purchaseValue": fmt(cost, 4),
            "lastPriceDate": price_date, "lastPrice": fmt(price),
            "realizedGainNet": fmt(realized[k], 2),
            "unrealizedGainNet": fmt(value - cost, 4) if shares else "0",
            "earliestActivityDate": first[k], "activityCount": str(count[k])})
    activities.sort(key=lambda r: r["datetime"])
    return activities, positions, notes


def manual_profile_dir(profile):
    """The profile's directory, or exit: it must exist and be marked "source": "manual".

    A Parqet one has its CSVs written by the refresh task, and rebuilding them from these ledgers
    would throw its other portfolios away — so no key, or any other value, counts as Parqet: the
    side that refuses."""
    d = os.path.join(ROOT, "private-profiles", profile)
    if not os.path.isdir(d):
        sys.exit(f"no profile {profile!r} — expected {d}/ (create it on the page's Config tab)")
    try:
        with open(os.path.join(d, "profile.json")) as fh:
            source = json.load(fh).get("source")
    except (OSError, ValueError):
        source = None
    if source != "manual":
        sys.exit(f"{profile} is controlled by Parqet, not manually — refusing to change it "
                 f"(set \"source\": \"manual\" in its profile.json only if that is really meant)")
    return d


def rebuild(profile, d, tr_rows=None):
    """positions.csv + activities.csv from both ledgers — the TR imports and the hand-entered rows
    (manual_tx.py), which share one row format and so one conversion."""
    if tr_rows is None:
        path = os.path.join(d, LEDGER)
        tr_rows = read_csv(path) if os.path.exists(path) else []
    path = os.path.join(d, TR_DELETED)
    if os.path.exists(path):
        gone = {r["transaction_id"] for r in read_csv(path)}
        tr_rows = [r for r in tr_rows if r["transaction_id"] not in gone]
    path = os.path.join(d, MANUAL_LEDGER)
    manual = read_csv(path) if os.path.exists(path) else []
    ledger = sorted(tr_rows + manual, key=lambda r: (r["datetime"], r["transaction_id"]))
    activities, positions, notes = convert(ledger)
    write_csv(os.path.join(d, "activities.csv"), ACTIVITIES_FIELDS, activities)
    write_csv(os.path.join(d, "positions.csv"), POSITIONS_FIELDS, positions)
    cash = cash_rows(ledger)
    write_csv(os.path.join(d, "cash.csv"), CASH_FIELDS, cash)

    open_n = sum(1 for p in positions if p["isSold"] == "0")
    print(f"{profile}: {len(tr_rows)} imported + {len(manual)} hand-entered rows → "
          f"{len(activities)} activities, {open_n} open and {len(positions) - open_n} closed positions")
    for note, n in sorted(notes.items()):
        print(f"  {note}" + (f" (×{n})" if n > 1 else ""))
    for pf in sorted({c["portfolio"] for c in cash}):
        print(f"  cash, {pf}: {sum(num(c['amount']) for c in cash if c['portfolio'] == pf):.2f}")

    registry = registry_names()
    unknown = sorted({p["identifier"] for p in positions} - set(registry))
    if unknown:
        print(f"  not in registry/instruments.csv yet ({len(unknown)}): " + ", ".join(unknown))
        print("  — they show under their export name with no price chart until a row is added")


def main():
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <profile> [<transactions.csv> ...]")
    profile, files = sys.argv[1], sys.argv[2:]
    d = manual_profile_dir(profile)
    rebuild(profile, d, merge_into_ledger(os.path.join(d, LEDGER), files))


if __name__ == "__main__":
    main()
