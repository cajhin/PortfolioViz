#!/usr/bin/env python3
"""Enter transactions by hand into a normal portfolio — a virtual demo portfolio, say — and edit or
delete any of its transactions, imported ones included.

    python3 scripts/manual_tx.py <portfolio> add <YYYY-MM-DD> <id> buy|sell|dividend <shares> <price> [<fee>] [--tax T] [--depot D]
    python3 scripts/manual_tx.py <portfolio> edit <transaction_id> <YYYY-MM-DD> buy|sell|dividend <shares> <price> [<fee>] [--tax T] [--depot D]
    python3 scripts/manual_tx.py <portfolio> delete <transaction_id>
    python3 scripts/manual_tx.py <portfolio> cash <YYYY-MM-DD> deposit|withdrawal|interest <amount> [--depot D]
    python3 scripts/manual_tx.py <portfolio> edit-cash <transaction_id> <YYYY-MM-DD> deposit|withdrawal|interest <amount> [--depot D]
    --api: through the API (server.py, for a token) — a new row's id is api-…, not manual-…, and
           it is unverified until its user accepts it on the page; an edit or delete touches only
           a row still unverified — an accepted one is its user's to change

Rows go to the portfolio's ledger (origin 'manual') in the same format as a Trade Republic export
row, so import_tr.py's one conversion turns every ledger into the portfolio's positions,
activities and cash — which every change here rebuilds, in the same transaction. Prices are per
share in the portfolio currency (a dividend's: gross per share), the fee is on top (a buy costs
shares × price + fee, a sale brings shares × price − fee), and the tax is paid on a sale or withheld
from a dividend (negative on a sale: a refund). A new row books under depot "Manual" unless --depot
names another — the depot a real trade was made in, say, entered by hand rather than imported.

An edit rewrites a row in place, whatever its origin — hand-entered, a Trade Republic import or a
pasted CSV line: same id, same instrument, and its depot unless --depot moves it. An imported row
keeps its id, so re-importing the same export does not undo the edit. What the broker booked is the
user's to correct; a split or a warrant's exercise cannot be edited this way, only deleted.

The instrument must be in the registry: a virtual position is only worth tracking if
it has a price series to track it by. No add, edit or hand-entered delete may leave a depot's
holding short at any point in its history. Cash, on the other hand, may go negative: a buy is never
refused for want of a deposit, the balance just shows what was overspent.
"""
import sys, uuid
from datetime import date, datetime, timedelta

import db
from import_tr import (DIVIDEND_TYPES, MANUAL_DEPOT, active_ledger, depot_of, num, rebuild,
                       registry_names, require_normal, short_at)

FIELDS = ["datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol",
          "shares", "price", "amount", "fee", "tax", "currency", "original_amount",
          "original_currency", "fx_rate", "description", "transaction_id"]

# A row entered through the API (an agent's or a script's token — server.py passes --api) is told
# apart from one typed on the page by its id, api-… rather than manual-…, and lands unverified, as a
# pasted CSV does: its user accepts it, or rolls it back, on the page. Through the API, only such a
# row — one not yet accepted — can be edited or deleted.
VIA_API = False
DESCRIPTION = {False: "entered by hand", True: "entered through the API"}


def new_id():
    return f"{'api' if VIA_API else 'manual'}-{uuid.uuid4()}"


CASH_TYPES = {"deposit": "CUSTOMER_INBOUND", "withdrawal": "CUSTOMER_OUTBOUND_REQUEST",
              "interest": "INTEREST_PAYMENT"}
# the ledger types an edit may rewrite — as a trade or dividend, or as a cash booking
TRADE_EDITABLE = {"BUY", "SELL"} | DIVIDEND_TYPES
CASH_EDITABLE = {"CUSTOMER_INBOUND", "TRANSFER_INBOUND", "TRANSFER_INSTANT_INBOUND",
                 "CUSTOMER_OUTBOUND_REQUEST", "TRANSFER_OUTBOUND", "TRANSFER_INSTANT_OUTBOUND",
                 "INTEREST_PAYMENT"}


def never_short(after, isin, depot):
    """Exit unless the depot's holding of `isin` stays at or above zero throughout `after` — every
    active ledger row as it would stand after the change, whatever its origin (a hand-entered sale
    from a depot that imports cover). Replaying the whole history, not just the date of the change,
    is what catches a buy edited down — or deleted — under a later sale."""
    r = short_at(sorted(after, key=lambda t: (t["datetime"], t["transaction_id"])), depot, isin)
    if r:
        sys.exit(f"that would sell more {isin} than {depot} holds on {r['date']}")


def checked_depot(depot):
    depot = " ".join((depot or "").split())
    if not depot or len(depot) > 40:
        sys.exit("the depot needs a name, of at most 40 characters")
    return depot


def booked_in(row, depot):
    """The row, booked under `depot` — "Manual" is a hand-entered row's own and needs no saying."""
    if depot != MANUAL_DEPOT:
        row["depot"] = depot
    return row


def checked_day(day):
    try:
        when = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"date {day!r} is not YYYY-MM-DD")
    if when > date.today():
        sys.exit(f"{day} is in the future")
    return when


def stamp(rows, when, day, keep_at):
    """Midday, then a second later per row already on that day — so same-day entries keep the order
    they were made in (a buy, then selling part of it). `keep_at` holds an edited row's own
    timestamp when its date is unchanged, so it keeps its place among the same day's others."""
    return keep_at or (datetime(when.year, when.month, when.day, 12) + timedelta(
        seconds=sum(1 for r in rows if r["date"] == day))).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_row(rows, day, isin, kind, shares, price, fee, tid=None, keep_at=None, tax=0):
    """A validated ledger row: a buy, a sale or a dividend."""
    when = checked_day(day)
    inst = registry_names().get(isin)
    if not inst:
        sys.exit(f"{isin} is not in the registry — add it there first, so it has prices")
    if kind not in ("buy", "sell", "dividend"):
        sys.exit(f"type must be buy, sell or dividend, not {kind!r}")
    shares, price, fee, tax = num(shares), num(price), num(fee), num(tax)
    if shares <= 0 or price <= 0 or fee < 0:
        sys.exit("shares and price must be positive, the fee zero or more")
    if kind == "dividend" and fee:
        sys.exit("a dividend has no fee — only tax withheld")
    if tax < 0 and kind != "sell":
        sys.exit("only a sale's tax can be negative (a refund)")
    gross = shares * price
    return {
        "datetime": stamp(rows, when, day, keep_at), "date": day, "account_type": "MANUAL",
        "category": "TRADING", "type": kind.upper(), "asset_class": "",
        "name": inst.get("name") or isin, "symbol": isin,
        "shares": f"{-shares if kind == 'sell' else shares:.10f}", "price": f"{price:.6f}",
        "amount": f"{-gross if kind == 'buy' else gross:.2f}", "fee": f"{-fee:.2f}" if fee else "",
        "tax": f"{-tax:.2f}" if tax else "", "currency": "EUR", "description": DESCRIPTION[VIA_API],
        "transaction_id": tid or new_id()}, inst


def make_cash_row(rows, day, kind, amount, tid=None, keep_at=None):
    """A deposit, a withdrawal or interest — the TR export's own types for the same thing, so the
    one conversion books it (into cash, see import_tr.cash_rows)."""
    when = checked_day(day)
    if kind not in CASH_TYPES:
        sys.exit(f"cash type must be deposit, withdrawal or interest, not {kind!r}")
    amount = num(amount)
    if amount <= 0:
        sys.exit("the amount must be positive")
    return {"datetime": stamp(rows, when, day, keep_at), "date": day, "account_type": "MANUAL",
            "category": "CASH", "type": CASH_TYPES[kind], "asset_class": "", "name": "", "symbol": "",
            "shares": "", "price": "", "amount": f"{-amount if kind == 'withdrawal' else amount:.2f}",
            "fee": "", "tax": "", "currency": "EUR", "description": DESCRIPTION[VIA_API],
            "transaction_id": tid or new_id()}


def manual_rows(portfolio):
    """The portfolio's hand-entered rows, oldest first."""
    return db.ledger(portfolio, "manual")


def save(portfolio, rows, new=None):
    """The hand-entered rows as they now stand, all of them, oldest first; `new` the one just added —
    unverified if it came through the API."""
    db.replace_ledger(portfolio, "manual", sorted(rows, key=lambda r: r["datetime"]),
                      unverified={new["transaction_id"]} if new and VIA_API else ())


def require_pending(portfolio, tid):
    """Through the API, exit unless the row is one not yet accepted."""
    if VIA_API and db.is_verified(portfolio, tid):
        sys.exit("that transaction is accepted — only its user can change it, on the page; ask them")


def describe(day, kind, shares, price, fee, tax, name):
    return (f"{day} {kind} {num(shares):g} × {name} at {num(price):g}"
            + (f" + fee {num(fee):g}" if num(fee) else "") + (f" + tax {num(tax):g}" if num(tax) else ""))


def add(portfolio, day, isin, kind, shares, price, fee, depot=MANUAL_DEPOT, tax=0):
    rows = manual_rows(portfolio)
    depot = checked_depot(depot)
    row, inst = make_row(rows, day, isin, kind, shares, price, fee, tax=tax)
    booked_in(row, depot)
    never_short(active_ledger(portfolio) + [row], isin, depot)
    save(portfolio, rows + [row], new=row)
    print("added: " + describe(day, kind, shares, price, fee, tax, inst.get("display") or isin))


def cash(portfolio, day, kind, amount, depot=MANUAL_DEPOT):
    rows = manual_rows(portfolio)
    row = booked_in(make_cash_row(rows, day, kind, amount), checked_depot(depot))
    save(portfolio, rows + [row], new=row)
    print(f"added: {day} {kind} {num(amount):g}")


def editable(portfolio, tid, types, what):
    """(the row to edit, every other active ledger row), or exit."""
    entry = db.ledger_entry(portfolio, tid)
    if not entry:
        sys.exit(f"no transaction {tid!r} in this portfolio")
    old = entry[1]
    if old["type"] not in types:
        sys.exit(f"a {old['type']} row is not {what} — it can be deleted, not edited this way")
    return old, [r for r in active_ledger(portfolio) if r["transaction_id"] != tid]


def edit(portfolio, tid, day, kind, shares, price, fee, depot=None, tax=0):
    """Any buy, sale or dividend rewritten in place — what it says now, over what it said, so an
    imported row keeps whatever else the broker's export had on it (its original currency, say)."""
    require_pending(portfolio, tid)
    old, rest = editable(portfolio, tid, TRADE_EDITABLE, "a buy, sale or dividend")
    row, inst = make_row(rest, day, old["symbol"], kind, shares, price, fee, tid=tid, tax=tax,
                         keep_at=old["datetime"] if old["date"] == day else None)
    to = checked_depot(depot) if depot else depot_of(old)
    new = {**old, **{k: row[k] for k in ("datetime", "date", "type", "shares", "price", "amount",
                                         "fee", "tax")}, "depot": to}
    for d in {depot_of(old), to}:
        never_short(rest + [new], old["symbol"], d)
    db.update_ledger_row(portfolio, tid, new)
    print("edited: " + describe(day, kind, shares, price, fee, tax, inst.get("display") or old["symbol"]))


def edit_cash(portfolio, tid, day, kind, amount, depot=None):
    """Any deposit, withdrawal or interest rewritten in place — its tax, if the export had one
    (interest), kept."""
    require_pending(portfolio, tid)
    old, rest = editable(portfolio, tid, CASH_EDITABLE, "a deposit, withdrawal or interest")
    row = make_cash_row(rest, day, kind, amount, tid=tid,
                        keep_at=old["datetime"] if old["date"] == day else None)
    to = checked_depot(depot) if depot else depot_of(old)
    db.update_ledger_row(portfolio, tid, {**old, **{k: row[k] for k in ("datetime", "date", "type", "amount")},
                                          "depot": to})
    print(f"edited: {day} {kind} {num(amount):g}")


def delete(portfolio, tid):
    """A hand-entered row is removed outright. An imported one stays in the ledger — so the next
    import of an overlapping export still recognises it as seen — marked deleted, which the
    rebuild leaves out. Clearing that mark (ledger.deleted) restores it."""
    require_pending(portfolio, tid)
    rows = manual_rows(portfolio)
    keep = [r for r in rows if r["transaction_id"] != tid]
    if len(keep) < len(rows):
        gone = next(r for r in rows if r["transaction_id"] == tid)
        if gone["symbol"]:                         # a buy that a later sale still needs stays
            never_short([r for r in active_ledger(portfolio) if r["transaction_id"] != tid],
                        gone["symbol"], depot_of(gone))
        save(portfolio, keep)
        print(f"deleted {tid}")
        return
    if db.mark_deleted(portfolio, tid):
        print(f"deleted imported {tid} (marked deleted; a re-import will not bring it back)")
        return
    sys.exit(f"no transaction {tid!r} in this portfolio")


def main():
    global VIA_API
    args, opts = sys.argv[1:], {}
    if "--api" in args:
        args.remove("--api")
        VIA_API = True
    for opt in ("--depot", "--tax"):
        if opt in args:
            i = args.index(opt)
            if i + 1 >= len(args):
                sys.exit(f"{opt} needs a value")
            opts[opt[2:]] = args[i + 1]
            del args[i:i + 2]
    commands = {("add", 7): add, ("add", 8): add, ("edit", 7): edit, ("edit", 8): edit,
                ("cash", 5): cash, ("edit-cash", 6): edit_cash, ("delete", 3): delete}
    allowed = {add: {"depot", "tax"}, edit: {"depot", "tax"}, cash: {"depot"}, edit_cash: {"depot"}}
    fn = commands.get((args[1], len(args))) if len(args) >= 2 else None
    if not fn or not set(opts) <= allowed.get(fn, set()):
        sys.exit(__doc__.strip().split("\n\n")[1])
    rest = args[2:] + (["0"] if fn in (add, edit) and len(args) == 7 else [])   # no fee given
    # the change and the rebuild it causes land together, or (on any refusal) not at all
    with db.tx():
        portfolio = require_normal(args[0])
        fn(portfolio, *rest, **opts)
        rebuild(portfolio)


if __name__ == "__main__":
    main()
