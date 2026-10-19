#!/usr/bin/env python3
"""The instructions server.py serves to agents — an AI or a script using a portfolio over HTTP from
somewhere else, not developing this repo. Public, like login.html: an agent reads them before it
has a token.

    /agents.md   (and /llms.txt)  what the system is, how to get access, the routes, how to behave
    /agents/csv                   the PViz CSV format — import_csv.py's own docstring
    /agents/game-rules            a game's trading rules — trading-rules.md
    /agents/game-agent            the role of an agent trading a game: its session, its notes

The two specs are fetched on demand, so an agent reads only the one its work needs; both come from
the text the scripts themselves are written to, so they cannot drift from what is enforced. The
address they give is server.py's public one (the `publicUrl` setting, else where the request came
in) — an agent on another host cannot use "localhost".

    python3 scripts/agents.py [md | csv | game-rules] [BASE_URL]      # one of them on stdout
"""
import os, sys

import import_csv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def entry(base):
    return f"""# Portfolioviz — instructions for agents

Portfolioviz shows investment portfolios: positions, transactions, cash, gains. You use **one
portfolio** of **one user**, over HTTP at {base}/ — every path below is relative to it.

## Access

You need a **token** from your user. Ask them to open the portfolio on the page, go to Config,
tick **Allow AI (API) access, read-only** — or **read-write**, if you are to change anything — then
create a token there and press **Copy token**. Send it on every request:

    Authorization: Bearer <token>

The token is bound to that one portfolio: leave `?portfolio=` out, every route acts on it.

- **401**: the token is wrong or revoked — ask your user for a new one.
- **403**: the portfolio's owner has not allowed this (AI access, writing, or this kind of change).
  **Tell your user what they would have to allow. Never try to get around it.**

Start with `GET portfolios` — one entry: its `id`, `label`, `type` (`normal` or `game`), `write`
(whether your user may change it) and `ai` — what you may do: `1` read only, `2` read and write.
Then `GET notes`: its `brief` is your user's **special instructions** for this portfolio. They come
before everything else here — only the trading rules and the limits above are above them. Follow
them.

## Reading (any portfolio)

- `GET api/positions` — positions, open and closed (CSV)
- `GET api/activities` — transactions: buys, sells, dividends, taxes (CSV); `transactionId` names each
- `GET api/cash` — cash bookings per depot (CSV); deposits, withdrawals and interest are transactions too
- `GET export-csv` — every transaction in PViz CSV
- `GET instrument?isin=<ISIN>` — whether that instrument is registered, and its name (JSON)
- `GET instrument-search?find=<name or ISIN>` — instruments and their ISINs, registered or not (JSON)

## A normal portfolio: adding transactions

Everything you write lands **unverified**: it shows as "new" on the page until your user accepts it
there — or deletes it again. You cannot accept it. You may correct or withdraw your own rows while
they are still unverified; an accepted transaction is your user's — ask them to change it.
**After writing, tell your user to review and accept on the Transactions tab.**

- **Many, e.g. a statement: `POST import-csv`**, the PViz CSV as the body. **Read {base}/agents/csv
  before your first import.** Every line is checked first; on any problem nothing is imported and
  every problem is listed — fix them and send it again. Lines already in the portfolio are skipped,
  so sending the same file twice is safe. `POST import-csv?action=discard` withdraws every row not
  yet accepted.
- **Few: `POST manual-tx`**, one transaction per call, JSON:
  `{{"date": "2026-06-04", "depot": "Comdirect", "isin": "US0378331005", "type": "buy", "shares": 35, "price": 196.50, "fee": 6.80, "tax": 0}}`
  — `type` buy, sell or dividend; or `{{"date": …, "depot": …, "type": "deposit", "amount": 5000}}`
  for deposit, withdrawal or interest. Prices per share, in euros. There is no duplicate check
  here: before sending, make sure the transaction is not in `api/activities` already.
- **Correct or withdraw one of your unverified rows:** `POST manual-tx?edit=<transactionId>` (the same
  JSON; the instrument stays) or `POST manual-tx?delete=<transactionId>`.
- **Watchlist** — instruments the portfolio follows without holding them: `POST watchlist`
  `{{"add": "<ISIN>"}}` or `{{"remove": "<ISIN>"}}` (registered instruments only).
- **Instruments** must be registered before a transaction can name them. Find the ISIN with
  `instrument-search`, check it with `instrument?isin=`; if it is not registered, ask your user
  before registering it (this is for a normal portfolio — in a game, a buy registers an unknown
  ISIN by itself)
  (`POST instrument` `{{"isin", "symbol" (Yahoo), "name", "sector"}}`; `instrument-search?symbols=<ISIN>`
  lists the Yahoo symbols).

## A game portfolio: trading

No past and no edits — only buying and selling now, at a live price. **Read {base}/agents/game-rules
before your first trade.**

- `GET quote?instrument=<ISIN>` — what a buy and a sale would get right now, or why no trade is possible
- `POST trade` — `{{"side": "buy", "isin": "…", "eur": 2000, "reason": "…", "id": "<a fresh id>"}}`;
  `eur` is all that leaves the cash, fee included; or `"shares": 10`; a sale also `"all": true`.
  **Always send a fresh `id`** — any string unique to this trade, e.g. a UUID: resending the same
  one never trades twice, so a retry after a timeout is safe. The `reason` is kept with the trade.
  An ISIN the system does not know yet is registered by the buy itself — no need to ask.
- `GET game-status` — cash, positions valued live, result against the money put in
- `GET chart?instrument=<ISIN, ticker or name>` — returns over 1 month to 3 years, the 52-week range,
  volatility, the deepest drops, two years of month-end closes. Several `instrument=` at once, for
  `quote` too: then the answer is `{{"quotes": [...]}}` or `{{"charts": [...]}}`, one entry each, an
  instrument that failed with its own `"ok": false`
- **If you trade a game as its agent**, read {base}/agents/game-agent: how a session goes, and how
  you keep your memory, strategy and journal here.

## Notes (any portfolio)

What you keep between sessions lives on the server, so you can run anywhere: `GET notes` gives the
owner's special instructions (`brief` — read only, and binding), your **strategy** (versioned), your
**memory** and the newest **journal** entries. Write with `POST notes/memory` `{{"text": "…"}}`
(replaces it), `POST notes/strategy` `{{"text": "…", "reason": "…"}}` (a new version — the whole
text; the reason says what changed and why), `POST notes/journal` `{{"text": "…"}}` (appends).
`GET notes/strategy?history=1` lists the versions, `?version=N` reads one;
`GET notes/journal?limit=N`. Each at most 64,000 characters.

## How to behave

- Never guess an ISIN, a price, a date or a share count. Where a statement or an instruction is
  ambiguous, ask your user.
- Read the server's answer and report it — what was imported, skipped or refused, in its own words.
- Do only what your user asked, or what your role (e.g. a game) is for.
"""


def game_agent(base):
    return f"""# The game agent — how to trade a game portfolio

You manage a simulated portfolio: a **game**, with virtual cash, at {base}/ (read {base}/agents.md
for access and routes, and {base}/agents/game-rules for the rules). You run now and then — maybe daily,
maybe after three weeks — and remember nothing between sessions but what you keep in your notes here.

## Goals

Your owner's **special instructions** (`GET notes`, `brief`) say what you are for — e.g. "be
aggressive", "trade only tech". They take precedence over everything on this page; only the trading
rules stand above them. Where they say nothing:
- invest the cash in equities — stocks, bonds, ETFs; no options or futures
- aim beyond the common benchmarks (typically ~7–15% a year): +20% annualised is good, +50% very
  good, +100% great. Losses are accepted risk, but not catastrophic ones: −30% acceptable, −50% bad,
  −70% unacceptable
- develop strategies that could be applied to a real portfolio — this is not about winning a game.
  Exploiting flaws of the simulation teaches nothing; use it as intended

## How you work

- How you research and decide is yours to develop. Your universe is every listed equity and ETF
  worldwide, of any size — not only what you know or what makes headlines; `quote` and `chart` work
  for any ISIN, held or not, and you may research on the internet
- There is no time pressure: thorough work matters more than speed. Make decisions you could defend
  to an investment committee
- Explain every decision in writing — every trade carries its `reason`, and the journal the rest
- At each session, compare your earlier decisions with what has happened since, and improve your
  process

## A session

**Only during market hours.** Trading is live, so a session is only worth running while something
can be traded: gettex is open weekdays 08:00–22:00 German time (home exchanges only in their own
sessions — `quote` says whether an instrument can be traded now, and when). Outside those hours, do
nothing — no research, no trades, no notes — and end the session.

1. `GET notes` — your owner's special instructions, your strategy, memory and recent journal
2. `GET game-status` — cash, positions, result so far
3. Look back: what did you decide, and what has happened since?
4. Research; `GET quote` before any trade; `POST trade` with a `reason` and a fresh `id`
5. `POST notes/journal` — this session's decisions, including what you chose not to do, and why
6. `POST notes/memory` — what you want to recall next time (it replaces the last one)
7. Only when your strategy changes: `POST notes/strategy` with the whole new text and a `reason` —
   every version is kept, so the way it developed can be followed

Your owner sees your notes and trades on the page.
"""


def csv_spec():
    """import_csv.py's own description of the format and its checks — what an import enforces."""
    doc = import_csv.__doc__
    fmt = doc[doc.index("The format —"):doc.index("`export` writes")].rstrip()
    checks = doc[doc.index("Every line is checked"):doc.index("What is imported")].rstrip()
    return f"""# PViz CSV — the format of `POST import-csv` and `GET export-csv`

{fmt}

{checks}

Send it as the body of `POST import-csv`. What is imported is unverified until your user accepts it
on the page; `POST import-csv?action=discard` withdraws every row not yet accepted.
"""


# trading-rules.md is written for trade.py's command line; an agent over HTTP reads it in its own
# terms. Every phrase must still be in the file — a reworded rule would otherwise stay in trade.py's
# words without anyone noticing, so a miss fails loudly instead.
HTTP_TERMS = [
    ("The rules `trade.py` enforces on every game portfolio.", "The rules every trade on a game portfolio is held to."),
    ("**`trade.py quote <instrument> [<instrument> ...]`** shows", "**`GET quote?instrument=<ISIN>`** (several `instrument=` at once) shows"),
    ("`trade.py status` says which", "`GET game-status` says which"),
    ("`buy --eur X` spends exactly X", "`\"eur\": X` spends exactly X"),
    ("`--eur 2000` buys €1,990", "`\"eur\": 2000` buys €1,990"),
    ("`buy --shares N` buys N shares", "`\"shares\": N` buys N shares"),
    ("| `buy --eur 2000` |", "| buy, `\"eur\": 2000` |"),
    ("| `buy --shares 10` at €150 |", "| buy, `\"shares\": 10` at €150 |"),
    ("`trade.py status` reports the portfolio's **result**", "`GET game-status` reports the portfolio's **result**"),
]


def rules():
    """trading-rules.md, in HTTP's terms: GET quote and game-status, POST trade's "eur" and "shares"."""
    with open(os.path.join(ROOT, "trading-rules.md"), encoding="utf-8") as fh:
        text = fh.read()
    for cli, http in HTTP_TERMS:
        if cli not in text:
            raise ValueError(f"trading-rules.md no longer says {cli!r} — update agents.HTTP_TERMS")
        text = text.replace(cli, http)
    title, _, rest = text.partition("\n")
    return (f"{title}\n\nA buy or sell is `POST trade` (see agents.md); the amounts below are its fields.\n" + rest)


if __name__ == "__main__":
    args = sys.argv[1:]
    what, base = (args[0] if args else "md"), (args[1] if len(args) > 1 else "<server>")
    sys.stdout.write({"md": lambda: entry(base), "csv": csv_spec, "game-rules": rules,
                      "game-agent": lambda: game_agent(base)}[what]())
