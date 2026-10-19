#!/usr/bin/env python3
"""An agent's notes on a portfolio, from the command line — what server.py's /notes routes serve.

    python3 scripts/notes.py <portfolio> show                         # brief, strategy, memory, journal
    python3 scripts/notes.py <portfolio> brief    <file | ->          # the owner's brief, replaced
    python3 scripts/notes.py <portfolio> memory   <file | ->          # the agent's memory, replaced
    python3 scripts/notes.py <portfolio> strategy <file | -> --reason TEXT   # a new strategy version
    python3 scripts/notes.py <portfolio> journal  <file | ->          # one journal entry, appended
    python3 scripts/notes.py <portfolio> history                      # the strategy's versions

An agent working on a portfolio — wherever it runs — keeps these on the server, through its token:
its memory, rewritten every session; its strategy, every change a new version with the reason for it,
so how it developed stays readable; and a journal of its decisions, only ever added to. The brief is
the owner's: what the agent is for (its goals, its limits) — the agent reads it, never writes it.
Each is at most db.NOTE_MAX characters.
"""
import json, sys

import db


def read(src):
    text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    if not text.strip():
        sys.exit("nothing to save — the text is empty")
    if len(text) > db.NOTE_MAX:
        sys.exit(f"{len(text)} characters — at most {db.NOTE_MAX}")
    return text


def main():
    args = sys.argv[1:]
    reason = ""
    if "--reason" in args:
        i = args.index("--reason")
        reason = " ".join(args[i + 1:i + 2]).strip()
        del args[i:i + 2]
    if len(args) < 2 or not db.portfolio(args[0]):
        sys.exit(__doc__.strip().split("\n\n")[1] if len(args) < 2 else f"no portfolio {args[0]!r}")
    pid, what, rest = args[0], args[1], args[2:]
    if what == "show" and not rest:
        print(json.dumps({"brief": db.note(pid, "brief"), "strategy": db.strategy(pid),
                          "memory": db.note(pid, "memory"), "journal": db.journal(pid)}, indent=1, ensure_ascii=False))
    elif what == "history" and not rest:
        print(json.dumps(db.strategy_history(pid), indent=1, ensure_ascii=False))
    elif what in ("brief", "memory") and len(rest) == 1:
        db.set_note(pid, what, read(rest[0]))
        print(f"{what} saved")
    elif what == "strategy" and len(rest) == 1:
        if not reason:
            sys.exit("a strategy version needs --reason: what changed, and why")
        print(f"strategy version {db.add_strategy(pid, read(rest[0]), reason)} saved")
    elif what == "journal" and len(rest) == 1:
        db.add_journal(pid, read(rest[0]))
        print("journal entry added")
    else:
        sys.exit(__doc__.strip().split("\n\n")[1])


if __name__ == "__main__":
    main()
