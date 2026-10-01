#!/usr/bin/env python3
"""Manage demo accounts from the command line — live only, JSON in every answer.

    manage-accounts.py create        <account> --cash EUR [--label TEXT]
    manage-accounts.py deposit       <account> --eur EUR [--reason TEXT] [--id ID]
    manage-accounts.py withdraw      <account> --eur EUR [--reason TEXT] [--id ID]
    manage-accounts.py list-accounts

Trading in an account is trade.py's; the rules both enforce are trading-rules.md. Every answer is one JSON object; on error
{"ok": false, "error": ..., "hint": ...}, exit code 1.

`create` makes an account open to both scripts — its profile.json says "allow-cli": true — and
books the opening cash today. deposit and withdraw move cash today as well; there is no date to
give. --id makes a call safe to repeat: a second call with the same id answers with the first
one's result instead of moving the money twice. Cash can never go below zero: a withdrawal larger
than the account holds is refused.

`list-accounts` shows every account open to these scripts as it stands — value at the latest close
on file, cash, and the result against the money put in, cash included, the measure to compare
accounts by.
"""
import json, os

import import_tr
import manual_tx
import trade
from import_tr import write_csv
from trade import Refusal, account_dir, locked, profile, quiet, require_cli, today


def move_cash(name, kind, eur, reason="", oid=None):
    d = require_cli(name)
    if not (eur or 0) > 0:
        raise Refusal("give --eur as a positive number")
    with locked(d):
        path, rows = manual_tx.manual_rows(d)
        tid = f"manual-{oid}" if oid else None
        if tid and any(r["transaction_id"] == tid for r in rows):
            return {"repeat": True, "id": oid}
        if kind == "withdrawal" and eur > trade.cash_of(d) + 0.005:
            raise Refusal(f"the account holds €{trade.cash_of(d):.2f}, less than €{eur:g}",
                          "cash can never go below zero")
        row = quiet(manual_tx.make_cash_row, rows, today(), kind, eur, tid=tid)
        row["description"] = reason or "manage-accounts.py"
        rows.append(row)
        write_csv(path, manual_tx.FIELDS, sorted(rows, key=lambda r: r["datetime"]))
        quiet(import_tr.rebuild, name, d)
        return {kind: eur, "date": today(), "id": row["transaction_id"].removeprefix("manual-")}


def create(name, eur, label=""):
    d = account_dir(name, must_exist=False)
    if os.path.exists(d):
        raise Refusal(f"account {name!r} exists", "pick another name, or use it as it is")
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


def list_accounts():
    out = []
    for name in sorted(os.listdir(trade.PROFILES)) if os.path.isdir(trade.PROFILES) else []:
        d = os.path.join(trade.PROFILES, name)
        cfg = profile(d)
        if not os.path.isdir(d) or cfg.get("source") != "manual" or cfg.get("allow-cli") is not True:
            continue
        s = trade.summary(name, d)
        out.append({k: s[k] for k in ("account", "label", "cash", "value_positions", "value_total",
                                      "net_deposits", "result", "result_pct")}
                   | {"positions": len(s["positions"])})
    return {"date": today(), "accounts": out}


def configure(sub):
    c = sub.add_parser("create"); c.add_argument("account")
    c.add_argument("--cash", type=float, required=True); c.add_argument("--label", default="")
    for kind in ("deposit", "withdraw"):
        m = sub.add_parser(kind); m.add_argument("account"); m.add_argument("--eur", type=float, required=True)
        m.add_argument("--reason", default=""); m.add_argument("--id")
    sub.add_parser("list-accounts")


def dispatch(a):
    if a.cmd == "create":
        return create(a.account, a.cash, a.label)
    if a.cmd in ("deposit", "withdraw"):
        return move_cash(a.account, "deposit" if a.cmd == "deposit" else "withdrawal", a.eur, a.reason, a.id)
    return list_accounts()


if __name__ == "__main__":
    trade.run(__doc__, "manage-accounts.py", configure, dispatch)
