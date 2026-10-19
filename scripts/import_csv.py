#!/usr/bin/env python3
"""Import transactions in the Portfolioviz CSV format into a manual portfolio — for any broker's
statement, converted by hand, a script or an AI; and accept or roll back what was imported.

    python3 scripts/import_csv.py <portfolio> <file.csv | ->     # import (- reads stdin)
    python3 scripts/import_csv.py <portfolio> accept             # the new rows are verified
    python3 scripts/import_csv.py <portfolio> discard            # the new rows are deleted

The format — one header line, then one transaction per line, comma-separated, any column order:

    date,depot,type,isin,shares,price,fee,tax
    2026-06-04,Comdirect,buy,US0378331005,35,196.50,6.80,0
    2026-06-19,Comdirect,sell,US0378331005,20,201.10,9.30,14.20
    2026-03-02,Comdirect,dividend,IE00B4L5Y983,390,0.25,,14.63
    2026-01-15,Comdirect,deposit,,,5000,,

  date    YYYY-MM-DD, not in the future
  depot   the securities account it was booked in — any name but "Manual", which is the hand-entered
          rows' own (manual_tx.py)
  type    buy | sell | dividend | deposit | withdrawal
  isin    an instrument in the registry (add it there first); empty for deposit and withdrawal
  shares  how many, positive; for a dividend, the shares it was paid on; empty for deposit/withdrawal
  price   per share in the portfolio currency, positive (a dividend: gross per share);
          for deposit and withdrawal, the amount
  fee     charged on top of a buy, off a sale; zero or empty if none
  tax     paid on a sale or withheld from a dividend; zero or empty if none — negative on a sale is
          a refund
Numbers are plain decimals with a point and no thousands separator. Blank lines, and lines starting
with #, are skipped.

Every line is checked first — format, the instrument in the registry, no sale of more than the depot
holds at that point — and on any problem nothing is imported and every problem is listed. A line
equal to a transaction the portfolio already has (same date, depot, instrument, shares and price —
for cash, same date, depot, type and amount), or had and deleted on the page, is a duplicate:
reported and skipped, not an error, so pasting the same lines twice is harmless.

What is imported lands in the ledger (origin 'csv') *unverified*: the page lists it as new, and
`accept` makes it a transaction like any other, or `discard` removes every row not yet accepted —
the rollback for a bad paste. An accepted row can still be deleted one at a time, like a Trade
Republic row (manual_tx.py delete): kept, marked deleted, so a re-paste does not revive it.
"""
import csv, re, sys, uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

import db
from import_tr import (MANUAL_DEPOT, active_ledger, depot_of, num, rebuild, registry_names,
                       require_manual, short_at)

COLUMNS = ["date", "depot", "type", "isin", "shares", "price", "fee", "tax"]
TYPES = ("buy", "sell", "dividend", "deposit", "withdrawal")
CASH_TYPES = {"deposit": "CUSTOMER_INBOUND", "withdrawal": "CUSTOMER_OUTBOUND_REQUEST"}
NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
# what a ledger row's type is, in this format's words; anything else (a split, a tax refund) is
# never a duplicate of a pasted line
KIND = {"BUY": "buy", "SELL": "sell", "DIVIDEND": "dividend", "DISTRIBUTION": "dividend",
        "EARNINGS": "dividend", **{v: k for k, v in CASH_TYPES.items()},
        "TRANSFER_INBOUND": "deposit", "TRANSFER_INSTANT_INBOUND": "deposit",
        "TRANSFER_OUTBOUND": "withdrawal", "TRANSFER_INSTANT_OUTBOUND": "withdrawal"}


def parse(text):
    """(line number, row) per transaction line, or exit naming what is wrong with the header."""
    lines = [(i, l) for i, l in enumerate(text.lstrip("﻿").splitlines(), 1)
             if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        sys.exit("nothing to import — the first line must be the header:\n  " + ",".join(COLUMNS))
    head = [h.strip().lower() for h in next(csv.reader([lines[0][1]]))]
    if sorted(head) != sorted(COLUMNS):
        sys.exit(f"line {lines[0][0]}: the header must name exactly these columns, in any order:\n  "
                 + ",".join(COLUMNS) + f"\nnot\n  {','.join(head)}")
    out = []
    for i, line in lines[1:]:
        cells = next(csv.reader([line]))
        out.append((i, dict(zip(head, (c.strip() for c in cells))) if len(cells) == len(head) else
                    {"_cells": len(cells)}))
    if not out:
        sys.exit("nothing to import — only a header")
    return out


def check(i, r, registry):
    """The problems with one line, as text; none if it is fine."""
    if "_cells" in r:
        return [f"line {i}: {r['_cells']} values, but the header has {len(COLUMNS)}"]
    bad = []
    try:
        if datetime.strptime(r["date"], "%Y-%m-%d").date() > date.today():
            bad.append(f"line {i}: {r['date']} is in the future")
    except ValueError:
        bad.append(f"line {i}: date {r['date']!r} is not YYYY-MM-DD")
    if not r["depot"]:
        bad.append(f"line {i}: no depot")
    elif r["depot"].lower() == MANUAL_DEPOT.lower():
        bad.append(f"line {i}: depot {MANUAL_DEPOT!r} is for transactions entered by hand — name the real one")
    kind = r["type"].lower()
    if kind not in TYPES:
        return bad + [f"line {i}: type {r['type']!r} is not one of {', '.join(TYPES)}"]
    unreadable = [f"line {i}: {col} {r[col]!r} is not a plain number (like 1984.30)"
                  for col in ("shares", "price", "fee", "tax") if r[col] and not NUMBER.match(r[col])]
    if unreadable:
        return bad + unreadable
    shares, price, fee, tax = (num(r[c]) for c in ("shares", "price", "fee", "tax"))
    if kind in CASH_TYPES:
        if r["isin"] or r["shares"]:
            bad.append(f"line {i}: a {kind} has no isin or shares — its amount goes in price")
        if price <= 0:
            bad.append(f"line {i}: a {kind} needs its amount, positive, in price")
        if fee or tax:
            bad.append(f"line {i}: a {kind} has no fee or tax")
        return bad
    if r["isin"].upper() not in registry:
        bad.append(f"line {i}: {r['isin'] or 'no isin'} is not in the registry — add it first "
                   "(+ New instrument… on the Transactions tab)")
    if shares <= 0:
        bad.append(f"line {i}: shares must be positive")
    if price <= 0:
        bad.append(f"line {i}: price must be positive")
    if fee < 0:
        bad.append(f"line {i}: the fee cannot be negative")
    if kind == "dividend" and fee:
        bad.append(f"line {i}: a dividend has no fee — only tax withheld")
    if tax < 0 and kind != "sell":
        bad.append(f"line {i}: only a sale's tax can be negative (a refund)")
    return bad


def ledger_row(r, at, registry):
    """The line as a ledger row, in the TR export's format and signs (what import_tr converts)."""
    kind = r["type"].lower()
    shares, price, fee, tax = (num(r[c]) for c in ("shares", "price", "fee", "tax"))
    row = {"datetime": at, "date": r["date"], "account_type": "CSV", "depot": r["depot"],
           "asset_class": "", "currency": "EUR", "description": "imported from csv",
           "transaction_id": f"csv-{uuid.uuid4()}", "fee": f"{-fee:.2f}" if fee else "",
           "tax": f"{-tax:.2f}" if tax else ""}
    if kind in CASH_TYPES:
        return {**row, "category": "CASH", "type": CASH_TYPES[kind], "name": "", "symbol": "",
                "shares": "", "price": "", "amount": f"{price if kind == 'deposit' else -price:.2f}"}
    isin = r["isin"].upper()
    gross = shares * price
    return {**row, "category": "TRADING", "type": kind.upper(), "symbol": isin,
            "name": registry[isin].get("name") or isin,
            "shares": f"{-shares if kind == 'sell' else shares:.10f}", "price": f"{price:.6f}",
            "amount": f"{-gross if kind == 'buy' else gross:.2f}"}


def key(t):
    """What makes two rows the same transaction, but for the amount (compared with a cent's slack):
    date, depot, type, instrument and shares."""
    return (t["date"], depot_of(t).lower(), KIND.get(t["type"]), t["symbol"], round(abs(num(t["shares"])), 6))


def import_csv(portfolio, text):
    registry = registry_names()
    lines = parse(text)
    bad = [p for i, r in lines for p in check(i, r, registry)]
    if bad:
        sys.exit(f"{len(bad)} problem{'s' * (len(bad) > 1)} — nothing imported:\n  " + "\n  ".join(bad))

    ledger = active_ledger(portfolio)
    # each existing row can be the duplicate of one line, so two equal lines against one existing
    # row are a duplicate and a new transaction (a second buy at the same price, the same day).
    # A row deleted on the page counts too: re-pasting it does not bring it back.
    existing = defaultdict(list)
    for t in db.ledger(portfolio, "tr") + db.ledger(portfolio, "csv") + db.ledger(portfolio, "manual"):
        existing[key(t)].append(abs(num(t["amount"])))
    per_day = defaultdict(int)
    for t in ledger:
        per_day[t["date"]] += 1
    new, dupes = [], []
    # oldest first; lines of the same day keep their order (a buy, then selling part of it), and
    # each is booked a second after the one before — as manual_tx.py does
    for i, r in sorted(lines, key=lambda x: x[1]["date"]):
        when = datetime.strptime(r["date"], "%Y-%m-%d")
        at = (when + timedelta(hours=12, seconds=per_day[r["date"]])).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        row = ledger_row(r, at, registry)
        k, amount = key(row), abs(num(row["amount"]))
        match = next((a for a in existing[k] if abs(a - amount) < 0.011), None)
        if match is not None:
            existing[k].remove(match)
            dupes.append((i, r))
            continue
        per_day[r["date"]] += 1
        new.append((i, r, row))

    after = sorted(ledger + [row for _, _, row in new], key=lambda t: (t["datetime"], t["transaction_id"]))
    for depot, isin in sorted({(row["depot"], row["symbol"]) for _, r, row in new if row["type"] == "SELL"}):
        t = short_at(after, depot, isin)
        if t:
            sys.exit(f"{depot} would sell more {isin} than it holds on {t['date']} — nothing imported")

    db.add_ledger_rows(portfolio, "csv", [row for _, _, row in new], verified=False)
    print(f"{len(lines)} lines: {len(new)} new, {len(dupes)} duplicate{'s' * (len(dupes) != 1)} skipped")
    for i, r, _ in new:
        print(f"  new        line {i}: {describe(r, registry)}")
    for i, r in dupes:
        print(f"  duplicate  line {i}: {describe(r, registry)}")
    if new:
        print("New transactions are unverified until accepted — or deleted again, all at once.")


def describe(r, registry):
    kind = r["type"].lower()
    if kind in CASH_TYPES:
        return f"{r['date']} {r['depot']} {kind} {num(r['price']):g}"
    inst = registry.get(r["isin"].upper()) or {}
    extra = "".join(f" + {c} {num(r[c]):g}" for c in ("fee", "tax") if num(r[c]))
    return (f"{r['date']} {r['depot']} {kind} {num(r['shares']):g} × "
            f"{inst.get('display') or inst.get('name') or r['isin']} at {num(r['price']):g}{extra}")


def accept(portfolio):
    n = db.accept_unverified(portfolio)
    print(f"{n} new transaction{'s' * (n != 1)} accepted" if n else "no new transactions to accept")


def discard(portfolio):
    n = db.discard_unverified(portfolio)
    print(f"{n} new transaction{'s' * (n != 1)} deleted" if n else "no new transactions to delete")


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().split("\n\n")[1])
    portfolio, what = sys.argv[1:]
    text = None
    if what not in ("accept", "discard"):
        if what == "-":
            text = sys.stdin.read()
        else:
            with open(what, encoding="utf-8-sig") as fh:
                text = fh.read()
    # the change and the rebuild it causes land together, or (on any refusal) not at all
    with db.tx():
        require_manual(portfolio)
        if what == "accept":
            accept(portfolio)
        elif what == "discard":
            discard(portfolio)
        else:
            import_csv(portfolio, text)
        rebuild(portfolio)


if __name__ == "__main__":
    main()
