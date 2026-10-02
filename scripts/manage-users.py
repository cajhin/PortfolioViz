#!/usr/bin/env python3
"""Manage the page's users from the command line — JSON in every answer. Admin only, like
manage-portfolios.py.

    manage-users.py add     <user>                          # asks for the password
    manage-users.py passwd  <user>                          # asks for the new one; logs them out
    manage-users.py grant   <user> <portfolio>... [--read-only]
    manage-users.py revoke  <user> <portfolio>...
    manage-users.py delete  <user>
    manage-users.py list-users
    manage-users.py list-portfolios                           # every portfolio, and who may open it

server.py lets nobody in without a login, and shows a user only the portfolios granted to them —
scripts/db.py's `user_portfolio`. Anyone who can reach the page can also create a user there
("Create user"), who then sees only the portfolios they create. Every portfolio that existed
before, or that a script made (an agent's game, a Parqet refresh), is granted here. A grant
lets the user change the portfolio too (rename, import, transactions), unless --read-only.

A password is read from the terminal, never the command line (it would land in the shell's
history); piped in, its first line is the password. Every answer is one JSON object; on error
{"ok": false, "error": ..., "hint": ...}, exit code 1.
"""
import getpass, sqlite3, sys

import db
import server
import trade
from trade import Refusal


def password(prompt):
    if not sys.stdin.isatty():
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass(prompt, stream=sys.stderr)
        if getpass.getpass("again: ", stream=sys.stderr) != pw:
            raise Refusal("the two passwords differ")
    return pw


def existing(name):
    if not db.user(name):
        raise Refusal(f"no user {name!r}", "manage-users.py list-users shows them")
    return name


def add(name):
    if not server.USER_NAME.match(name):
        raise Refusal(f"{name!r} is not a user name", "letters, digits, '.', '-' and '_', at most 40")
    if db.user(name):
        raise Refusal(f"user {name!r} exists")
    try:
        db.create_user(name, password(f"password for {name}: "))
    except sqlite3.IntegrityError:
        raise Refusal(f"user {name!r} exists")
    return {"user": name}


def passwd(name):
    db.set_password(existing(name), password(f"new password for {name}: "))
    return {"user": name}


def grant(name, portfolios, read_only=False):
    existing(name)
    missing = [p for p in portfolios if not db.portfolio(p)]
    if missing:
        raise Refusal(f"no portfolio {', '.join(map(repr, missing))}",
                      "manage-users.py list-portfolios lists them")
    with db.tx():
        for p in portfolios:
            db.grant(name, p, not read_only)
    return {"user": name, "portfolios": {p: db.access(name, p) for p in portfolios}}


def revoke(name, portfolios):
    existing(name)
    with db.tx():
        gone = [p for p in portfolios if db.revoke(name, p)]
    return {"user": name, "revoked": gone}


def list_portfolios():
    users = db.users()
    return {"portfolios": [{"name": p["name"], "label": p["label"], "type": p["type"],
                          "users": {u["name"]: u["portfolios"][p["name"]] for u in users if p["name"] in u["portfolios"]}}
                         for p in db.portfolios()]}


def delete(name):
    db.delete_user(existing(name))
    return {"deleted": name}


def configure(sub):
    for cmd in ("add", "passwd", "delete"):
        sub.add_parser(cmd).add_argument("user")
    g = sub.add_parser("grant"); g.add_argument("user"); g.add_argument("portfolio", nargs="+")
    g.add_argument("--read-only", action="store_true")
    r = sub.add_parser("revoke"); r.add_argument("user"); r.add_argument("portfolio", nargs="+")
    sub.add_parser("list-users")
    sub.add_parser("list-portfolios")


def dispatch(a):
    if a.cmd == "add":
        return add(a.user)
    if a.cmd == "passwd":
        return passwd(a.user)
    if a.cmd == "grant":
        return grant(a.user, a.portfolio, a.read_only)
    if a.cmd == "revoke":
        return revoke(a.user, a.portfolio)
    if a.cmd == "delete":
        return delete(a.user)
    if a.cmd == "list-portfolios":
        return list_portfolios()
    return {"users": db.users()}


if __name__ == "__main__":
    trade.run(__doc__, "manage-users.py", configure, dispatch)
