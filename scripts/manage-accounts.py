#!/usr/bin/env python3
"""Manage demo accounts from the command line — live only, JSON in every answer. Admin only: a
trading agent is given trade.py and nothing else.

    manage-accounts.py create        <account> --cash EUR [--label TEXT]
    manage-accounts.py deposit       <account> --eur EUR [--reason TEXT] [--id ID]
    manage-accounts.py withdraw      <account> --eur EUR [--reason TEXT] [--id ID]
    manage-accounts.py list-accounts
    manage-accounts.py purge         <profile> --yes (--backup-dir DIR | --no-backup)

Trading in an account is trade.py's; the rules both enforce are trading-rules.md. Every answer is one JSON object; on error
{"ok": false, "error": ..., "hint": ...}, exit code 1.

`create` makes an account open to both scripts — its profile says allow-cli — and
books the opening cash today. deposit and withdraw move cash today as well; there is no date to
give. --id makes a call safe to repeat: a second call with the same id answers with the first
one's result instead of moving the money twice. Cash can never go below zero: a withdrawal larger
than the account holds is refused.

`list-accounts` shows every account open to these scripts as it stands — value at the latest close
on file, cash, and the result against the money put in, cash included, the measure to compare
accounts by.

`purge` removes any obsolete profile, account or not, for good: every row it has in the database
(ledgers, positions, activities, cash, settings). The registry and the prices stay — they are
shared. Where the backup goes is not guessed: --backup-dir DIR backs the database up there first
(scripts/db.py backup; nothing is purged if that fails), --no-backup skips it. The profile's
leftover folder, if any (private-profiles/<profile>/, its imported export files), is moved
to backup/purged/. The default profile cannot be purged; an agent's own folder under agents/ is
not touched.
"""
import contextlib, os, shutil, sqlite3
from datetime import datetime

import db
import import_tr
import manual_tx
import trade
from trade import Refusal, account, locked, profile, quiet, require_cli, today


def move_cash(name, kind, eur, reason="", oid=None):
    require_cli(name)
    if not (eur or 0) > 0:
        raise Refusal("give --eur as a positive number")
    with locked(name):
        rows = manual_tx.manual_rows(name)
        tid = f"manual-{oid}" if oid else None
        if tid and any(r["transaction_id"] == tid for r in rows):
            return {"repeat": True, "id": oid}
        if kind == "withdrawal" and eur > trade.cash_of(name) + 0.005:
            raise Refusal(f"the account holds €{trade.cash_of(name):.2f}, less than €{eur:g}",
                          "cash can never go below zero")
        row = quiet(manual_tx.make_cash_row, rows, today(), kind, eur, tid=tid)
        row["description"] = reason or "manage-accounts.py"
        rows.append(row)
        with db.tx():
            manual_tx.save(name, rows)
            quiet(import_tr.rebuild, name)
        return {kind: eur, "date": today(), "id": row["transaction_id"].removeprefix("manual-")}


def create(name, eur, label=""):
    account(name, must_exist=False)
    if db.profile(name):
        raise Refusal(f"account {name!r} exists", "pick another name, or use it as it is")
    if not (eur or 0) >= 0:
        raise Refusal("give --cash as zero or more")
    db.create_profile(name, label or name, "manual", allow_cli=True)
    quiet(import_tr.rebuild, name)
    if eur:
        move_cash(name, "deposit", eur, "opening balance")
    return {"account": name, "cash": eur or 0, "date": today()}


def list_accounts():
    out = []
    for p in db.profiles():
        if p["source"] != "manual" or not p["allow_cli"]:
            continue
        s = trade.summary(p["name"])
        out.append({k: s[k] for k in ("account", "label", "cash", "value_positions", "value_total",
                                      "net_deposits", "result", "result_pct")}
                   | {"positions": len(s["positions"])})
    return {"date": today(), "accounts": out}


def purge(name, yes=False, backup_dir=None):
    """backup_dir None means no backup — the command line insists on one or the other."""
    if not db.profile(name):
        raise Refusal(f"no profile {name!r}")
    if name == db.config().get("defaultProfile"):
        raise Refusal(f"{name!r} is the default profile", "make another one the default first: "
                      "scripts/db.py config set defaultProfile '\"<name>\"'")
    if not yes:
        raise Refusal(f"purging {name!r} deletes all its data for good", "add --yes to go ahead")
    saved = None
    if backup_dir:
        try:
            saved = quiet(db.backup, backup_dir, f"before-purge-{name}")
        except (Refusal, OSError, sqlite3.Error) as err:
            raise Refusal(f"no backup could be made, so nothing was purged: {getattr(err, 'error', err)}",
                          "give another --backup-dir, or --no-backup")
    with locked(name):
        label = profile(name).get("label") or name
        db.purge_profile(name)
    with contextlib.suppress(OSError):
        os.remove(os.path.join(os.path.dirname(db.DB_PATH), "locks", f"{name}.lock"))
    folder = os.path.join(db.ROOT, "private-profiles", name)
    moved = None
    if os.path.isdir(folder):
        moved = os.path.join(db.ROOT, "backup", "purged", f"{name}-{datetime.now():%Y%m%d-%H%M%S}")
        os.makedirs(os.path.dirname(moved), exist_ok=True)
        shutil.move(folder, moved)
    return {"purged": name, "label": label, "backup": saved,
            **({"folder_moved_to": os.path.relpath(moved, db.ROOT)} if moved else {})}


def configure(sub):
    c = sub.add_parser("create"); c.add_argument("account")
    c.add_argument("--cash", type=float, required=True); c.add_argument("--label", default="")
    for kind in ("deposit", "withdraw"):
        m = sub.add_parser(kind); m.add_argument("account"); m.add_argument("--eur", type=float, required=True)
        m.add_argument("--reason", default=""); m.add_argument("--id")
    sub.add_parser("list-accounts")
    p = sub.add_parser("purge"); p.add_argument("account")
    p.add_argument("--yes", action="store_true")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--backup-dir"); where.add_argument("--no-backup", action="store_true")


def dispatch(a):
    if a.cmd == "create":
        return create(a.account, a.cash, a.label)
    if a.cmd in ("deposit", "withdraw"):
        return move_cash(a.account, "deposit" if a.cmd == "deposit" else "withdrawal", a.eur, a.reason, a.id)
    if a.cmd == "purge":
        return purge(a.account, a.yes, a.backup_dir)
    return list_accounts()


if __name__ == "__main__":
    trade.run(__doc__, "manage-accounts.py", configure, dispatch)
