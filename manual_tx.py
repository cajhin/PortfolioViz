#!/usr/bin/env python3
"""Enter buys and sells by hand into a manual profile — a virtual demo portfolio, say.

    python3 manual_tx.py <profile> add <YYYY-MM-DD> <id> buy|sell <shares> <price> [<fee>]
    python3 manual_tx.py <profile> edit <transaction_id> <YYYY-MM-DD> buy|sell <shares> <price> [<fee>]
    python3 manual_tx.py <profile> delete <transaction_id>

Rows go to private-profiles/<profile>/manual_ledger.csv in the same format as a Trade Republic
export row, so import_tr.py's one conversion turns both ledgers into the profile's positions.csv
and activities.csv — which every change here rebuilds. Prices are per share in the portfolio
currency, the fee is on top (a buy costs shares × price + fee, a sale brings shares × price − fee).

The instrument must be in registry/instruments.csv: a virtual position is only worth tracking if
it has a price series to track it by. No change — add, edit or delete — may leave the holding
short at any point in its history.
"""
import csv, os, sys, uuid
from datetime import date, datetime, timedelta

from import_tr import (LEDGER, MANUAL_LEDGER, TR_DELETED, num, manual_profile_dir, read_csv,
                       rebuild, registry_names, write_csv)

FIELDS = ["datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol",
          "shares", "price", "amount", "fee", "tax", "currency", "original_amount",
          "original_currency", "fx_rate", "description", "transaction_id"]


def never_short(rows, isin):
    """Exit unless the "Manual" portfolio's holding of `isin` stays at or above zero throughout.

    Hand-entered rows alone: that is where a hand-entered sale books, and shares imported from TR
    sit in a portfolio of their own and cannot cover it. Replaying the whole history, not just the
    date of the change, is what catches a buy edited down — or deleted — under a later sale."""
    held = 0.0
    for r in sorted((r for r in rows if r["symbol"] == isin), key=lambda r: r["datetime"]):
        held += num(r["shares"])
        if held < -1e-9:
            sys.exit(f"that would sell more than is held on {r['date']} "
                     f"({-num(r['shares']):g} sold, only {held - num(r['shares']):g} held)")


def make_row(rows, day, isin, kind, shares, price, fee, tid=None, keep_at=None):
    """A validated ledger row. `keep_at` holds an edited row's own timestamp when its date is
    unchanged, so it keeps its place among the same day's other entries."""
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
    # midday, then a second later per row already on that day — so same-day entries keep the order
    # they were made in (a buy, then selling part of it)
    at = keep_at or (datetime(when.year, when.month, when.day, 12) + timedelta(
        seconds=sum(1 for r in rows if r["date"] == day))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    gross = shares * price
    return {
        "datetime": at, "date": day, "account_type": "MANUAL", "category": "TRADING",
        "type": kind.upper(), "asset_class": "", "name": inst.get("name") or isin, "symbol": isin,
        "shares": f"{shares if kind == 'buy' else -shares:.10f}", "price": f"{price:.6f}",
        "amount": f"{-gross if kind == 'buy' else gross:.2f}", "fee": f"{-fee:.2f}" if fee else "",
        "tax": "", "currency": "EUR", "description": "entered by hand",
        "transaction_id": tid or f"manual-{uuid.uuid4()}"}, inst


def manual_rows(d):
    path = os.path.join(d, MANUAL_LEDGER)
    return path, (read_csv(path) if os.path.exists(path) else [])


def add(d, day, isin, kind, shares, price, fee):
    path, rows = manual_rows(d)
    row, inst = make_row(rows, day, isin, kind, shares, price, fee)
    rows.append(row)
    never_short(rows, isin)
    write_csv(path, FIELDS, sorted(rows, key=lambda r: r["datetime"]))
    print(f"added: {day} {kind} {num(shares):g} × {inst.get('display') or isin} at {num(price):g}"
          + (f" + fee {num(fee):g}" if num(fee) else ""))


def edit(d, tid, day, kind, shares, price, fee):
    """A hand-entered row rewritten in place — same id, same instrument. An imported row is a copy
    of what the broker booked, so it is not edited: delete it instead."""
    path, rows = manual_rows(d)
    old = next((r for r in rows if r["transaction_id"] == tid), None)
    if not old:
        sys.exit("only transactions entered by hand can be edited — an imported one can be deleted")
    rest = [r for r in rows if r is not old]
    row, inst = make_row(rest, day, old["symbol"], kind, shares, price, fee, tid=tid,
                         keep_at=old["datetime"] if old["date"] == day else None)
    rest.append(row)
    never_short(rest, old["symbol"])
    write_csv(path, FIELDS, sorted(rest, key=lambda r: r["datetime"]))
    print(f"edited: {day} {kind} {num(shares):g} × {inst.get('display') or old['symbol']} at {num(price):g}"
          + (f" + fee {num(fee):g}" if num(fee) else ""))


def delete(d, tid):
    """A hand-entered row is removed outright. An imported one stays in tr_ledger.csv — so the next
    import of an overlapping export still recognises it as seen — and is listed in tr_deleted.csv,
    which the rebuild leaves out. Deleting that line by hand restores it."""
    path, rows = manual_rows(d)
    keep = [r for r in rows if r["transaction_id"] != tid]
    if len(keep) < len(rows):
        gone = next(r for r in rows if r["transaction_id"] == tid)
        never_short(keep, gone["symbol"])          # a buy that a later sale still needs stays
        write_csv(path, FIELDS, keep)
        print(f"deleted {tid}")
        return
    ledger = os.path.join(d, LEDGER)
    if os.path.exists(ledger) and any(r["transaction_id"] == tid for r in read_csv(ledger)):
        gone_path = os.path.join(d, TR_DELETED)
        gone = read_csv(gone_path) if os.path.exists(gone_path) else []
        if all(r["transaction_id"] != tid for r in gone):
            write_csv(gone_path, ["transaction_id"], gone + [{"transaction_id": tid}])
        print(f"deleted imported {tid} (listed in {TR_DELETED}; a re-import will not bring it back)")
        return
    sys.exit(f"no transaction {tid!r} in this profile")


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[1] == "add" and len(args) in (7, 8):
        profile = args[0]
        d = manual_profile_dir(profile)
        add(d, *args[2:7], args[7] if len(args) == 8 else "0")
    elif len(args) in (7, 8) and args[1] == "edit":
        profile = args[0]
        d = manual_profile_dir(profile)
        edit(d, args[2], *args[3:7], args[7] if len(args) == 8 else "0")
    elif len(args) == 3 and args[1] == "delete":
        profile = args[0]
        d = manual_profile_dir(profile)
        delete(d, args[2])
    else:
        sys.exit(__doc__.strip().split("\n\n")[1])
    rebuild(profile, d)


if __name__ == "__main__":
    main()
