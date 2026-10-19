Custom visualization for portfolio data: your transactions (imported from Trade Republic, pasted as CSV from any broker, or entered by hand), priced from Yahoo and gettex.
Coded by (and for) Claude, I only made the design decisions.

To init:
1. clone repo; run `scripts/sqlite-install.sh` — installs sqlite3 if missing and creates the database (data/portfolio.db, not in git) with the base registry and settings
2. settings live in the database: `python3 scripts/db.py config` shows them, `python3 scripts/db.py config set timelineStart '"2019-01-01"'` changes one; adjust timeline start to your liking (default 1.1.2019; careful, 30 years back will need to pull a lot of price history that is probably irrelevant to today's stock market)
3. run start.sh for local webserver; create a user on the login page
4. "Create new portfolio" on the Config tab — Normal (a regular portfolio, every transaction editable) or Game (you cannot edit the past, only buy and sell now; starts with €10,000). Fill a normal one on the Transactions tab: import a Trade Republic transaction export (re-importing an overlapping export only adds what is new), paste transactions as CSV (any broker — a script or an AI can convert its statement), or enter them by hand (new instruments are registered from there too, by ISIN and Yahoo symbol)
5. click [Update]; this pulls all missing daily EOB ticks for 1.1.2019..today for the current portfolio's stocks from Yahoo (thank you Y).

Back up the database by hand: `python3 scripts/db.py backup` (to the NAS, toast/nas/bkp/portfolioviz). An obsolete portfolio goes with `scripts/manage-portfolios.py purge <portfolio> --yes --backup-dir <dir>` (backs up there first; `--no-backup` instead to skip that).

Game portfolios for you (Transactions tab: Trade now) or for AI agents: `scripts/manage-portfolios.py --help` to create and fund them from a terminal (admin only), `scripts/trade.py --help` to trade (all an agent gets — and only on a portfolio that allows AI access). Games are live-only — trades execute at live bid/ask prices, €10 fee each, 20% tax on gains, cash never below zero — so agents can be compared fairly. The rules: `trading-rules.md`.

Scripts and AI over HTTP: tick "Allow AI (API) access" on a portfolio's Config tab, make an API token there, and send it as `Authorization: Bearer <token>`. It reads that portfolio, and writes to it only if the token's own "allow writing" is ticked; what it writes waits for you to accept it on the Transactions tab (a game's trades excepted). "Copy agent prompt" next to a token gives an AI agent everything it needs to start — it then reads `/agents.md` on this server.

Notes:
- you can tell Claude directly to analyze the known data (like 'calc the max downdraw for all my stocks' or 'how many successful trades did i do in 2025?')
- design and calculations uses both standard methods and my personal tweaks (e.g. Volatility calc discounts upward moves by 50%, standard is either 0% or 100%)
- undocumented: in stock charts, you can click to set 0% line to a specific date, e.g. your buy date; click twice to go back to std; drag to select custom timeline
- work in progress and not battle tested; may contain calc errors. Contains inaccuracies (e.g. dividends are not reliably considered for gain calc; negligible with my tech stocks)

Open Questions? Ask Claude, it's really good with both coding and finance math :)
