# Working in this repo

One page that draws a portfolio out of data imported from Parqet or Trade Republic. No build step,
no dependencies, no framework — served from a local directory and opened in a browser. All data
lives in one SQLite database, `data/portfolio.db`.

```
web/                    the page — all the server serves besides its routes
  portfolio.html          markup only; loads the css and the two scripts, in that order
  portfolio.css
  portfolio.model.js      data and arithmetic — never touches the DOM
  portfolio.view.js       everything that reads or writes the page
check_portfolio.js      regression check for both scripts (see below)
start.sh                checks the port, runs scripts/server.py, opens the page
scripts/                every Python script; each finds the repo as its own folder's parent, so it
                          runs from any working directory, and the others import from one another
  db.py                   THE way into the database — every script imports it, none opens the DB
                            itself; also its CLI: settings, registry rows, read-only queries, backup
  schema.sql              the database's tables; seed.sql the base a new one starts from
  sqlite-install.sh       sets a host up: installs sqlite3 if missing, creates the database
  api.py                  the api/ routes the page reads — each in the shape of the file it replaced
  server.py               the page's server: web/ on localhost, the api/ routes, plus
                            every route the page writes through — most just run a script below
  update_prices.py        fetches price history per the registry's price sources
  import_tr.py            imports a Trade Republic transaction export into a manual profile, and
                            rebuilds a manual profile's positions/activities/cash from its ledger
  import_parqet.py        imports a Parqet refresh (two CSVs) into a Parqet profile
  manual_tx.py            adds/edits/deletes a transaction in a manual profile (the page's backend)
  trade.py                trades a demo account from the command line — live only, JSON out
  manage-accounts.py      creates demo accounts, moves their cash, lists them, purges an obsolete
                            profile — same style; admin only, never given to an agent
  add_instrument.py       registers a new instrument (registry rows + price fetch) — the page's
                            "+ New instrument…"; also looks up Yahoo symbols for an ISIN
REFRESH_PARQET_DATA.md  how to pull fresh data from Parqet — a task for an agent with the MCP tools
data/portfolio.db       ALL the data — gitignored; backed up by hand (`scripts/db.py backup`)
private-profiles/<p>/exports/   import files only: the Trade Republic exports and staged Parqet
                          refreshes that were imported, kept as the record; gitignored
backup/                 the files the data came from before the database (config.json, registry/,
                          gen_prices/, gen_fx/, private-profiles/) — read by nothing; only
                          config.json and registry/ are committed
```

The database's tables (`scripts/schema.sql`) fall into three groups that differ by
**lifecycle**, and that is the distinction to preserve:

- **a profile's own** (IMPORTED) — `profile` (label, source, allow_cli), its keys in `setting`,
  `ledger`, and `position`/`activity`/`cash`. A Parqet profile's positions and activities are
  replaced wholesale by each refresh; a manual profile's `ledger` is its real source — every TR
  row imported (origin `tr`, kept even when deleted on the page, flagged `deleted`) and every
  hand-entered or traded row (origin `manual`) — and `position`/`activity`/`cash` are rebuilt from
  it on every change.
- **the registry** (CURATED) — `instrument`, `price_source`, `sector_color`, plus the global
  `setting`s. Hand- or agent-maintained, never clobbered by an import, not regenerable — losing it
  would cost real work, so back the database up.
- **the prices** (DERIVED) — `price`, `fx_rate`, `live_price`, and the view `latest_close` (newest
  close per instrument). Reproducible from `price_source` alone. `live_price` is a fresher price
  than any close (US pre-market from Yahoo, EU gettex mid in-session): the page makes it each
  series' last point, but it is never written into `price`.

Changes go through the scripts, never raw SQL: `db.py`'s own CLI for settings and registry rows
(`config`, `upsert`, `delete`), `add_instrument.py` to register an instrument, `import_tr.py` /
`import_parqet.py` / `manual_tx.py` for profiles. `db.py query` is read-only. The page reads only
through the `api/` routes, which answer in the old files' shapes (same CSV header, same JSON), so
its parsing never changed. Profile tables keep numbers as the text that was written; prices are
real numbers.

`instrument` gives every instrument an `id`, equal to its ISIN, and is the single answer to "what
exists and what is it called". `price_source` is the single answer to "where do this instrument's
prices come from" — keyed by that same `id`, **one row per instrument**, no fallback or priority.
A thin listing that needs a second source is registered as a second instrument instead (its own
`instrument` row, its own `id`, its own slug — e.g. a `hynix-frankfurt` alongside `hynix`), so
`price_source` never has to choose between two rows for one thing. Prices are converted to the
portfolio currency **on write**, with the original kept in `close_raw`; nothing in the browser does
FX. An instrument may be listed with no matching Parqet holding — that is how a benchmark or a
watchlist name gets charted. Parqet's cash accounts are not tracked: `build()` drops the export's
cash rows, since they carry no dated balance history and no past date could be reconstructed for
them. A TR-imported or manual profile *does* have that history — every booking — so its rebuild
writes `cash` (each booking's cash effect, by account), and the page shows the balance on a
date in a Cash tile of its own and counts it into **nothing else**: not the Current value /
"Value on" tile, not Invested, no gain, return or IRR, no benchmark, no map area. Moving
money to the broker is not an investment. Cash may go negative (a demo buy is never refused for
want of a deposit). Reconstructed TR cash matches Parqet's balance for the same account to the
cent, apart from TR's tax refunds, which Parqet leaves out.

**Profiles.** The page can show several portfolios (profiles of the same person — no access
control). Only a profile's own tables are per profile; the registry, the prices and the browser's
localStorage are shared, since an instrument is the same thing whoever holds it. So the registry
is the union of every profile's instruments, and a profile sees only its slice: what its
positions and activities hold (open or closed), its `watchlist` setting, and its benchmark. Being in the
registry without being held is **not** enough to show up in a profile's watchlist — that would leak
every other profile's holdings into it. The page picks the profile from `?profile=`, else the
global `defaultProfile` setting; a profile's own settings override the global ones (flat, shallow —
`label`, `watchlist`, `benchmarkIsin`, `benchmarkLabel`, `timelineStart`), except `currency`, which
stays shared because the price series are converted into it on write. The Update button runs
`scripts/update_prices.py --profile <p>`, fetching only that profile's instruments.

A profile is controlled **either** by Parqet **or** manually, and its `source` says
which: `parqet` — refreshed by REFRESH_PARQET_DATA.md (`import_parqet.py`), never imported into
otherwise — or `manual` — fed
by Trade Republic exports (`import_tr.py`) and/or buys and sells entered by hand (`manual_tx.py`,
for virtual demo portfolios; booked under portfolio "Manual", so never merged with a real
position), both on the Transactions tab, never refreshed from Parqet. That tab's "+ New
instrument…" writes to the registry (via `add_instrument.py`), the shared catalog. server.py,
`import_tr.py`, `manual_tx.py`, `import_parqet.py` and the page all check the source, so `main`
cannot be overwritten by a stray import. The TR conversion follows Parqet's conventions and was
checked against Parqet's own import of the same account; its docstring lists them.

**Agent accounts.** `scripts/manage-accounts.py` creates demo accounts, moves their cash and lists
them; `scripts/trade.py` trades them. Each one's `--help` is its whole interface, and every answer
is one JSON object. Both touch only profiles with `allow_cli` set (which `manage-accounts.py
create` sets); the scripts cannot tell an agent from a human, so that flag is the boundary — and
splitting the two lets an agent be handed trading alone: `manage-accounts.py` is admin only. Its
`purge` removes any obsolete profile (not the default one) after backing the database up. An agent's session is also
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
fee included. Trades land in the ledger like hand-entered ones (their `--reason` in its
description), so the page shows and can edit them — the one way round these rules; leave them
alone if accounts are to be compared.

The global settings (`python3 scripts/db.py config`, `... config set <key> '<json>'`) are what
an agent changes for the as-of picker's earliest date (`timelineStart`), the portfolio `currency`,
the benchmark (`benchmarkIsin`, `benchmarkLabel`) or the `defaultProfile`; `--profile <p>` does the
same for one profile's own. `ingest()` reads them (falling back to built-in defaults for a missing
key) and `update_prices.py` reads `timelineStart`/`currency` too, so the page and the fetcher move
together. Keep them flat.

**The database.** A new host runs `scripts/sqlite-install.sh` (sqlite3, then the database from
`schema.sql` + `seed.sql`), then `update_prices.py --from <timelineStart>`. `python3 scripts/db.py
seed > scripts/seed.sql` refreshes that seed from the current registry and settings. Backups are
by hand: `python3 scripts/db.py backup` writes a consistent, timestamped copy to the NAS
(`/Volumes/nas/bkp/portfolioviz` on macOS, `/mnt/nas/bkp/portfolioviz` on Linux) — copying the
file itself while the server runs is not safe. `$PORTFOLIO_DB` points every script at another
database file, e.g. a copy to try something on.

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
`check_portfolio.js` runs both scripts headlessly against the real data — the `api/` routes answered
by `scripts/api.py`, the same code the server uses — and records every computed figure and every
rendered tooltip. Around any edit:

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

`data/`, `private-profiles/`, `backup/`'s old data files and `check_baseline*.json` hold real
position values and are gitignored — never commit them, and don't paste figures from them into
commit messages or issues. Nothing in the database is committed, the registry included:
`scripts/seed.sql` (no amounts — only what each instrument is and where its prices come from,
though it does enumerate which instruments are held) is the committed base, and the NAS backup
the rest. The `.gitignore` reads `*.csv`, so a new CSV anywhere is ignored by default.
