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
(/Volumes/nas/bkp/portfolioviz on macOS, /mnt/nas/bkp/portfolioviz on Linux, or $PORTFOLIO_BACKUP_DIR).
It copies through SQLite's backup API, so a copy taken while the server or a fetch is writing is
still consistent — which copying the file is not. Users and their access are scripts/manage-users.py's.

$PORTFOLIO_DB points everything at another database file (a test copy, say).
"""
import contextlib, hashlib, hmac, json, os, secrets, sqlite3, sys, threading
from datetime import datetime, timedelta

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)                  # the repo — this file lives in scripts/
DB_PATH = os.environ.get("PORTFOLIO_DB") or os.path.join(ROOT, "data", "portfolio.db")
BACKUP_DIRS = ("/Volumes/nas/bkp/portfolioviz", "/mnt/nas/bkp/portfolioviz")

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

SCHEMA_VERSION = 4


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


MIGRATIONS = {2: _to_2, 3: _to_3, 4: _to_4}


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
# A portfolio is one row in `portfolio` (label, type) plus its own settings (watchlist,
# benchmarkIsin, parqetPortfolios, ...) — together what its profile.json used to say.

POSITION_FIELDS = ["depot", "name", "identifier", "assetType", "isSold", "shares", "currency",
                   "currentValue", "purchaseValue", "lastPriceDate", "lastPrice", "realizedGainNet",
                   "unrealizedGainNet", "earliestActivityDate", "activityCount"]
ACTIVITY_FIELDS = ["depot", "name", "identifier", "type", "datetime", "shares", "price", "amount",
                   "amountNet", "fee", "tax", "realizedGains", "realizedGainsNet", "currency",
                   "transactionId"]
CASH_FIELDS = ["depot", "datetime", "date", "kind", "amount", "transactionId"]
PORTFOLIO_TABLES = {"position": POSITION_FIELDS, "activity": ACTIVITY_FIELDS, "cash": CASH_FIELDS}


def portfolio(name):
    """The portfolio's row as {name, label, type}, or None."""
    r = conn().execute("SELECT * FROM portfolio WHERE name = ?", (name,)).fetchone()
    return dict(r) if r else None


def portfolios():
    return rows("SELECT * FROM portfolio ORDER BY name")


def portfolio_config(name):
    """Its label and type with its own settings — what its profile.json used to hold."""
    p = portfolio(name)
    if not p:
        return None
    return {"label": p["label"], "type": p["type"], **settings(name)}


TYPES = ("parqet", "manual", "game")


def create_portfolio(name, label, type_):
    """A new, empty portfolio with an empty watchlist; sqlite3.IntegrityError if the name is taken."""
    with tx() as c:
        c.execute("INSERT INTO portfolio (name, label, type) VALUES (?, ?, ?)", (name, label or name, type_))
        c.execute("INSERT INTO setting (portfolio, key, value) VALUES (?, 'watchlist', '[]')", (name,))


def set_label(name, label):
    with tx() as c:
        return c.execute("UPDATE portfolio SET label = ? WHERE name = ?", (label, name)).rowcount


def purge_portfolio(name):
    """Every row the portfolio has — ledgers, positions, activities, cash, settings. The registry and
    the prices stay: they are shared."""
    with tx() as c:
        c.execute("DELETE FROM setting WHERE portfolio = ?", (name,))
        return c.execute("DELETE FROM portfolio WHERE name = ?", (name,)).rowcount   # cascades


def portfolio_rows(name, table):
    """A portfolio's positions, activities or cash, as text, in the order they were written."""
    fields = PORTFOLIO_TABLES[table]
    return rows(f"SELECT {', '.join(fields)} FROM {table} WHERE portfolio = ? ORDER BY rowid", (name,))


def replace_portfolio_rows(name, table, new):
    fields = PORTFOLIO_TABLES[table]
    with tx() as c:
        c.execute(f"DELETE FROM {table} WHERE portfolio = ?", (name,))
        c.executemany(f"INSERT INTO {table} (portfolio, {', '.join(fields)}) VALUES "
                      f"(?, {', '.join('?' * len(fields))})",
                      [[name] + [r.get(f) for f in fields] for r in new])


# ---------- ledgers ----------
# A manual portfolio's source of truth: the Trade Republic rows it imported (origin 'tr') and the
# rows entered by hand or traded (origin 'manual'), each the TR export's row as a dict.

def ledger(name, origin, deleted=True):
    """One origin's rows, oldest first (same-time rows in the order they were written). An
    imported row deleted on the page is left out unless `deleted`."""
    return [json.loads(r[0]) for r in conn().execute(
        "SELECT data FROM ledger WHERE portfolio = ? AND origin = ?" + ("" if deleted else " AND NOT deleted")
        + " ORDER BY datetime, rowid", (name, origin))]


def add_ledger_rows(name, origin, new):
    with tx() as c:
        c.executemany("INSERT INTO ledger (portfolio, transaction_id, origin, datetime, data) VALUES (?, ?, ?, ?, ?)",
                      [(name, r["transaction_id"], origin, r["datetime"], json.dumps(r, ensure_ascii=False))
                       for r in new])


def replace_ledger(name, origin, new):
    """All of one origin's rows at once — how a hand-entered row is added, edited or removed."""
    with tx() as c:
        c.execute("DELETE FROM ledger WHERE portfolio = ? AND origin = ?", (name, origin))
        add_ledger_rows(name, origin, new)


def mark_deleted(name, tid):
    """An imported row deleted on the page: kept, so a re-import still knows it, but left out."""
    with tx() as c:
        return c.execute("UPDATE ledger SET deleted = 1 WHERE portfolio = ? AND transaction_id = ? AND origin = 'tr'",
                         (name, tid)).rowcount


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


def grant(name, portfolio_name, can_write=True):
    with tx() as c:
        c.execute("INSERT INTO user_portfolio (user, portfolio, can_write) VALUES (?, ?, ?) "
                  "ON CONFLICT (user, portfolio) DO UPDATE SET can_write = excluded.can_write",
                  (name, portfolio_name, 1 if can_write else 0))


def revoke(name, portfolio_name):
    with tx() as c:
        return c.execute("DELETE FROM user_portfolio WHERE user = ? AND portfolio = ?",
                         (name, portfolio_name)).rowcount


def access(name, portfolio_name):
    """None if the user may not open the portfolio, else whether they may change it."""
    r = conn().execute("SELECT can_write FROM user_portfolio WHERE user = ? AND portfolio = ?",
                       (name, portfolio_name)).fetchone()
    return None if r is None else bool(r[0])


def user_portfolios(name):
    """The portfolio rows the user may open, each with its `can_write`."""
    return rows("SELECT p.*, g.can_write FROM portfolio p JOIN user_portfolio g ON g.portfolio = p.name "
                "WHERE g.user = ? ORDER BY p.name", (name,))


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
