#!/usr/bin/env python3
"""Manage game portfolios, and purge any portfolio, from the command line — live only, JSON in every answer. Admin only: a
trading agent is given trade.py and nothing else.

    manage-portfolios.py create      --label TEXT --cash EUR [--ai]
    manage-portfolios.py deposit     <portfolio> --eur EUR [--reason TEXT] [--id ID]
    manage-portfolios.py withdraw    <portfolio> --eur EUR [--reason TEXT] [--id ID]
    manage-portfolios.py list-games
    manage-portfolios.py purge       <portfolio> --yes (--backup-dir DIR | --no-backup)
    manage-portfolios.py renumber    --yes

Trading in a game portfolio is trade.py's; the rules both enforce are trading-rules.md. Every
answer is one JSON object; on error {"ok": false, "error": ..., "hint": ...}, exit code 1.

A <portfolio> is its id: pid1, pid2, … `create` makes a game portfolio — the only type these scripts
touch, and trade.py trades — under the next id (in its answer), and books the opening cash today.
--ai allows AI read-write access: an agent's trade.py session trades only such a portfolio (the
page's Config tab sets it, too). deposit and withdraw move cash today as well; there is no date to give. --id
makes a call safe to repeat: a second call with the same id answers with the first one's result
instead of moving the money twice. Cash can never go below zero: a withdrawal larger than the
portfolio holds is refused.

`list-games` shows every game portfolio as it stands — value at the latest close
on file, cash, and the result against the money put in, cash included, the measure to compare
portfolios by.

`purge` removes any obsolete portfolio, of any type, for good: every row it has in the database
(ledgers, positions, activities, cash, settings). The registry and the prices stay — they are
shared. Where the backup goes is not guessed: --backup-dir DIR backs the database up there first
(scripts/db.py backup; nothing is purged if that fails), --no-backup skips it. The default
portfolio cannot be purged.

`renumber` gives every portfolio still known by an older id (a name, before ids were numbers) the
next pid, oldest first — its data, grants, tokens and the default setting follow — and answers with
what became what. Whatever else names one — an agent's TRADE_PORTFOLIO, a link, a baseline file — is
the admin's to update.
"""
import contextlib, os, sqlite3

import db
import import_tr
import manual_tx
import trade
from trade import Refusal, locked, portfolio, quiet, require_game, today


def move_cash(pid, kind, eur, reason="", oid=None):
    require_game(pid)
    if not (eur or 0) > 0:
        raise Refusal("give --eur as a positive number")
    with locked(pid):
        rows = manual_tx.manual_rows(pid)
        tid = f"manual-{oid}" if oid else None
        if tid and any(r["transaction_id"] == tid for r in rows):
            return {"repeat": True, "id": oid}
        if kind == "withdrawal" and eur > trade.cash_of(pid) + 0.005:
            raise Refusal(f"the portfolio holds €{trade.cash_of(pid):.2f}, less than €{eur:g}",
                          "cash can never go below zero")
        row = quiet(manual_tx.make_cash_row, rows, today(), kind, eur, tid=tid)
        row["description"] = reason or "manage-portfolios.py"
        rows.append(row)
        with db.tx():
            manual_tx.save(pid, rows)
            quiet(import_tr.rebuild, pid)
        return {kind: eur, "date": today(), "id": row["transaction_id"].removeprefix("manual-")}


def create(label, eur, ai=False):
    label = " ".join((label or "").split())
    if not label:
        raise Refusal("give --label, the name the page shows")
    if not (eur or 0) >= 0:
        raise Refusal("give --cash as zero or more")
    pid = db.create_portfolio(label, "game", ai_access=db.AI_WRITE if ai else db.AI_NONE)
    quiet(import_tr.rebuild, pid)
    if eur:
        move_cash(pid, "deposit", eur, "opening balance")
    return {"portfolio": pid, "label": label, "cash": eur or 0, "date": today(), "ai_access": bool(ai)}


def renumber(yes=False):
    if not yes:
        raise Refusal("renumbering changes the id of every portfolio not yet known by a pid",
                      "add --yes to go ahead")
    return {"renumbered": [{"from": old, "to": new} for old, new in db.renumber_portfolios()]}


def list_games():
    out = []
    for p in db.portfolios():
        if p["type"] != "game":
            continue
        s = trade.summary(p["id"])
        out.append({k: s[k] for k in ("portfolio", "label", "cash", "value_positions", "value_total",
                                      "net_deposits", "result", "result_pct")}
                   | {"positions": len(s["positions"])})
    return {"date": today(), "portfolios": out}


def purge(pid, yes=False, backup_dir=None):
    """backup_dir None means no backup — the command line insists on one or the other."""
    if not db.portfolio(pid):
        raise Refusal(f"no portfolio {pid!r}")
    if pid == db.config().get("defaultPortfolio"):
        raise Refusal(f"{pid!r} is the default portfolio", "make another one the default first: "
                      "scripts/db.py config set defaultPortfolio '\"<pid>\"'")
    if not yes:
        raise Refusal(f"purging {pid!r} deletes all its data for good", "add --yes to go ahead")
    saved = None
    if backup_dir:
        try:
            saved = quiet(db.backup, backup_dir, f"before-purge-{pid}")
        except (Refusal, OSError, sqlite3.Error) as err:
            raise Refusal(f"no backup could be made, so nothing was purged: {getattr(err, 'error', err)}",
                          "give another --backup-dir, or --no-backup")
    with locked(pid):
        label = portfolio(pid).get("label") or pid
        db.purge_portfolio(pid)
    with contextlib.suppress(OSError):
        os.remove(os.path.join(os.path.dirname(db.DB_PATH), "locks", f"{pid}.lock"))
    return {"purged": pid, "label": label, "backup": saved}


def configure(sub):
    c = sub.add_parser("create"); c.add_argument("--label", required=True)
    c.add_argument("--cash", type=float, required=True); c.add_argument("--ai", action="store_true")
    for kind in ("deposit", "withdraw"):
        m = sub.add_parser(kind); m.add_argument("portfolio"); m.add_argument("--eur", type=float, required=True)
        m.add_argument("--reason", default=""); m.add_argument("--id")
    sub.add_parser("list-games")
    p = sub.add_parser("purge"); p.add_argument("portfolio")
    p.add_argument("--yes", action="store_true")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--backup-dir"); where.add_argument("--no-backup", action="store_true")
    sub.add_parser("renumber").add_argument("--yes", action="store_true")


def dispatch(a):
    if a.cmd == "create":
        return create(a.label, a.cash, a.ai)
    if a.cmd in ("deposit", "withdraw"):
        return move_cash(a.portfolio, "deposit" if a.cmd == "deposit" else "withdrawal", a.eur, a.reason, a.id)
    if a.cmd == "purge":
        return purge(a.portfolio, a.yes, a.backup_dir)
    if a.cmd == "renumber":
        return renumber(a.yes)
    return list_games()


if __name__ == "__main__":
    trade.run(__doc__, "manage-portfolios.py", configure, dispatch)
