#!/usr/bin/env python3
"""The one way into data/portfolio.db — every other script imports this, none opens the DB itself.

    python3 scripts/db.py config [--profile P]                    # the settings, as JSON
    python3 scripts/db.py config [--profile P] set <key> <json>   # e.g. set timelineStart '"2019-01-01"'
    python3 scripts/db.py config [--profile P] unset <key>
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
    return c


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

def settings(profile=""):
    """The keys stored for one scope — '' for the global ones (was config.json), else a profile's own
    (was its profile.json, less label/source/allow-cli, which are its `profile` row)."""
    return {r["key"]: json.loads(r["value"])
            for r in conn().execute("SELECT key, value FROM setting WHERE profile = ? ORDER BY rowid",
                                    (profile,))}


def config():
    """The global settings over the built-in defaults."""
    return {**DEFAULTS, **settings("")}


def set_setting(key, value, profile=""):
    with tx() as c:
        c.execute("INSERT INTO setting (profile, key, value) VALUES (?, ?, ?) "
                  "ON CONFLICT (profile, key) DO UPDATE SET value = excluded.value",
                  (profile, key, json.dumps(value, ensure_ascii=False)))


def unset_setting(key, profile=""):
    with tx() as c:
        c.execute("DELETE FROM setting WHERE profile = ? AND key = ?", (profile, key))


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


# ---------- profiles ----------
# A profile is one row in `profile` (label, source, allow_cli) plus its own settings (watchlist,
# benchmarkIsin, parqetPortfolios, ...) — together what its profile.json used to say.

POSITION_FIELDS = ["portfolio", "name", "identifier", "assetType", "isSold", "shares", "currency",
                   "currentValue", "purchaseValue", "lastPriceDate", "lastPrice", "realizedGainNet",
                   "unrealizedGainNet", "earliestActivityDate", "activityCount"]
ACTIVITY_FIELDS = ["portfolio", "name", "identifier", "type", "datetime", "shares", "price", "amount",
                   "amountNet", "fee", "tax", "realizedGains", "realizedGainsNet", "currency",
                   "transactionId"]
CASH_FIELDS = ["portfolio", "datetime", "date", "kind", "amount", "transactionId"]
PROFILE_TABLES = {"position": POSITION_FIELDS, "activity": ACTIVITY_FIELDS, "cash": CASH_FIELDS}


def profile(name):
    """The profile's row as {name, label, source, allow_cli}, or None."""
    r = conn().execute("SELECT * FROM profile WHERE name = ?", (name,)).fetchone()
    return dict(r) if r else None


def profiles():
    return rows("SELECT * FROM profile ORDER BY name")


def profile_config(name):
    """What the profile's profile.json used to hold: label, source, allow-cli and its settings."""
    p = profile(name)
    if not p:
        return None
    return {"label": p["label"], "source": p["source"],
            **({"allow-cli": True} if p["allow_cli"] else {}), **settings(name)}


def create_profile(name, label, source, allow_cli=False):
    """A new, empty profile with an empty watchlist; sqlite3.IntegrityError if the name is taken."""
    with tx() as c:
        c.execute("INSERT INTO profile (name, label, source, allow_cli) VALUES (?, ?, ?, ?)",
                  (name, label or name, source, 1 if allow_cli else 0))
        c.execute("INSERT INTO setting (profile, key, value) VALUES (?, 'watchlist', '[]')", (name,))


def set_label(name, label):
    with tx() as c:
        return c.execute("UPDATE profile SET label = ? WHERE name = ?", (label, name)).rowcount


def purge_profile(name):
    """Every row the profile has — ledgers, positions, activities, cash, settings. The registry and
    the prices stay: they are shared."""
    with tx() as c:
        c.execute("DELETE FROM setting WHERE profile = ?", (name,))
        return c.execute("DELETE FROM profile WHERE name = ?", (name,)).rowcount   # cascades


def profile_rows(name, table):
    """A profile's positions, activities or cash, as text, in the order they were written."""
    fields = PROFILE_TABLES[table]
    return rows(f"SELECT {', '.join(fields)} FROM {table} WHERE profile = ? ORDER BY rowid", (name,))


def replace_profile_rows(name, table, new):
    fields = PROFILE_TABLES[table]
    with tx() as c:
        c.execute(f"DELETE FROM {table} WHERE profile = ?", (name,))
        c.executemany(f"INSERT INTO {table} (profile, {', '.join(fields)}) VALUES "
                      f"(?, {', '.join('?' * len(fields))})",
                      [[name] + [r.get(f) for f in fields] for r in new])


# ---------- ledgers ----------
# A manual profile's source of truth: the Trade Republic rows it imported (origin 'tr') and the
# rows entered by hand or traded (origin 'manual'), each the TR export's row as a dict.

def ledger(name, origin, deleted=True):
    """One origin's rows, oldest first (same-time rows in the order they were written). An
    imported row deleted on the page is left out unless `deleted`."""
    return [json.loads(r[0]) for r in conn().execute(
        "SELECT data FROM ledger WHERE profile = ? AND origin = ?" + ("" if deleted else " AND NOT deleted")
        + " ORDER BY datetime, rowid", (name, origin))]


def add_ledger_rows(name, origin, new):
    with tx() as c:
        c.executemany("INSERT INTO ledger (profile, transaction_id, origin, datetime, data) VALUES (?, ?, ?, ?, ?)",
                      [(name, r["transaction_id"], origin, r["datetime"], json.dumps(r, ensure_ascii=False))
                       for r in new])


def replace_ledger(name, origin, new):
    """All of one origin's rows at once — how a hand-entered row is added, edited or removed."""
    with tx() as c:
        c.execute("DELETE FROM ledger WHERE profile = ? AND origin = ?", (name, origin))
        add_ledger_rows(name, origin, new)


def mark_deleted(name, tid):
    """An imported row deleted on the page: kept, so a re-import still knows it, but left out."""
    with tx() as c:
        return c.execute("UPDATE ledger SET deleted = 1 WHERE profile = ? AND transaction_id = ? AND origin = 'tr'",
                         (name, tid)).rowcount


# ---------- access ----------
# Users, which profiles each may open (and change), and their logged-in sessions — server.py's
# login. The scripts themselves never ask: a command line is the admin's.

SESSION_DAYS = 30


def migrate():
    """Bring an older database up to schema.sql — every statement there is IF NOT EXISTS."""
    with open(os.path.join(SCRIPTS, "schema.sql"), encoding="utf-8") as fh:
        conn().executescript(fh.read())


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
    """Every user with the profiles they may open: {name, created, profiles: {profile: can_write}}."""
    out = {u["name"]: {"name": u["name"], "created": u["created"], "profiles": {}}
           for u in rows("SELECT name, created FROM user ORDER BY name")}
    for g in rows("SELECT * FROM user_profile ORDER BY profile"):
        out[g["user"]]["profiles"][g["profile"]] = bool(g["can_write"])
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


def grant(name, profile_name, can_write=True):
    with tx() as c:
        c.execute("INSERT INTO user_profile (user, profile, can_write) VALUES (?, ?, ?) "
                  "ON CONFLICT (user, profile) DO UPDATE SET can_write = excluded.can_write",
                  (name, profile_name, 1 if can_write else 0))


def revoke(name, profile_name):
    with tx() as c:
        return c.execute("DELETE FROM user_profile WHERE user = ? AND profile = ?",
                         (name, profile_name)).rowcount


def access(name, profile_name):
    """None if the user may not open the profile, else whether they may change it."""
    r = conn().execute("SELECT can_write FROM user_profile WHERE user = ? AND profile = ?",
                       (name, profile_name)).fetchone()
    return None if r is None else bool(r[0])


def user_profiles(name):
    """The profile rows the user may open, each with its `can_write`."""
    return rows("SELECT p.*, g.can_write FROM profile p JOIN user_profile g ON g.profile = p.name "
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
    for r in rows("SELECT key, value FROM setting WHERE profile = '' ORDER BY rowid"):
        out.append(f"INSERT INTO setting (profile, key, value) VALUES ('', {lit(r['key'])}, {lit(r['value'])});")
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
        profile = ""
        if rest[:1] == ["--profile"] and len(rest) >= 2:
            profile, rest = rest[1], rest[2:]
        if not rest:
            print(json.dumps(settings(profile) if profile else config(), indent=2, ensure_ascii=False))
        elif rest[0] == "set" and len(rest) == 3:
            try:
                value = json.loads(rest[2])
            except ValueError:
                sys.exit(f"{rest[2]!r} is not JSON — a string needs its quotes: '\"{rest[2]}\"'")
            set_setting(rest[1], value, profile)
        elif rest[0] == "unset" and len(rest) == 2:
            unset_setting(rest[1], profile)
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
