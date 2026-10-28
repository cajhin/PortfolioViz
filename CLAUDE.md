# Working in this repo

One page that draws a portfolio out of transactions imported (Trade Republic, any broker as CSV) or
entered by hand — or traded live, in a game. No build step,
no dependencies, no framework — served from a local directory and opened in a browser. All data
lives in one SQLite database, `data/portfolio.db`.

```
web/                    the page — all the server serves besides its routes
  portfolioviz.html       markup only; loads the css and the two scripts, in that order
  login.html              log in or create a user — server.py's login, see **Users**
  portfolioviz.css
  portfolioviz.model.js   data and arithmetic — never touches the DOM
  portfolioviz.view.js    everything that reads or writes the page
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
  import_tr.py            imports a Trade Republic transaction export into a normal portfolio, and
                            rebuilds a portfolio's positions/activities/cash from its ledger
  import_csv.py           imports transactions pasted in the Portfolioviz CSV format, PViz CSV (any
                            broker, converted by hand, script or AI) into a normal portfolio —
                            unverified until accepted, or rolled back — and exports a portfolio in
                            it, to replicate it in another; its docstring is the format's spec
  manual_tx.py            adds/edits/deletes any transaction of a normal portfolio (the page's backend)
  trade.py                trades a game portfolio from the command line — live only, JSON out
  manage-portfolios.py    creates game portfolios, moves their cash, lists them, purges an obsolete
                            portfolio — same style; admin only, never given to an agent
  manage-users.py         the page's users: add, passwd, grant/revoke portfolios, delete, list-users,
                            list-portfolios — same style, admin only
  agents.py               the instructions agents elsewhere read (/agents.md, /agents/csv,
                            /agents/game-rules, /agents/game-agent) — built from import_csv.py's
                            docstring and trading-rules.md
  notes.py                an agent's notes on a portfolio — brief, memory, journal, and its strategy
                            in versions; the CLI of what server.py's /notes serves
  add_instrument.py       registers a new instrument (registry rows + price fetch) — the page's
                            "+ New instrument…"; also looks up Yahoo symbols for an ISIN
data/portfolio.db       ALL the data — gitignored; backed up by hand (`scripts/db.py backup`)
backup/                 the files the data came from before the database (config.json, registry/,
                          gen_prices/, gen_fx/, private-profiles/) — read by nothing; only
                          config.json and registry/ are committed
```

The database's tables (`scripts/schema.sql`) fall into three groups that differ by
**lifecycle**, and that is the distinction to preserve:

- **a portfolio's own** (IMPORTED) — `portfolio` (id, label, type, ai_access, public), its keys in `setting`,
  `ledger`, `position`/`activity`/`cash`, and an agent's notes on it (`agent_note`, `agent_journal`,
  `strategy_version`). The `ledger` is its real source — every TR
  row imported (origin `tr`, kept even when deleted on the page, flagged `deleted`) and every
  hand-entered or traded row (origin `manual`), and every row pasted as Portfolioviz CSV (origin
  `csv`, booked in the depot it names, `verified` 0 until accepted on the page, and until then
  deleted all at once by "Delete new transactions") — and `position`/`activity`/`cash` are rebuilt
  from it on every change, each row carrying that `verified` flag.
- **the registry** (CURATED) — `instrument`, `price_source`, `sector_color`, plus the global
  `setting`s. Hand- or agent-maintained, never clobbered by an import, not regenerable — losing it
  would cost real work, so back the database up.
- **the prices** (DERIVED) — `price`, `fx_rate`, `live_price`, and the view `latest_close` (newest
  close per instrument). Reproducible from `price_source` alone. `live_price` is a fresher price
  than any close (US pre-market from Yahoo, EU gettex mid in-session): the page makes it each
  series' last point, but it is never written into `price`.

A position, activity or cash booking belongs to a **depot** (`depot` column): one securities
account (Trade Republic, Comdirect, …), or "Manual" for hand-entered rows not booked elsewhere. A position is (depot, ISIN); the same ISIN can sit in two depots.

`scripts/schema.sql` is always the latest schema, and `db.py`'s `SCHEMA_VERSION` its number. A
database made from an older one is brought up to date the first time any script opens it: one
step per version in `db.py`'s `MIGRATIONS`, then `schema.sql` for any table still missing. A
schema change bumps both and adds its step.

Changes go through the scripts, never raw SQL: `db.py`'s own CLI for settings and registry rows
(`config`, `upsert`, `delete`), `add_instrument.py` to register an instrument, `import_tr.py` /
`import_csv.py` / `manual_tx.py` for portfolios, `trade.py` / `manage-portfolios.py` for games. `db.py query` is read-only. The page reads only
through the `api/` routes, which answer in the old files' shapes (same CSV header, same JSON), so
its parsing never changed. Portfolio tables keep numbers as the text that was written; prices are
real numbers.

`instrument` gives every instrument an `id`, equal to its ISIN, and is the single answer to "what
exists and what is it called". `price_source` is the single answer to "where do this instrument's
prices come from" — keyed by that same `id`, **one row per instrument**, no fallback or priority.
A thin listing that needs a second source is registered as a second instrument instead (its own
`instrument` row, its own `id`, its own slug — e.g. a `hynix-frankfurt` alongside `hynix`), so
`price_source` never has to choose between two rows for one thing. Prices are converted to the
portfolio currency **on write**, with the original kept in `close_raw`; nothing in the browser does
FX. An instrument may be listed with no holding — that is how a benchmark or a watchlist name gets
charted. A portfolio's ledger has every booking, so its rebuild writes `cash` (each booking's cash
effect, by depot), and the page shows the balance on a
date in a Cash tile of its own and counts it into **nothing else**: not the Current value /
"Value on" tile, not Invested, no gain, return or IRR, no benchmark, no map area. Moving
money to the broker is not an investment. Cash may go negative (a demo buy is never refused for
want of a deposit). Reconstructed TR cash matches Parqet's balance for the same depot to the
cent, apart from TR's tax refunds, which Parqet leaves out.

**Portfolios.** The page can show several portfolios — each a whole the page draws, made of one or
more depots, and seen only by the users it is granted to (**Users** below). Only a portfolio's own
tables are per portfolio; the registry, the prices and the browser's localStorage are shared, since
an instrument is the same thing whoever holds it. So the registry is the union of every portfolio's
instruments, and a portfolio sees only its slice: what its positions and activities hold (open or
closed), its `watchlist` setting, and its benchmark. Being in the registry without being held is
**not** enough to show up in a portfolio's watchlist — that would leak every other portfolio's
holdings into it. A portfolio is known by its **id** — `pid1`, `pid2`, … a running number
(`meta`'s `portfolio_seq`), handed out on creation and never reused — which every URL
(`?portfolio=`), script argument, token and `baselines/check_baseline.<id>.json` uses; its `label` is what the
page shows, and the only thing a rename changes. An id from before (a chosen name) stays until
`manage-portfolios.py renumber`, which renames every such portfolio's rows, grants, tokens and the
default — anything outside the database that names one (an agent's `TRADE_PORTFOLIO`, a baseline
file) is the admin's to follow up. The page picks the portfolio from `?portfolio=`, else the
user's own default (Config tab, "Make default": `user.default_portfolio`), else the global
`defaultPortfolio` setting; a portfolio's own settings override the global ones (flat, shallow —
`label`, `watchlist`, `benchmarkIsin`, `benchmarkLabel`, `timelineStart`), except `currency`, which
stays shared because the price series are converted into it on write. The Update button runs
`scripts/update_prices.py --portfolio <p>`, fetching only that portfolio's instruments.

A portfolio's `type` is one of two. `normal` — a regular portfolio, editable: fed by Trade
Republic exports (`import_tr.py`), Portfolioviz CSV pastes (`import_csv.py`) and buys, sells and
cash entered by hand (`manual_tx.py`, in any depot, "Manual" by default), all on the Transactions
tab, and every transaction — whatever its origin — editable or deletable there afterwards. `game` —
you cannot edit the past, only buy and sell now: trades at a live price under `trading-rules.md`
(`trade.py`, from the page's "Trade now" or an agent), cash moved only by `manage-portfolios.py`
(see **Game portfolios** below). The page creates both; a game made there starts with €10,000.
That tab's "+ New instrument…" writes to the registry (via `add_instrument.py`), the shared
catalog. server.py, `import_tr.py`, `import_csv.py`, `manual_tx.py`, `trade.py` and the page all
check the type, so a game's rules cannot be bent from the page. The TR conversion follows Parqet's
conventions (the page reads Parqet's schema) and was checked against Parqet's own import of the
same depot; its docstring lists them. Parqet itself is no longer a source: a portfolio a Parqet
refresh once filled becomes `normal` on migration, and `import_tr.rebuild` refuses to throw its
data away — purge it and import its transactions anew.

**AI access.** A portfolio's `ai_access` — the Config tab's "Allow AI (API) access, read-only" and
"…, read-write", one level: 0 none (a new portfolio's), 1 read, 2 read and write — says what a script
or an AI may do with it. Over HTTP that means an API token: a user makes, copies and revokes theirs
on the Config tab, shown there while access is allowed
(`api_token`: 10 letters and digits, looked up by hash but kept as is too, so it can be copied
again — convenience over hardening), and sends it as `Authorization: Bearer …` in place of the
session cookie. A token is bound to the portfolio whose Config tab made it: every route acts on
that one (`?portfolio=` may be left out; another is refused). It is refused outright while that
portfolio's `ai_access` is 0; it reads at 1, and at 2 writes too, only through server.py's `TOKEN_WRITES`
(transactions, imports, trades, instruments, Update) — never accepting its own CSV import, which
is its user's check of it, and never anything about portfolios, users, tokens or the server.
Whatever a token writes to a normal portfolio lands **unverified** (`manual_tx.py`/`import_tr.py`
`--api`, and every CSV import), for its user to accept or delete on the page; a token may edit or
delete only rows still unverified. A game's trades are live and final, so they are not. An agent
working from elsewhere learns all this from `/agents.md` (public, `scripts/agents.py`; the format and
the rules at `/agents/csv` and `/agents/game-rules`, fetched when needed), which every refusal a
token gets points to; the Config tab's "Copy agent prompt" hands it the URL and the token. Both give
the global `publicUrl` setting as the server's address — where others reach it — else the address the
request came in on, which from this machine is only localhost (the Config tab says so). On
the command line, an agent's `trade.py` session (`TRADE_PORTFOLIO` set) trades only a game with
`ai_access` read-write (`manage-portfolios.py create --ai`, or the boxes).

**Agents anywhere.** An agent needs no access to this repo: a token, the server's `publicUrl` and
`/agents.md` are enough; a game's agent also reads `/agents/game-agent` (its role: the session
routine and the default goals, which the owner's per-portfolio *special instructions* override —
the notes' `brief`, edited on the Config tab under the AI checkbox, binding on the agent above
everything but the rules). What it keeps
between sessions lives on the server, per portfolio (`notes.py`, `/notes`): its memory (replaced
each session), its journal (appended), its strategy (every save a new version with its reason —
the Transactions tab shows any version and what changed since another).

**Users.** server.py wants a login for every route but the page's own files (no data in them) and
login.html's: an HttpOnly session cookie, its token's hash in the `session` table. A user sees and
changes only the portfolios granted to them (`user_portfolio`, `can_write`); any other is a 404 on
every route, the same as one that does not exist, and api/config's `defaultPortfolio` is rewritten
to one they have. Anyone who reaches the page can create a user; it sees nothing until it
creates a portfolio (granted to its creator) or is granted one — `scripts/manage-users.py grant`,
which is also how every portfolio made by a script gets an owner. Update fetches one granted
portfolio's instruments, never the whole registry; registering an instrument takes write access to
some portfolio. A portfolio can be made **public** (Config tab, `portfolio.public`): every other
logged-in user may then open it — all of it, transactions included, read only (server.py's
`may_open`, never `may_change`; no Update either). The Watch tab lists other users' public
portfolios under their owners (who may change them); opening one shows its map alone, the header
picker naming it "owner / label" in red. An API token never reaches another's portfolio. An admin —
a user named in the global `admins` setting (a list; never sent to
the page) — gets a red "Restart Server" button under Update whenever server.py or a module it
imports changed on disk since it started; the server then replaces itself in place. The Config tab
lists the user's other portfolios and deletes one they may change — `manage-portfolios.py purge`,
with no backup. These tables (`user`, `user_portfolio`, `session`, `api_token`) are a
fourth lifecycle — people, neither a portfolio's own nor the registry — and an older database gets
them like any other schema change (below). It is for convenience and keeping people apart, not
hardened: the registry, prices and every script stay shared and unchecked. An agent's boundary is
its token — one portfolio, while that allows AI access, writing only if the token may — or, given
nothing but `trade.py` on this machine, the `game` type and `TRADE_PORTFOLIO` below.

**Game portfolios.** `scripts/manage-portfolios.py` creates game portfolios, moves their cash and
lists them; `scripts/trade.py` trades them — and so does the page's "Trade now", through server.py's
`/quote` and `/trade`, which run it. Each script's `--help` is its whole interface, and every
answer is one JSON object. Both touch only portfolios of type `game` (which `manage-portfolios.py
create` makes, the page through it); that type is the boundary — and splitting the two lets an
agent be handed trading alone: `manage-portfolios.py` is admin only. Its
`purge` removes any obsolete portfolio (not the default one), after backing the database up to
`--backup-dir` — or, said explicitly, `--no-backup`. An agent's session is also bound to its own
portfolio: whoever starts a command-line agent sets `TRADE_PORTFOLIO`, and `trade.py` refuses any
other — which holds only while the agent cannot change its environment, i.e. may run nothing but
`trade.py`. An agent elsewhere uses HTTP and a token instead (**Agents anywhere** above). It is
**live only**: no date can be given. A trade executes at once at a live price: gettex's ask (buy) or bid (sell) while gettex is
open (weekdays 08:00–22:00 German time) and its quote is under 15 minutes old — read from onvista's
unofficial API — else the home exchange via Yahoo while that is open and its price under 30 minutes
old, else not at all. `status`/`list-games` value a position at gettex's bid while its home market
is closed and gettex is open; otherwise at Yahoo's latest price. Charts, history and the page stay
on Yahoo's closes. A trade costs a €10 fee, on top of the spread — gettex's own, or 1% each way at
the home exchange, which has no bid/ask; a sale also pays 20% tax on its gain. Cash can never go
below zero: buys (fee included) and withdrawals are refused past it. The rules, with examples, are
`trading-rules.md` — keep it in step with `trade.py`'s FEE_FIXED/TAX_RATE, and its command-line
wording with `agents.py`'s HTTP_TERMS, which turns it into `/agents/game-rules` (a miss fails
loudly). `buy --eur` is the total
that leaves the portfolio's cash, fee included. Trades land in the ledger like hand-entered ones
(their `--reason` in its description), and the page shows them, but neither it nor `manual_tx.py`
can change them: a game's only way in is these two scripts.

The global settings (`python3 scripts/db.py config`, `... config set <key> '<json>'`) are what
an agent changes for the as-of picker's earliest date (`timelineStart`), the portfolio `currency`,
the benchmark (`benchmarkIsin`, `benchmarkLabel`) or the `defaultPortfolio`; `--portfolio <p>` does the
same for one portfolio's own. `ingest()` reads them (falling back to built-in defaults for a missing
key) and `update_prices.py` reads `timelineStart`/`currency` too, so the page and the fetcher move
together. Keep them flat.

**The database.** A new host runs `scripts/sqlite-install.sh` (sqlite3, then the database from
`schema.sql` + `seed.sql`), then `update_prices.py --from <timelineStart>`. `python3 scripts/db.py
seed > scripts/seed.sql` refreshes that seed from the current registry and settings. Backups are
by hand: `python3 scripts/db.py backup` writes a consistent, timestamped copy to the NAS
(`/Volumes/nas/bkp/portfolioviz` on macOS, `/mnt/data/nas/bkp/portfolioviz` on cube,
`/mnt/nas/bkp/portfolioviz` on other Linux hosts) — copying the
file itself while the server runs is not safe. `$PORTFOLIO_DB` points every script at another
database file, e.g. a copy to try something on.

**Deploying.** Run `./deploy.sh` on dev to copy the current working versions of Git-tracked
files to `jjj@cube:/mnt/data/web/portfolioviz/` using rsync over SSH. First it requires a clean
local Git working tree: staged changes, unstaged changes, and untracked files stop deployment
before any remote action (ignored files are allowed). Git metadata and untracked files are
excluded from the transfer. `./deploy.sh -n` also checks cleanliness before previewing the transfer.
Existing production files outside the transfer, including `data/portfolio.db`, remain in place.
Before copying, the script backs up the production database with the existing production code
and verifies the backup exists. After copying, it runs database migrations as `jjj`, restarts
`portfolioviz` with sudo, then checks service activity and HTTP responses on cube's port 8000.
Every step stops on failure. SSH allocates a terminal for sudo when restarting the service.
Dry runs only preview the transfer; they do not back up, migrate, or restart.
Before a deploy with a schema change, try it on a copy of prod's latest backup here
(`$PORTFOLIO_DB`, check_portfolio.js). Never copy dev's database to prod.

The two scripts are classic `<script>` tags sharing one global scope — no modules, no imports.
`portfolioviz.model.js` loads first and declares the state; `portfolioviz.view.js` reads it. The
dependency arrow points one way: **the model never calls into the view.** Keep it that way — it
is what lets the model be exercised without a browser.

## Running it

`fetch` is blocked on `file://`, so the page must be served:

```bash
./start.sh                         # serves on 8000 and opens portfolioviz.html
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
node check_portfolio.js --portfolio pid6 [--save]   # another portfolio, against baselines/check_baseline.pid6.json
```

A clean run means the numbers and the rendered text are untouched — worth having after any
refactor, since much of the arithmetic here (FIFO lots, XIRR, split detection, the as-of replay)
is easy to break in ways that still render fine. When a change is *meant* to move a number, read
the diff, agree with it, then `--save` over the baseline.

It cannot see layout, colour, or anything needing a real browser. Check those by eye.

## Data

`data/`, `backup/`'s old data files and `baselines/` hold real
position values and are gitignored — never commit them, and don't paste figures from them into
commit messages or issues. Nothing in the database is committed, the registry included:
`scripts/seed.sql` (no amounts — only what each instrument is and where its prices come from,
though it does enumerate which instruments are held) is the committed base, and the NAS backup
the rest. The `.gitignore` reads `*.csv`, so a new CSV anywhere is ignored by default.
