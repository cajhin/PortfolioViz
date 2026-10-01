#!/usr/bin/env python3
"""Enter buys and sells by hand into a manual profile — a virtual demo portfolio, say.

    python3 manual_tx.py <profile> add <YYYY-MM-DD> <id> buy|sell <shares> <price> [<fee>]
    python3 manual_tx.py <profile> delete <transaction_id>

Rows go to private-profiles/<profile>/manual_ledger.csv in the same format as a Trade Republic
export row, so import_tr.py's one conversion turns both ledgers into the profile's positions.csv
and activities.csv — which every change here rebuilds. Prices are per share in the portfolio
currency, the fee is on top (a buy costs shares × price + fee, a sale brings shares × price − fee).

The instrument must be in registry/instruments.csv: a virtual position is only worth tracking if
it has a price series to track it by. A sale may not exceed what is held at that date.
"""
import csv, os, sys, uuid
from datetime import date, datetime, timedelta

from import_tr import (LEDGER, MANUAL_LEDGER, num, manual_profile_dir, read_csv, rebuild,
                       registry_names, write_csv)

FIELDS = ["datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol",
          "shares", "price", "amount", "fee", "tax", "currency", "original_amount",
          "original_currency", "fx_rate", "description", "transaction_id"]


def held_at(d, isin, when):
    """Shares of `isin` held at datetime `when`, across both ledgers — splits included."""
    rows = []
    for name in (LEDGER, MANUAL_LEDGER):
        path = os.path.join(d, name)
        if os.path.exists(path):
            rows += [r for r in read_csv(path) if r["symbol"] == isin and r["datetime"] <= when]
    return sum(num(r["shares"]) for r in rows
               if r["type"] in ("BUY", "SELL", "WARRANT_EXERCISE", "SPLIT"))


def add(d, day, isin, kind, shares, price, fee):
    try:
        when = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"date {day!r} is not YYYY-MM-DD")
    if when > date.today():
        sys.exit(f"{day} is in the future")
    inst = registry_names().get(isin)
    if not inst:
        sys.exit(f"{isin} is not in registry/instruments.csv — add it there first, so it has prices")
    if kind not in ("buy", "sell"):
        sys.exit(f"type must be buy or sell, not {kind!r}")
    shares, price, fee = num(shares), num(price), num(fee)
    if shares <= 0 or price <= 0 or fee < 0:
        sys.exit("shares and price must be positive, the fee zero or more")

    path = os.path.join(d, MANUAL_LEDGER)
    rows = read_csv(path) if os.path.exists(path) else []
    # midday, then a second later per row already on that day — so same-day entries keep the order
    # they were made in (a buy, then selling part of it)
    stamp = datetime(when.year, when.month, when.day, 12) + timedelta(
        seconds=sum(1 for r in rows if r["date"] == day))
    at = stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if kind == "sell":
        held = held_at(d, isin, at)
        if shares > held + 1e-9:
            sys.exit(f"cannot sell {shares:g} — only {held:g} held on {day}")

    gross = shares * price
    rows.append({
        "datetime": at, "date": day, "account_type": "MANUAL", "category": "TRADING",
        "type": kind.upper(), "asset_class": "", "name": inst.get("name") or isin, "symbol": isin,
        "shares": f"{shares if kind == 'buy' else -shares:.10f}", "price": f"{price:.6f}",
        "amount": f"{-gross if kind == 'buy' else gross:.2f}", "fee": f"{-fee:.2f}" if fee else "",
        "tax": "", "currency": "EUR", "description": "entered by hand",
        "transaction_id": f"manual-{uuid.uuid4()}"})
    write_csv(path, FIELDS, sorted(rows, key=lambda r: r["datetime"]))
    print(f"added: {day} {kind} {shares:g} × {inst.get('display') or isin} at {price:g}"
          + (f" + fee {fee:g}" if fee else ""))


def delete(d, tid):
    path = os.path.join(d, MANUAL_LEDGER)
    rows = read_csv(path) if os.path.exists(path) else []
    keep = [r for r in rows if r["transaction_id"] != tid]
    if len(keep) == len(rows):
        sys.exit(f"no hand-entered transaction {tid!r}")
    write_csv(path, FIELDS, keep)
    print(f"deleted {tid}")


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[1] == "add" and len(args) in (7, 8):
        profile = args[0]
        d = manual_profile_dir(profile)
        add(d, *args[2:7], args[7] if len(args) == 8 else "0")
    elif len(args) == 3 and args[1] == "delete":
        profile = args[0]
        d = manual_profile_dir(profile)
        delete(d, args[2])
    else:
        sys.exit(__doc__.strip().split("\n\n")[1])
    rebuild(profile, d)


if __name__ == "__main__":
    main()
