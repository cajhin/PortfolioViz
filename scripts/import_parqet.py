#!/usr/bin/env python3
"""Import a Parqet refresh — the positions and activities REFRESH_PARQET_DATA.md pulls — into a portfolio.

    python3 scripts/import_parqet.py <portfolio> <positions.csv> <activities.csv>

Parqet portfolios only: a manual one is rebuilt from its own ledger (import_tr.py), and this would
throw that away. The two files are what the refresh task writes, in Parqet's schema bar one name —
a portfolio in Parqet is a `depot` here (see REFRESH_PARQET_DATA.md, sections 3 and 4); they replace
the portfolio's positions and activities wholesale, in one transaction — the page sees the old data or the new, never a mix. Kept where the
task wrote them (private-portfolios/<portfolio>/exports/), they are the record of what was imported.

Before it writes anything, checks what REFRESH_PARQET_DATA.md asks to hold: every activity named,
every (depot, ISIN) traded also a position, every sale's net amount = gross − tax − fee. A
failed check refuses the import. After it, lists any held instrument the registry lacks, or that
has no price source.
"""
import sys

import db
from import_tr import num, read_csv

POSITION_FIELDS = db.POSITION_FIELDS
ACTIVITY_FIELDS = db.ACTIVITY_FIELDS[:-1]          # Parqet's own schema has no transactionId


def load(path, fields):
    rows = read_csv(path)
    head = list(rows[0].keys()) if rows else fields
    if head != fields:
        sys.exit(f"{path}: header must be exactly\n  {','.join(fields)}\nnot\n  {','.join(head)}")
    return rows


def check(positions, activities):
    problems = []
    unnamed = sum(1 for r in activities if not r["name"])
    if unnamed:
        problems.append(f"{unnamed} activities have no name")
    orphans = {(r["depot"], r["identifier"]) for r in activities} \
        - {(p["depot"], p["identifier"]) for p in positions}
    if orphans:
        problems.append(f"activities with no matching position: {sorted(orphans)}")
    bad = [r for r in activities if r["type"] == "sell"
           and abs(num(r["amount"]) - num(r["tax"]) - num(r["fee"]) - num(r["amountNet"])) > 0.02]
    if bad:
        problems.append(f"{len(bad)} sells where gross − tax − fee ≠ net")
    return problems


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__.strip().split("\n\n")[1])
    portfolio, pos_path, act_path = sys.argv[1:]
    p = db.portfolio(portfolio)
    if not p:
        sys.exit(f"no portfolio {portfolio!r} (create it on the page's Config tab)")
    if p["type"] != "parqet":
        sys.exit(f"{portfolio} is a {p['type']} portfolio — its data comes from its own ledger, not Parqet")
    positions, activities = load(pos_path, POSITION_FIELDS), load(act_path, ACTIVITY_FIELDS)
    problems = check(positions, activities)
    if problems:
        sys.exit("not imported:\n  " + "\n  ".join(problems))
    with db.tx():
        db.replace_portfolio_rows(portfolio, "position", positions)
        db.replace_portfolio_rows(portfolio, "activity", activities)

    open_n = sum(1 for r in positions if r["isSold"] == "0" and r["assetType"] != "cash")
    kinds = {}
    for r in activities:
        kinds[r["type"]] = kinds.get(r["type"], 0) + 1
    print(f"{portfolio}: {len(positions)} positions ({open_n} open), {len(activities)} activities "
          + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    print(f"  current value {sum(num(r['currentValue']) for r in positions):.2f}, "
          f"total tax {sum(num(r['tax']) for r in activities):.2f}")
    known, sources = {r["id"] for r in db.instruments()}, db.sources()
    held = {r["identifier"] for r in positions if r["identifier"]}
    if held - known:
        print(f"  not in the registry yet: {', '.join(sorted(held - known))}")
    if (held & known) - set(sources):
        print(f"  no price source: {', '.join(sorted((held & known) - set(sources)))}")


if __name__ == "__main__":
    main()
