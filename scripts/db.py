#!/usr/bin/env python3
"""The one way into data/portfolio.db — every other script imports this, none opens the DB itself.

    python3 scripts/db.py config [--portfolio P]                    # the settings, as JSON
    python3 scripts/db.py config [--portfolio P] set <key> <json>   # e.g. set timelineStart '"2019-01-01"'
    python3 scripts/db.py config [--portfolio P] unset <key>
    python3 scripts/db.py upsert <table> <col>=<value> ...        # a registry row: instrument,
                                                                  #   price_source or sector_color
    python3 scripts/db.py delete <table> <key>                    # one registry row, by its key
    python3 scripts/db.py query "<SELECT ...>"                    # read-only, JSON rows out
    python3 scripts/db.py backup [<dir>]                          # a consistent copy, timestamped
    python3 scripts/db.py seed                                    # registry + global settings as SQL

The schema is scripts/schema.sql; scripts/sqlite-install.sh creates the DB from it and from
scripts/seed.sql, which `seed` regenerates (`db.py seed > scripts/seed.sql`) — the base a new host
starts from. The DB itself is not in git: `backup` is how it is kept, by hand, on the NAS
(/Volumes/nas/bkp/portfolioviz on macOS, /mnt/data/nas/bkp/portfolioviz on cube, /mnt/nas/bkp/portfolioviz
on other Linux hosts, or $PORTFOLIO_BACKUP_DIR).
It copies through SQLite's backup API, so a copy taken while the server or a fetch is writing is
still consistent — which copying the file is not. Users and their access are scripts/manage-users.py's.

$PORTFOLIO_DB points everything at another database file (a test copy, say).
"""
import contextlib, hashlib, hmac, json, os, secrets, sqlite3, sys, threading
from datetime import datetime, timedelta

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)                  # the repo — this file lives in scripts/
DB_PATH = os.environ.get("PORTFOLIO_DB") or os.path.join(ROOT, "data", "portfolio.db")
# where `backup` writes: the first whose share is mounted — macOS, cube (prod), other Linux hosts
BACKUP_DIRS = ("/Volumes/nas/bkp/portfolioviz", "/mnt/data/nas/bkp/portfolioviz", "/mnt/nas/bkp/portfolioviz")

# the built-in settings, for a key the DB does not have — the same defaults the page falls back to
DEFAULTS = {"timelineStart": "2019-01-01", "currency": "EUR"}

INSTRUMENT_FIELDS = ["id", "isin", "slug", "name", "display", "type", "currency", "sector", "note"]
SOURCE_FIELDS = ["id", "source", "symbol", "quote_currency", "fx_symbol", "note"]
SECTOR_COLOR_FIELDS = ["sector", "hue", "sat"]
SERIES_FIELDS = ["date", "close", "close_raw", "quote_currency", "source"]
LIVE_FIELDS = ["id", "date", "at", "price", "price_raw", "quote_currency", "source"]
LATEST_FIELDS = ["id", "date", "close", "source"]
# the registry tables `upsert`/`delete` may touch, with their key column
REGISTRY = {"instrument": ("id", INSTRUMENT_FIELDS), "price_source": ("id", SOURCE_FIELDS),
            "sector_color": ("sector", SECTOR_COLOR_FIELDS)}


# ---------- connection ----------

_local = threading.local()


def conn():
    """This thread's connection — sqlite3 connections are not shared across threads, and
    update_prices.py writes from a pool. Autocommit; tx() groups writes."""
    c = getattr(_local, "conn", None)
    if c is None:
        if not os.path.exists(DB_PATH):
            sys.exit(f"no database at {DB_PATH} — run scripts/sqlite-install.sh first")
        c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 60000")
        _local.conn, _local.depth = c, 0
        migrate(c)
    return c


# ---------- schema versions ----------
# schema.sql is always the latest; a database made from an older one is brought up to it the first
# time a script opens it. Each step takes one version to the next, and holds back on what is not
# there (a table a much older database never had is simply created, by schema.sql, afterwards).

SCHEMA_VERSION = 14


def _columns(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _rename_column(c, table, old, new):
    if old in _columns(c, table):
        c.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def _rename_table(c, old, new):
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (old,)).fetchone():
        c.execute(f"ALTER TABLE {old} RENAME TO {new}")


def _to_2(c):
    """A position's, activity's or cash booking's `portfolio` (Parqet's word) is its `depot`."""
    for table in ("position", "activity", "cash"):
        _rename_column(c, table, "portfolio", "depot")


def _to_3(c):
    """A profile is a portfolio: its table, every column naming one, and their indexes."""
    _rename_table(c, "profile", "portfolio")
    _rename_table(c, "user_profile", "user_portfolio")
    for table in ("setting", "ledger", "position", "activity", "cash", "user_portfolio"):
        if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            _rename_column(c, table, "profile", "portfolio")
    for table in ("position", "activity", "cash"):
        c.execute(f"DROP INDEX IF EXISTS {table}_profile")      # schema.sql makes {table}_portfolio
    c.execute("UPDATE setting SET key = 'defaultPortfolio' WHERE key = 'defaultProfile'")


def _to_4(c):
    """A portfolio's source is its type, and one open to the command line (allow_cli) is a game.
    A new CHECK means a new table; migrate() has foreign keys off, so dropping the old one
    cascades to nothing."""
    if "source" not in _columns(c, "portfolio"):
        return
    c.execute("CREATE TABLE portfolio_new (name TEXT PRIMARY KEY, label TEXT NOT NULL, "
              "type TEXT NOT NULL CHECK (type IN ('parqet', 'manual', 'game')))")
    c.execute("INSERT INTO portfolio_new (name, label, type) SELECT name, label, "
              "CASE WHEN allow_cli THEN 'game' ELSE source END FROM portfolio ORDER BY rowid")
    c.execute("DROP TABLE portfolio")
    c.execute("ALTER TABLE portfolio_new RENAME TO portfolio")


def _to_5(c):
    """Pasted CSV rows: the ledger takes origin 'csv' and a verified flag (a new CHECK, so a new
    table), and what is rebuilt from it — activities and cash — carries that flag to the page."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ledger'").fetchone() \
            and "verified" not in _columns(c, "ledger"):
        c.execute("CREATE TABLE ledger_new (portfolio TEXT NOT NULL REFERENCES portfolio (name) ON DELETE CASCADE, "
                  "transaction_id TEXT NOT NULL, "
                  "origin TEXT NOT NULL CHECK (origin IN ('tr', 'manual', 'csv')), datetime TEXT NOT NULL, "
                  "deleted INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 1, "
                  "data TEXT NOT NULL, PRIMARY KEY (portfolio, transaction_id))")
        c.execute("INSERT INTO ledger_new (portfolio, transaction_id, origin, datetime, deleted, data) "
                  "SELECT portfolio, transaction_id, origin, datetime, deleted, data FROM ledger ORDER BY rowid")
        c.execute("DROP TABLE ledger")
        c.execute("ALTER TABLE ledger_new RENAME TO ledger")
    for table in ("activity", "cash"):
        if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() \
                and "verified" not in _columns(c, table):
            c.execute(f"ALTER TABLE {table} ADD COLUMN verified TEXT")


def _to_6(c):
    """Two types, normal and game: manual is normal, and so is a Parqet one (its import path is
    gone — import_tr.rebuild will not overwrite what it imported). A portfolio says whether AI may
    write to it; a game already in use is an agent's, so it may. A new CHECK means a new table, as
    in _to_4."""
    if "ai_write" in _columns(c, "portfolio"):
        return
    c.execute("CREATE TABLE portfolio_new (name TEXT PRIMARY KEY, label TEXT NOT NULL, "
              "type TEXT NOT NULL CHECK (type IN ('normal', 'game')), ai_write INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO portfolio_new (name, label, type, ai_write) SELECT name, label, "
              "CASE WHEN type = 'game' THEN 'game' ELSE 'normal' END, type = 'game' FROM portfolio ORDER BY rowid")
    c.execute("DROP TABLE portfolio")
    c.execute("ALTER TABLE portfolio_new RENAME TO portfolio")


def _to_7(c):
    """An API token is kept as it is too, so its user can copy it again. One made before has only
    its hash: it still works, but cannot be shown — its user makes a new one."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_token'").fetchone() \
            and "token" not in _columns(c, "api_token"):
        c.execute("ALTER TABLE api_token ADD COLUMN token TEXT")


def _to_8(c):
    """An API token is bound to one portfolio. One made before has none, and is refused until its
    user makes a new one."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_token'").fetchone() \
            and "portfolio" not in _columns(c, "api_token"):
        c.execute("ALTER TABLE api_token ADD COLUMN portfolio TEXT REFERENCES portfolio (name) ON DELETE CASCADE")


def _to_9(c):
    """A portfolio is known by its id, not a name: the column is renamed, and every foreign key
    naming it follows (SQLite rewrites them). Existing ids stay as they are — manage-portfolios.py
    renumber gives them pid{n} ones, when their admin says so."""
    _rename_column(c, "portfolio", "name", "id")


def _to_10(c):
    """A portfolio's AI flag is about any access (ai_access), a token's own can_write about writing.
    A token made before could write wherever its portfolio let it, so it still may."""
    _rename_column(c, "portfolio", "ai_write", "ai_access")
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_token'").fetchone() \
            and "can_write" not in _columns(c, "api_token"):
        c.execute("ALTER TABLE api_token ADD COLUMN can_write INTEGER NOT NULL DEFAULT 0")
        c.execute("UPDATE api_token SET can_write = 1")


def _to_11(c):
    """A portfolio may be public: viewable, read only, by every other logged-in user."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'portfolio'").fetchone() \
            and "public" not in _columns(c, "portfolio"):
        c.execute("ALTER TABLE portfolio ADD COLUMN public INTEGER NOT NULL DEFAULT 0")


def _to_12(c):
    """An agent's notes: new tables, which schema.sql creates."""


def _to_13(c):
    """AI access is a level per portfolio — 0 none, 1 read-only, 2 read-write — in place of a flag
    plus each token's own "allow writing". Where access was allowed, its tokens could all write, so
    it becomes read-write."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'portfolio'").fetchone() \
            and "ai_access" in _columns(c, "portfolio"):
        c.execute("UPDATE portfolio SET ai_access = 2 WHERE ai_access = 1")


def _to_14(c):
    """Each user may have a default portfolio of their own, over the global defaultPortfolio."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user'").fetchone() \
            and "default_portfolio" not in _columns(c, "user"):
        c.execute("ALTER TABLE user ADD COLUMN default_portfolio TEXT REFERENCES portfolio (id) ON DELETE SET NULL")


MIGRATIONS = {2: _to_2, 3: _to_3, 4: _to_4, 5: _to_5, 6: _to_6, 7: _to_7, 8: _to_8, 9: _to_9, 10: _to_10,
              11: _to_11, 12: _to_12, 13: _to_13, 14: _to_14}


def migrate(c):
    row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row and int(row[0]) >= SCHEMA_VERSION:
        return
    c.execute("PRAGMA foreign_keys = OFF")       # a rebuilt table must not cascade (see _to_4)
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        for v in range(int(row[0]) + 1 if row else 2, SCHEMA_VERSION + 1):
            MIGRATIONS[v](c)
        broken = c.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"migration left {len(broken)} rows pointing nowhere, e.g. {tuple(broken[0])}")
        c.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                  "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(SCRIPTS, "schema.sql"), encoding="utf-8") as fh:
        c.executescript(fh.read())            # every statement there is IF NOT EXISTS


def close():
    """Close this thread's connection, if it has one. A thread that ends does not: its connection
    lingers until a garbage collection happens to run, and a server answering each request in a
    thread of its own runs out of file handles first."""
    c = getattr(_local, "conn", None)
    if c is not None:
        _local.conn = None
        c.close()


@contextlib.contextmanager
def tx():
    """One write transaction, taken up front (BEGIN IMMEDIATE) so a read-then-write inside cannot
    be overtaken by another writer. Nested calls join the outer one."""
    c = conn()
    if _local.depth:
        _local.depth += 1
        try:
            yield c
        finally:
            _local.depth -= 1
        return
    c.execute("BEGIN IMMEDIATE")
    _local.depth = 1
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        _local.depth = 0


def rows(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args)]


def text(v):
    """A stored value as the CSV text it replaced: NULL is empty, a float its shortest exact form."""
    if v is None:
        return ""
    return repr(v) if isinstance(v, float) else str(v)


# ---------- settings ----------

def settings(portfolio=""):
    """The keys stored for one scope — '' for the global ones (was config.json), else a portfolio's own
    (was its profile.json, less label/source/allow-cli, now its `portfolio` row's label and type)."""
    return {r["key"]: json.loads(r["value"])
            for r in conn().execute("SELECT key, value FROM setting WHERE portfolio = ? ORDER BY rowid",
                                    (portfolio,))}


def config():
    """The global settings over the built-in defaults."""
    return {**DEFAULTS, **settings("")}


def set_setting(key, value, portfolio=""):
    with tx() as c:
        c.execute("INSERT INTO setting (portfolio, key, value) VALUES (?, ?, ?) "
                  "ON CONFLICT (portfolio, key) DO UPDATE SET value = excluded.value",
                  (portfolio, key, json.dumps(value, ensure_ascii=False)))


def unset_setting(key, portfolio=""):
    with tx() as c:
        c.execute("DELETE FROM setting WHERE portfolio = ? AND key = ?", (portfolio, key))


def timeline_start():
    return config().get("timelineStart") or DEFAULTS["timelineStart"]


def portfolio_currency():
    return config().get("currency") or DEFAULTS["currency"]


# ---------- registry ----------

def instruments():
    """Every instrument, in the order it was registered."""
    return rows("SELECT * FROM instrument ORDER BY rowid")


def sources():
    """Each instrument's price source, by instrument id."""
    return {r["id"]: r for r in rows("SELECT * FROM price_source ORDER BY rowid")}


def sector_colors():
    return rows("SELECT * FROM sector_color ORDER BY rowid")


def upsert(table, row):
    key, fields = REGISTRY[table]
    unknown = set(row) - set(fields)
    if unknown:
        sys.exit(f"{table} has no column {', '.join(sorted(unknown))} — it has {', '.join(fields)}")
    cols = [f for f in fields if f in row]
    sets = ", ".join(f"{f} = excluded.{f}" for f in cols if f != key) or f"{key} = excluded.{key}"
    with tx() as c:
        c.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                  f"ON CONFLICT ({key}) DO UPDATE SET {sets}", [row[f] for f in cols])


def delete(table, value):
    key, _ = REGISTRY[table]
    with tx() as c:
        return c.execute(f"DELETE FROM {table} WHERE {key} = ?", (value,)).rowcount


# ---------- prices ----------

def series(iid):
    """An instrument's closes, oldest first."""
    return rows("SELECT date, close, close_raw, quote_currency, source FROM price "
                "WHERE id = ? ORDER BY date", (iid,))


def series_dates(iid):
    return {r[0] for r in conn().execute("SELECT date FROM price WHERE id = ?", (iid,))}


def write_series(iid, new):
    """Rows by date, each {date, close, close_raw, quote_currency, source} — added, or replacing
    the stored row for the same date."""
    with tx() as c:
        c.executemany("INSERT INTO price (id, date, close, close_raw, quote_currency, source) "
                      "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id, date) DO UPDATE SET "
                      "close = excluded.close, close_raw = excluded.close_raw, "
                      "quote_currency = excluded.quote_currency, source = excluded.source",
                      [(iid, r["date"], r["close"], r.get("close_raw"), r.get("quote_currency") or "",
                        r.get("source") or "") for r in new])


def latest():
    """The freshest close per instrument, by id."""
    return {r["id"]: r for r in rows("SELECT * FROM latest_close ORDER BY id")}


def live():
    return rows("SELECT * FROM live_price ORDER BY id")


def set_live(found, done):
    """Every instrument in `done` either gets its row from `found` or loses its old one — a
    pre-market price must not outlive the session that superseded it. Others keep theirs."""
    with tx() as c:
        c.executemany("DELETE FROM live_price WHERE id = ?", [(i,) for i in done])
        c.executemany(f"INSERT INTO live_price ({', '.join(LIVE_FIELDS)}) VALUES "
                      f"({', '.join('?' * len(LIVE_FIELDS))})",
                      [[r[f] for f in LIVE_FIELDS] for r in found])


def fx(pair):
    """Daily rates of a Yahoo FX symbol (EURUSD=X), by date."""
    return {r[0]: r[1] for r in conn().execute("SELECT date, rate FROM fx_rate WHERE pair = ?", (pair,))}


def write_fx(pair, rates):
    with tx() as c:
        c.executemany("INSERT INTO fx_rate (pair, date, rate) VALUES (?, ?, ?) "
                      "ON CONFLICT (pair, date) DO UPDATE SET rate = excluded.rate",
                      [(pair, d, r) for d, r in rates.items()])


# ---------- portfolios ----------
# A portfolio is one row in `portfolio` (id, label, type, ai_access, public) plus its own settings (watchlist,
# benchmarkIsin, ...) — together what its profile.json used to say.

POSITION_FIELDS = ["depot", "name", "identifier", "assetType", "isSold", "shares", "currency",
                   "currentValue", "purchaseValue", "lastPriceDate", "lastPrice", "realizedGainNet",
                   "unrealizedGainNet", "earliestActivityDate", "activityCount"]
ACTIVITY_FIELDS = ["depot", "name", "identifier", "type", "datetime", "shares", "price", "amount",
                   "amountNet", "fee", "tax", "realizedGains", "realizedGainsNet", "currency",
                   "transactionId", "verified"]
# verified: "0" for a pasted row not yet accepted (import_csv.py), "1" for any other ledger row
CASH_FIELDS = ["depot", "datetime", "date", "kind", "amount", "transactionId", "verified"]
PORTFOLIO_TABLES = {"position": POSITION_FIELDS, "activity": ACTIVITY_FIELDS, "cash": CASH_FIELDS}


def portfolio(pid):
    """The portfolio's row as {id, label, type, ai_access, public}, or None."""
    r = conn().execute("SELECT * FROM portfolio WHERE id = ?", (pid,)).fetchone()
    return dict(r) if r else None


def portfolios():
    return rows("SELECT * FROM portfolio ORDER BY rowid")


def portfolio_config(pid):
    """Its label and type with its own settings — what its profile.json used to hold."""
    p = portfolio(pid)
    if not p:
        return None
    return {"label": p["label"], "type": p["type"], **settings(pid)}


TYPES = ("normal", "game")


def next_portfolio_id(c):
    """pid1, pid2, … — a running number (meta's portfolio_seq), never handed out twice, not even after
    a purge, so an old link or log line can never point at another portfolio."""
    row = c.execute("SELECT value FROM meta WHERE key = 'portfolio_seq'").fetchone()
    taken = [int(r[0][3:]) for r in c.execute("SELECT id FROM portfolio WHERE id GLOB 'pid[0-9]*'")]
    n = max([int(row[0]) if row else 0] + taken) + 1
    c.execute("INSERT INTO meta (key, value) VALUES ('portfolio_seq', ?) "
              "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(n),))
    return f"pid{n}"


def create_portfolio(label, type_, ai_access=0):
    """A new, empty portfolio with an empty watchlist, under the next portfolio id; that id."""
    with tx() as c:
        pid = next_portfolio_id(c)
        c.execute("INSERT INTO portfolio (id, label, type, ai_access) VALUES (?, ?, ?, ?)",
                  (pid, label or pid, type_, int(ai_access)))
        c.execute("INSERT INTO setting (portfolio, key, value) VALUES (?, 'watchlist', '[]')", (pid,))
    return pid


# every table with a `portfolio` column naming one by its id
PORTFOLIO_COLUMNS = ("setting", "ledger", "position", "activity", "cash", "user_portfolio", "api_token",
                     "agent_note", "agent_journal", "strategy_version")


def renumber_portfolios():
    """Every portfolio whose id is not a pid{n} yet gets the next one, oldest first, and everything
    naming it follows — its rows in PORTFOLIO_COLUMNS and the global defaultPortfolio. [(old, new)]."""
    out = []
    with tx() as c:
        c.execute("PRAGMA defer_foreign_keys = ON")      # parent and children change one by one
        default = settings("").get("defaultPortfolio")
        for (old,) in c.execute("SELECT id FROM portfolio WHERE id NOT GLOB 'pid[0-9]*' ORDER BY rowid").fetchall():
            new = next_portfolio_id(c)
            c.execute("UPDATE portfolio SET id = ? WHERE id = ?", (new, old))
            for table in PORTFOLIO_COLUMNS:
                c.execute(f"UPDATE {table} SET portfolio = ? WHERE portfolio = ?", (new, old))
            c.execute("UPDATE user SET default_portfolio = ? WHERE default_portfolio = ?", (new, old))
            if default == old:
                c.execute("UPDATE setting SET value = ? WHERE portfolio = '' AND key = 'defaultPortfolio'",
                          (json.dumps(new),))
            out.append((old, new))
    return out


def set_public(pid, public):
    with tx() as c:
        return c.execute("UPDATE portfolio SET public = ? WHERE id = ?", (int(public), pid)).rowcount


def public_portfolios(name):
    """The public portfolios the user is not granted, oldest first, each with `owners` — the users
    who may change it, which is who it is shown under."""
    out = rows("SELECT * FROM portfolio WHERE public AND id NOT IN "
               "(SELECT portfolio FROM user_portfolio WHERE user = ?) ORDER BY rowid", (name,))
    for p in out:
        p["owners"] = [r["user"] for r in rows("SELECT user FROM user_portfolio WHERE portfolio = ? AND can_write "
                                               "ORDER BY user", (p["id"],))]
    return out


AI_NONE, AI_READ, AI_WRITE = 0, 1, 2


def set_ai_access(pid, level):
    """0 no API access, 1 read-only, 2 read-write."""
    with tx() as c:
        return c.execute("UPDATE portfolio SET ai_access = ? WHERE id = ?", (int(level), pid)).rowcount


def set_label(pid, label):
    with tx() as c:
        return c.execute("UPDATE portfolio SET label = ? WHERE id = ?", (label, pid)).rowcount


def purge_portfolio(pid):
    """Every row the portfolio has — ledgers, positions, activities, cash, settings. The registry and
    the prices stay: they are shared."""
    with tx() as c:
        c.execute("DELETE FROM setting WHERE portfolio = ?", (pid,))
        return c.execute("DELETE FROM portfolio WHERE id = ?", (pid,)).rowcount   # cascades


def portfolio_rows(pid, table):
    """A portfolio's positions, activities or cash, as text, in the order they were written."""
    fields = PORTFOLIO_TABLES[table]
    return rows(f"SELECT {', '.join(fields)} FROM {table} WHERE portfolio = ? ORDER BY rowid", (pid,))


def replace_portfolio_rows(pid, table, new):
    fields = PORTFOLIO_TABLES[table]
    with tx() as c:
        c.execute(f"DELETE FROM {table} WHERE portfolio = ?", (pid,))
        c.executemany(f"INSERT INTO {table} (portfolio, {', '.join(fields)}) VALUES "
                      f"(?, {', '.join('?' * len(fields))})",
                      [[pid] + [r.get(f) for f in fields] for r in new])


# ---------- ledgers ----------
# A manual portfolio's source of truth: the Trade Republic rows it imported (origin 'tr'), the
# rows entered by hand or traded (origin 'manual') and the rows pasted in the Portfolioviz CSV
# format (origin 'csv'), each the TR export's row as a dict.

def ledger(pid, origin, deleted=True):
    """One origin's rows, oldest first (same-time rows in the order they were written). An
    imported row deleted on the page is left out unless `deleted`."""
    return [json.loads(r[0]) for r in conn().execute(
        "SELECT data FROM ledger WHERE portfolio = ? AND origin = ?" + ("" if deleted else " AND NOT deleted")
        + " ORDER BY datetime, rowid", (pid, origin))]


def add_ledger_rows(pid, origin, new, verified=True):
    """`verified` for all of them, or a set of the transaction ids that are verified."""
    with tx() as c:
        c.executemany("INSERT INTO ledger (portfolio, transaction_id, origin, datetime, verified, data) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      [(pid, r["transaction_id"], origin, r["datetime"],
                        int(r["transaction_id"] in verified if isinstance(verified, set) else verified),
                        json.dumps(r, ensure_ascii=False)) for r in new])


def replace_ledger(pid, origin, new, unverified=()):
    """All of one origin's rows at once — how a hand-entered row is added, edited or removed. Each row
    keeps whether it is verified; a new one is, unless its id is in `unverified`."""
    with tx() as c:
        was = {r[0]: bool(r[1]) for r in c.execute(
            "SELECT transaction_id, verified FROM ledger WHERE portfolio = ? AND origin = ?", (pid, origin))}
        c.execute("DELETE FROM ledger WHERE portfolio = ? AND origin = ?", (pid, origin))
        add_ledger_rows(pid, origin, new, verified={r["transaction_id"] for r in new
                                                    if r["transaction_id"] not in unverified
                                                    and was.get(r["transaction_id"], True)})


def is_verified(pid, tid):
    """Whether one ledger row is verified; None if there is none."""
    r = conn().execute("SELECT verified FROM ledger WHERE portfolio = ? AND transaction_id = ?", (pid, tid)).fetchone()
    return None if r is None else bool(r[0])


def ledger_entry(pid, tid):
    """(origin, row) of one ledger row, whatever its origin, or None."""
    r = conn().execute("SELECT origin, data FROM ledger WHERE portfolio = ? AND transaction_id = ?",
                       (pid, tid)).fetchone()
    return (r[0], json.loads(r[1])) if r else None


def update_ledger_row(pid, tid, row):
    """One row rewritten in place — same id, same origin, its flags (deleted, verified) kept."""
    with tx() as c:
        c.execute("UPDATE ledger SET datetime = ?, data = ? WHERE portfolio = ? AND transaction_id = ?",
                  (row["datetime"], json.dumps(row, ensure_ascii=False), pid, tid))


def mark_deleted(pid, tid):
    """An imported row deleted on the page: kept, so a re-import still knows it, but left out."""
    with tx() as c:
        return c.execute("UPDATE ledger SET deleted = 1 WHERE portfolio = ? AND transaction_id = ? "
                         "AND origin IN ('tr', 'csv')", (pid, tid)).rowcount


def unverified(pid):
    """The transaction ids of the rows pasted but not yet accepted."""
    return {r[0] for r in conn().execute(
        "SELECT transaction_id FROM ledger WHERE portfolio = ? AND NOT verified", (pid,))}


def accept_unverified(pid):
    """Every pasted row not yet accepted, accepted: no longer new, no longer rolled back."""
    with tx() as c:
        return c.execute("UPDATE ledger SET verified = 1 WHERE portfolio = ? AND NOT verified", (pid,)).rowcount


def discard_unverified(pid):
    """Every pasted row not yet accepted, gone — the rollback of a bad paste."""
    with tx() as c:
        return c.execute("DELETE FROM ledger WHERE portfolio = ? AND NOT verified", (pid,)).rowcount


# ---------- an agent's notes ----------
# What an agent working on a portfolio keeps between sessions, wherever it runs: memory and the
# owner's brief (overwritten), the journal (appended), the strategy (versioned, each with its reason).

NOTE_MAX = 64 * 1024                  # characters, per note, strategy version or journal entry
STRATEGY_KEEP = 200                   # versions kept per portfolio, the newest
JOURNAL_KEEP = 2000                   # journal entries kept per portfolio, the newest


def _now():
    return datetime.now().isoformat(timespec="seconds")


def note(pid, name):
    r = conn().execute("SELECT text, updated FROM agent_note WHERE portfolio = ? AND name = ?", (pid, name)).fetchone()
    return {"text": r[0], "updated": r[1]} if r else None


def set_note(pid, name, text):
    with tx() as c:
        c.execute("INSERT INTO agent_note (portfolio, name, text, updated) VALUES (?, ?, ?, ?) "
                  "ON CONFLICT (portfolio, name) DO UPDATE SET text = excluded.text, updated = excluded.updated",
                  (pid, name, text, _now()))


def add_journal(pid, text, at=None):
    with tx() as c:
        c.execute("INSERT INTO agent_journal (portfolio, at, text) VALUES (?, ?, ?)", (pid, at or _now(), text))
        c.execute("DELETE FROM agent_journal WHERE portfolio = ? AND rowid NOT IN (SELECT rowid FROM agent_journal "
                  "WHERE portfolio = ? ORDER BY at DESC, rowid DESC LIMIT ?)", (pid, pid, JOURNAL_KEEP))


def journal(pid, limit=50):
    """The newest `limit` entries, oldest first."""
    return list(reversed(rows("SELECT at, text FROM agent_journal WHERE portfolio = ? ORDER BY at DESC, rowid DESC "
                              "LIMIT ?", (pid, limit))))


def add_strategy(pid, text, reason, saved=None):
    """A new version of the portfolio's strategy; its number."""
    with tx() as c:
        n = (c.execute("SELECT max(version) FROM strategy_version WHERE portfolio = ?", (pid,)).fetchone()[0] or 0) + 1
        c.execute("INSERT INTO strategy_version (portfolio, version, text, reason, saved) VALUES (?, ?, ?, ?, ?)",
                  (pid, n, text, reason, saved or _now()))
        c.execute("DELETE FROM strategy_version WHERE portfolio = ? AND version <= ?", (pid, n - STRATEGY_KEEP))
    return n


def strategy(pid, version=None):
    """One version, the newest unless named: {version, text, reason, saved}, or None."""
    r = conn().execute("SELECT version, text, reason, saved FROM strategy_version WHERE portfolio = ? "
                       + ("AND version = ?" if version else "ORDER BY version DESC LIMIT 1"),
                       (pid, version) if version else (pid,)).fetchone()
    return dict(r) if r else None


def strategy_history(pid):
    """Every version kept, newest first, without its text: {version, reason, saved}."""
    return rows("SELECT version, reason, saved FROM strategy_version WHERE portfolio = ? ORDER BY version DESC", (pid,))


# ---------- access ----------
# Users, which portfolios each may open (and change), and their logged-in sessions — server.py's
# login. The scripts themselves never ask: a command line is the admin's.

SESSION_DAYS = 30


def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_password(pw, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), h)


def user(name):
    r = conn().execute("SELECT * FROM user WHERE name = ?", (name,)).fetchone()
    return dict(r) if r else None


def users():
    """Every user with the portfolios they may open: {name, created, portfolios: {portfolio: can_write}}."""
    out = {u["name"]: {"name": u["name"], "created": u["created"], "portfolios": {}}
           for u in rows("SELECT name, created FROM user ORDER BY name")}
    for g in rows("SELECT * FROM user_portfolio ORDER BY portfolio"):
        out[g["user"]]["portfolios"][g["portfolio"]] = bool(g["can_write"])
    return list(out.values())


def set_user_default(name, pid):
    """The portfolio the user's page opens with, when its address names none."""
    with tx() as c:
        return c.execute("UPDATE user SET default_portfolio = ? WHERE name = ?", (pid, name)).rowcount


def create_user(name, pw):
    """sqlite3.IntegrityError if the name is taken."""
    with tx() as c:
        c.execute("INSERT INTO user (name, pw_hash, created) VALUES (?, ?, ?)",
                  (name, hash_password(pw), datetime.now().isoformat(timespec="seconds")))


def set_password(name, pw):
    """Also ends every session the user has."""
    with tx() as c:
        c.execute("DELETE FROM session WHERE user = ?", (name,))
        return c.execute("UPDATE user SET pw_hash = ? WHERE name = ?", (hash_password(pw), name)).rowcount


def delete_user(name):
    with tx() as c:
        return c.execute("DELETE FROM user WHERE name = ?", (name,)).rowcount   # cascades


def grant(name, portfolio, can_write=True):
    with tx() as c:
        c.execute("INSERT INTO user_portfolio (user, portfolio, can_write) VALUES (?, ?, ?) "
                  "ON CONFLICT (user, portfolio) DO UPDATE SET can_write = excluded.can_write",
                  (name, portfolio, 1 if can_write else 0))


def revoke(name, portfolio):
    with tx() as c:
        return c.execute("DELETE FROM user_portfolio WHERE user = ? AND portfolio = ?",
                         (name, portfolio)).rowcount


def access(name, portfolio):
    """None if the user may not open the portfolio, else whether they may change it."""
    r = conn().execute("SELECT can_write FROM user_portfolio WHERE user = ? AND portfolio = ?",
                       (name, portfolio)).fetchone()
    return None if r is None else bool(r[0])


def user_portfolios(name):
    """The portfolio rows the user may open, each with its `can_write`."""
    return rows("SELECT p.*, g.can_write FROM portfolio p JOIN user_portfolio g ON g.portfolio = p.id "
                "WHERE g.user = ? ORDER BY p.rowid", (name,))


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session(name):
    """A fresh session token for the user — the cookie's value; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    with tx() as c:
        c.execute("DELETE FROM session WHERE expires < ?", (now.isoformat(timespec="seconds"),))
        c.execute("INSERT INTO session (token_hash, user, expires) VALUES (?, ?, ?)",
                  (_token_hash(token), name, (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    return token


def session_user(token):
    """The user a session token belongs to, or None (unknown or expired)."""
    if not token:
        return None
    r = conn().execute("SELECT user FROM session WHERE token_hash = ? AND expires > ?",
                       (_token_hash(token), datetime.now().isoformat(timespec="seconds"))).fetchone()
    return r[0] if r else None


def end_session(token):
    with tx() as c:
        c.execute("DELETE FROM session WHERE token_hash = ?", (_token_hash(token),))


# A user's API tokens, each bound to one portfolio: looked up by hash like a session, but no expiry —
# one lasts until revoked, or its portfolio is purged —
# and kept as it is as well, for its user to copy from the page: short and easy to hand to an agent
# rather than hardened (10 letters and digits, ~58 bits). Its id, for listing and revoking, is the
# hash's first 12 characters.
TOKEN_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"   # no 0/O, 1/l/I


def new_api_token(name, label, portfolio):
    token = "".join(secrets.choice(TOKEN_CHARS) for _ in range(10))
    with tx() as c:
        c.execute("INSERT INTO api_token (token_hash, user, label, created, token, portfolio) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (_token_hash(token), name, label,
                                                datetime.now().isoformat(timespec="seconds"), token, portfolio))
    return token


def api_tokens(name, portfolio):
    """The user's tokens for the portfolio, each with the token itself (None for one made before
    tokens were kept) — and any of theirs bound to none, made before tokens were bound."""
    return [{"id": r["token_hash"][:12], "label": r["label"], "created": r["created"],
             "last_used": r["last_used"], "token": r["token"], "portfolio": r["portfolio"]}
            for r in rows("SELECT * FROM api_token WHERE user = ? AND (portfolio = ? OR portfolio IS NULL) "
                          "ORDER BY created", (name, portfolio))]


def revoke_api_token(name, token_id):
    if len(token_id or "") != 12:
        return 0
    with tx() as c:
        return c.execute("DELETE FROM api_token WHERE user = ? AND substr(token_hash, 1, 12) = ?",
                         (name, token_id)).rowcount


def token_login(token):
    """(user, portfolio, can_write) of an API token bound to a portfolio, or None; notes when it was
    last used."""
    if not token:
        return None
    h = _token_hash(token)
    r = conn().execute("SELECT user, portfolio, can_write FROM api_token WHERE token_hash = ? "
                       "AND portfolio IS NOT NULL", (h,)).fetchone()
    if r:
        with tx() as c:
            c.execute("UPDATE api_token SET last_used = ? WHERE token_hash = ?",
                      (datetime.now().isoformat(timespec="seconds"), h))
    return (r[0], r[1], bool(r[2])) if r else None


# ---------- backup ----------

def backup_dir():
    if os.environ.get("PORTFOLIO_BACKUP_DIR"):
        return os.environ["PORTFOLIO_BACKUP_DIR"]
    for d in BACKUP_DIRS:
        if os.path.isdir(os.path.dirname(d)):    # the share is mounted; the folder may be new
            return d
    sys.exit("no backup share mounted (looked for " + ", ".join(os.path.dirname(d) for d in BACKUP_DIRS)
             + ") — mount it, or give a directory")


def backup(dest=None, tag=""):
    """A consistent copy of the DB as <dest>/portfolio-<timestamp>[-tag].db; its path."""
    dest = dest or backup_dir()
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, f"portfolio-{datetime.now():%Y%m%d-%H%M%S}{'-' + tag if tag else ''}.db")
    target = sqlite3.connect(path)
    with target:
        conn().backup(target)
    target.close()
    return path


# ---------- seed ----------

def seed_sql():
    """The registry and the global settings as INSERTs — scripts/seed.sql, the base a new
    database starts from."""
    def lit(v):
        return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"
    out = ["-- The base a new data/portfolio.db starts from: the registry and the global settings.",
           "-- Generated by `python3 scripts/db.py seed > scripts/seed.sql`; loaded by sqlite-install.sh.",
           "BEGIN;"]
    for table, (_, fields) in REGISTRY.items():
        for r in rows(f"SELECT {', '.join(fields)} FROM {table} ORDER BY rowid"):
            out.append(f"INSERT INTO {table} ({', '.join(fields)}) VALUES "
                       f"({', '.join(lit(r[f]) for f in fields)});")
    for r in rows("SELECT key, value FROM setting WHERE portfolio = '' ORDER BY rowid"):
        out.append(f"INSERT INTO setting (portfolio, key, value) VALUES ('', {lit(r['key'])}, {lit(r['value'])});")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


# ---------- command line ----------

def main():
    args = sys.argv[1:]
    usage = __doc__.strip().split("\n\n")[1]
    if not args:
        sys.exit(usage)
    cmd, rest = args[0], args[1:]
    if cmd == "config":
        portfolio = ""
        if rest[:1] == ["--portfolio"] and len(rest) >= 2:
            portfolio, rest = rest[1], rest[2:]
        if not rest:
            print(json.dumps(settings(portfolio) if portfolio else config(), indent=2, ensure_ascii=False))
        elif rest[0] == "set" and len(rest) == 3:
            try:
                value = json.loads(rest[2])
            except ValueError:
                sys.exit(f"{rest[2]!r} is not JSON — a string needs its quotes: '\"{rest[2]}\"'")
            set_setting(rest[1], value, portfolio)
        elif rest[0] == "unset" and len(rest) == 2:
            unset_setting(rest[1], portfolio)
        else:
            sys.exit(usage)
    elif cmd == "upsert" and len(rest) >= 2 and rest[0] in REGISTRY:
        row = dict(a.split("=", 1) for a in rest[1:] if "=" in a)
        if REGISTRY[rest[0]][0] not in row:
            sys.exit(f"{rest[0]} rows are keyed by {REGISTRY[rest[0]][0]} — give {REGISTRY[rest[0]][0]}=...")
        upsert(rest[0], row)
    elif cmd == "delete" and len(rest) == 2 and rest[0] in REGISTRY:
        if not delete(rest[0], rest[1]):
            sys.exit(f"no {rest[0]} {rest[1]!r}")
    elif cmd == "query" and len(rest) == 1:
        ro = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            print(json.dumps([dict(r) for r in ro.execute(rest[0])], indent=1, ensure_ascii=False))
        except sqlite3.Error as err:
            sys.exit(f"query failed: {err}")
    elif cmd == "backup" and len(rest) <= 1:
        print(backup(rest[0] if rest else None))
    elif cmd == "seed" and not rest:
        sys.stdout.write(seed_sql())
    else:
        sys.exit(usage)


if __name__ == "__main__":
    main()
