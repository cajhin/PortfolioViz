#!/usr/bin/env python3
"""Enter buys and sells by hand into a manual profile — a virtual demo portfolio, say.

    python3 scripts/manual_tx.py <profile> add <YYYY-MM-DD> <id> buy|sell <shares> <price> [<fee>]
    python3 scripts/manual_tx.py <profile> edit <transaction_id> <YYYY-MM-DD> buy|sell <shares> <price> [<fee>]
    python3 scripts/manual_tx.py <profile> delete <transaction_id>
    python3 scripts/manual_tx.py <profile> cash <YYYY-MM-DD> deposit|withdrawal <amount>
    python3 scripts/manual_tx.py <profile> edit-cash <transaction_id> <YYYY-MM-DD> deposit|withdrawal <amount>

Rows go to the profile's ledger (origin 'manual') in the same format as a Trade Republic export
row, so import_tr.py's one conversion turns both ledgers into the profile's positions,
activities and cash — which every change here rebuilds, in the same transaction. Prices are per share in the portfolio
currency, the fee is on top (a buy costs shares × price + fee, a sale brings shares × price − fee).

The instrument must be in the registry: a virtual position is only worth tracking if
it has a price series to track it by. No change — add, edit or delete — may leave the holding
short at any point in its history. Cash, on the other hand, may go negative: a buy is never
refused for want of a deposit, the balance just shows what was overspent.
"""
import sys, uuid
from datetime import date, datetime, timedelta

import db
from import_tr import num, rebuild, registry_names, require_manual

FIELDS = ["datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol",
          "shares", "price", "amount", "fee", "tax", "currency", "original_amount",
          "original_currency", "fx_rate", "description", "transaction_id"]


def never_short(rows, isin):
    """Exit unless the "Manual" depot's holding of `isin` stays at or above zero throughout.

    Hand-entered rows alone: that is where a hand-entered sale books, and shares imported from TR
    sit in a depot of their own and cannot cover it. Replaying the whole history, not just the
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
        sys.exit(f"{isin} is not in the registry — add it there first, so it has prices")
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


CASH_TYPES = {"deposit": "CUSTOMER_INBOUND", "withdrawal": "CUSTOMER_OUTBOUND_REQUEST"}


def make_cash_row(rows, day, kind, amount, tid=None, keep_at=None):
    """A deposit to, or withdrawal from, the "Manual" depot's cash — the TR export's own types
    for the same thing, so the one conversion books it (into cash, see import_tr.cash_rows)."""
    try:
        when = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"date {day!r} is not YYYY-MM-DD")
    if when > date.today():
        sys.exit(f"{day} is in the future")
    if kind not in CASH_TYPES:
        sys.exit(f"cash type must be deposit or withdrawal, not {kind!r}")
    amount = num(amount)
    if amount <= 0:
        sys.exit("the amount must be positive")
    at = keep_at or (datetime(when.year, when.month, when.day, 12) + timedelta(
        seconds=sum(1 for r in rows if r["date"] == day))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"datetime": at, "date": day, "account_type": "MANUAL", "category": "CASH",
            "type": CASH_TYPES[kind], "asset_class": "", "name": "", "symbol": "", "shares": "",
            "price": "", "amount": f"{amount if kind == 'deposit' else -amount:.2f}", "fee": "",
            "tax": "", "currency": "EUR", "description": "entered by hand",
            "transaction_id": tid or f"manual-{uuid.uuid4()}"}


def cash(profile, day, kind, amount):
    rows = manual_rows(profile)
    rows.append(make_cash_row(rows, day, kind, amount))
    save(profile, rows)
    print(f"added: {day} {kind} {num(amount):g}")


def edit_cash(profile, tid, day, kind, amount):
    rows = manual_rows(profile)
    old = next((r for r in rows if r["transaction_id"] == tid and r["category"] == "CASH"), None)
    if not old:
        sys.exit("only deposits and withdrawals entered by hand can be edited this way")
    rest = [r for r in rows if r is not old]
    rest.append(make_cash_row(rest, day, kind, amount, tid=tid,
                              keep_at=old["datetime"] if old["date"] == day else None))
    save(profile, rest)
    print(f"edited: {day} {kind} {num(amount):g}")


def manual_rows(profile):
    """The profile's hand-entered rows, oldest first."""
    return db.ledger(profile, "manual")


def save(profile, rows):
    """The hand-entered rows as they now stand, all of them, oldest first."""
    db.replace_ledger(profile, "manual", sorted(rows, key=lambda r: r["datetime"]))


def add(profile, day, isin, kind, shares, price, fee):
    rows = manual_rows(profile)
    row, inst = make_row(rows, day, isin, kind, shares, price, fee)
    rows.append(row)
    never_short(rows, isin)
    save(profile, rows)
    print(f"added: {day} {kind} {num(shares):g} × {inst.get('display') or isin} at {num(price):g}"
          + (f" + fee {num(fee):g}" if num(fee) else ""))


def edit(profile, tid, day, kind, shares, price, fee):
    """A hand-entered row rewritten in place — same id, same instrument. An imported row is a copy
    of what the broker booked, so it is not edited: delete it instead."""
    rows = manual_rows(profile)
    old = next((r for r in rows if r["transaction_id"] == tid and r["category"] != "CASH"), None)
    if not old:
        sys.exit("only buys and sells entered by hand can be edited — an imported one can be deleted")
    rest = [r for r in rows if r is not old]
    row, inst = make_row(rest, day, old["symbol"], kind, shares, price, fee, tid=tid,
                         keep_at=old["datetime"] if old["date"] == day else None)
    rest.append(row)
    never_short(rest, old["symbol"])
    save(profile, rest)
    print(f"edited: {day} {kind} {num(shares):g} × {inst.get('display') or old['symbol']} at {num(price):g}"
          + (f" + fee {num(fee):g}" if num(fee) else ""))


def delete(profile, tid):
    """A hand-entered row is removed outright. An imported one stays in the ledger — so the next
    import of an overlapping export still recognises it as seen — marked deleted, which the
    rebuild leaves out. Clearing that mark (ledger.deleted) restores it."""
    rows = manual_rows(profile)
    keep = [r for r in rows if r["transaction_id"] != tid]
    if len(keep) < len(rows):
        gone = next(r for r in rows if r["transaction_id"] == tid)
        never_short(keep, gone["symbol"])          # a buy that a later sale still needs stays
        save(profile, keep)
        print(f"deleted {tid}")
        return
    if db.mark_deleted(profile, tid):
        print(f"deleted imported {tid} (marked deleted; a re-import will not bring it back)")
        return
    sys.exit(f"no transaction {tid!r} in this profile")


def main():
    args = sys.argv[1:]
    commands = {("add", 7): add, ("add", 8): add, ("edit", 7): edit, ("edit", 8): edit,
                ("cash", 5): cash, ("edit-cash", 6): edit_cash, ("delete", 3): delete}
    fn = commands.get((args[1], len(args))) if len(args) >= 2 else None
    if not fn:
        sys.exit(__doc__.strip().split("\n\n")[1])
    rest = args[2:] + (["0"] if fn in (add, edit) and len(args) == 7 else [])   # no fee given
    # the change and the rebuild it causes land together, or (on any refusal) not at all
    with db.tx():
        profile = require_manual(args[0])
        fn(profile, *rest)
        rebuild(profile)


if __name__ == "__main__":
    main()
