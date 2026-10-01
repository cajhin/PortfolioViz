# Working in this repo

One page that draws a portfolio out of CSVs exported from Parqet. No build step, no dependencies,
no framework — served from a local directory and opened in a browser.

```
portfolio.html          markup only; loads the css and the two scripts, in that order
portfolio.css
portfolio.model.js      data and arithmetic — never touches the DOM
portfolio.view.js       everything that reads or writes the page
config.json             settings — timeline start, portfolio currency, benchmark ISIN, default profile
check_portfolio.js      regression check for both scripts (see below)
start.sh                serves the directory on localhost and opens the page
scripts/                every Python script; each finds the repo as its own folder's parent, so it
                          runs from any working directory, and the others import from one another
  update_prices.py        fetches price history per registry/price_sources.csv
  import_tr.py            imports a Trade Republic transaction export into a manual profile, and
                            rebuilds a manual profile's CSVs from its ledgers
  manual_tx.py            adds/edits/deletes a transaction in a manual profile (the page's backend)
  trade.py                trades a demo account from the command line — live only, JSON out
  manage-accounts.py      creates demo accounts, moves their cash, lists them — same style
  add_instrument.py       registers a new instrument (registry rows + price fetch) — the page's
                            "+ New instrument…"; also looks up Yahoo symbols for an ISIN
REFRESH_PARQET_DATA.md  how to pull fresh CSVs from Parqet — a task for an agent with the MCP tools
registry/*.csv          CURATED — instruments.csv, price_sources.csv; committed, not regenerable
private-profiles/<p>/   IMPORTED — one profile's positions.csv + activities.csv, plus its own
                          profile.json; gitignored, regenerate the CSVs. A TR-fed profile also
                          has tr_ledger.csv (every TR row imported, by transaction_id),
                          manual_ledger.csv (hand-entered rows, same format), tr_deleted.csv
                          (imported rows deleted on the page) — its real source; positions.csv,
                          activities.csv and cash.csv are rebuilt from these — and exports/
gen_prices/*.csv        DERIVED — <isin>-<slug>.csv per instrument, plus _latest.csv; gitignored
gen_fx/*.csv            DERIVED — one file per currency pair, for the conversion on write
```

The three data directories differ by **lifecycle**, and that is the distinction to preserve:
`private-profiles/<p>/` is overwritten wholesale by the refresh task (all but its `profile.json`), `registry/` is hand- or agent-maintained
and must never be clobbered by an import, and `gen_prices/` + `gen_fx/` are reproducible from
`registry/price_sources.csv` alone.

`registry/instruments.csv` gives every instrument an `id`, equal to its ISIN, and is the single
answer to "what exists and what is it called". `registry/price_sources.csv` is the single answer to "where do this instrument's prices
come from" — keyed by that same `id`, **one row per instrument**, no fallback or priority. A thin
listing that needs a second source is registered as a second instrument instead (`instruments.csv`
gets its own row, its own `id`, its own slug — e.g. a `hynix-frankfurt` alongside `hynix`), so
`price_sources.csv` never has to choose between two rows for one thing. Prices are converted to the
portfolio currency **on write**, with the original kept in `close_raw`; nothing in the browser does
FX. An instrument may be listed with no matching Parqet holding — that is how a benchmark or a
watchlist name gets charted. Parqet's cash accounts are not tracked: `build()` drops the export's
cash rows, since they carry no dated balance history and no past date could be reconstructed for
them. A TR-imported or manual profile *does* have that history — every booking — so its rebuild
writes `cash.csv` (each booking's cash effect, by account), and the page shows the balance on a
date in a Cash tile of its own and counts it into **nothing else**: not the Current value /
"Value on" tile, not Invested, no gain, return or IRR, no benchmark, no map area. Moving
money to the broker is not an investment. Cash may go negative (a demo buy is never refused for
want of a deposit). Reconstructed TR cash matches Parqet's balance for the same account to the
cent, apart from TR's tax refunds, which Parqet leaves out.

**Profiles.** The page can show several portfolios (profiles of the same person — no access
control). Only `private-profiles/<p>/` is per profile; `registry/`, `gen_prices/`, `gen_fx/` and the
browser's localStorage are shared, since an instrument is the same thing whoever holds it. So the
registry is the union of every profile's instruments, and a profile sees only its slice: what its
CSVs hold (open or closed), its `profile.json` `watchlist`, and its benchmark. Being in the
registry without being held is **not** enough to show up in a profile's watchlist — that would leak
every other profile's holdings into it. The page picks the profile from `?profile=`, else
`config.json`'s `defaultProfile`; `profile.json` keys override `config.json`'s (flat, shallow —
`label`, `watchlist`, `benchmarkIsin`, `benchmarkLabel`, `timelineStart`), except `currency`, which
stays shared because the price series are converted into it on write. The Update button runs
`scripts/update_prices.py --profile <p>`, fetching only that profile's instruments.

A profile is controlled **either** by Parqet **or** manually, and `profile.json`'s `source` says
which: `"parqet"` — refreshed by REFRESH_PARQET_DATA.md, never imported into — or `"manual"` — fed
by Trade Republic exports (`import_tr.py`) and/or buys and sells entered by hand (`manual_tx.py`,
for virtual demo portfolios; booked under portfolio "Manual", so never merged with a real
position), both on the Transactions tab, never refreshed from Parqet. That tab's "+ New
instrument…" writes to the **committed** `registry/` (via `add_instrument.py`) — so a page action
can leave a git change there, which is meant: the registry is the shared catalog. A missing `source` counts as Parqet, the side that refuses imports; start.sh,
`import_tr.py` and the page all hold to it, so `main` cannot be overwritten by a stray import. Its conversion follows Parqet's conventions and was checked
against Parqet's own import of the same account; its docstring lists them.

**Agent accounts.** `scripts/manage-accounts.py` creates demo accounts, moves their cash and lists
them; `scripts/trade.py` trades them. Each one's `--help` is its whole interface, and every answer
is one JSON object. Both touch only profiles whose `profile.json` has `"allow-cli": true` (which
`manage-accounts.py create` sets); the scripts cannot tell an agent from a human, so that flag is
the boundary — and splitting the two lets an agent be handed trading alone. An agent's session is also
bound to its own account: its `start-agent` sets `TRADE_ACCOUNT`, and `trade.py` refuses any other
(the agent cannot override it — a command not starting with trade.py's path is not allowed). Each
agent folder under `agents/` has its own config dir, so agents share no memory. It is **live only**: no date can be given. A trade executes at once at a live price: gettex's ask
(buy) or bid (sell) while gettex is open (weekdays 08:00–22:00 German time) and its quote is under
15 minutes old — read from onvista's unofficial API — else the home exchange via Yahoo while that is
open and its price under 30 minutes old, else not at all. `status`/`list-accounts` value a position
at gettex's bid while its home market is closed and gettex is open; otherwise at Yahoo's latest
price. Charts, history and the page stay on Yahoo's closes. A trade
costs a €10 fee, on top of the spread — gettex's own, or 1% each way at the home exchange, which has
no bid/ask; a sale also pays 20% tax on its gain. Cash can
never go below zero: buys (fee included) and withdrawals are refused past it. The rules, with
examples, are `trading-rules.md` — keep it in step with `trade.py`'s FEE_FIXED/TAX_RATE. `buy --eur` is the total that leaves the account,
fee included. Trades land in `manual_ledger.csv` like hand-entered ones (their `--reason` in its
description), so the page shows and can edit them — the one way round these rules; leave them
alone if accounts are to be compared.

`config.json` is committed, not generated — an agent edits it directly to change the as-of
picker's earliest date, the portfolio currency, or which ISIN is the benchmark. `ingest()` reads it
(falling back to built-in defaults if it's missing or malformed) and `update_prices.py` reads
`timelineStart`/`currency` too, so the page and the fetcher move together. Keep it to flat keys.

The two scripts are classic `<script>` tags sharing one global scope — no modules, no imports.
`portfolio.model.js` loads first and declares the state; `portfolio.view.js` reads it. The
dependency arrow points one way: **the model never calls into the view.** Keep it that way — it
is what lets the model be exercised without a browser.

## Running it

`fetch` is blocked on `file://`, so the page must be served:

```bash
./start.sh                         # serves on 8000 and opens portfolio.html
./start.sh 8080 -n                 # another port, no browser
```

## Editing the page

Each script opens with a map of its sections and the invariant it holds to — read that first, and
you will usually only need to open one of the two.

**Check your change against the baseline.** The page has no tests in the usual sense, but
`check_portfolio.js` runs both scripts headlessly against the real CSVs and records every computed
figure and every rendered tooltip. Around any edit:

```bash
node check_portfolio.js --save     # before: record what the code does today
node check_portfolio.js            # after: diff against it
node check_portfolio.js --profile x [--save]   # another profile, against check_baseline.x.json
```

A clean run means the numbers and the rendered text are untouched — worth having after any
refactor, since much of the arithmetic here (FIFO lots, XIRR, split detection, the as-of replay)
is easy to break in ways that still render fine. When a change is *meant* to move a number, read
the diff, agree with it, then `--save` over the baseline.

It cannot see layout, colour, or anything needing a real browser. Check those by eye.

## Data

`private-profiles/`, `gen_prices/`, `gen_fx/` and `check_baseline*.json` hold real position values and are gitignored
— never commit them, and don't paste figures from them into commit messages or issues.

`registry/*.csv` is the exception and **is** committed: two small tables no import can rebuild, and
losing them would cost real work. They carry no amounts — only what each instrument is and where
its prices come from — though `instruments.csv` does enumerate which instruments are held. The
`.gitignore` reads `*.csv` and then negates that one directory, so a new CSV anywhere else is
ignored by default.
