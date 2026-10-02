-- The whole data model of portfolioviz: data/portfolio.db, created by scripts/sqlite-install.sh.
-- scripts/db.py is the only code that opens it. Columns named like the CSV headers they replaced
-- (camelCase where Parqet's schema was), so a table serves as that CSV again unchanged — see
-- scripts/api.py. Row order is insertion order (rowid) wherever the order was meaningful.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '2');   -- db.py SCHEMA_VERSION

-- ---------- settings: was config.json (profile '') and private-profiles/<p>/profile.json ----------
-- value is JSON. A profile's keys override the global ones (flat, shallow), except currency.
-- label/source/allow-cli live in `profile`, not here.
CREATE TABLE IF NOT EXISTS setting (
  profile TEXT NOT NULL DEFAULT '',
  key     TEXT NOT NULL,
  value   TEXT NOT NULL,
  PRIMARY KEY (profile, key)
);

-- ---------- registry: shared by every profile ----------
CREATE TABLE IF NOT EXISTS instrument (
  id       TEXT PRIMARY KEY,                 -- equal to the ISIN for a security
  isin     TEXT NOT NULL DEFAULT '',
  slug     TEXT NOT NULL UNIQUE,
  name     TEXT NOT NULL DEFAULT '',
  display  TEXT NOT NULL DEFAULT '',
  type     TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT '',
  sector   TEXT NOT NULL DEFAULT '',
  note     TEXT NOT NULL DEFAULT ''
);

-- one row per instrument: no fallback, no priority
CREATE TABLE IF NOT EXISTS price_source (
  id             TEXT PRIMARY KEY REFERENCES instrument (id) ON DELETE CASCADE,
  source         TEXT NOT NULL DEFAULT '',     -- yahoo | manual
  symbol         TEXT NOT NULL DEFAULT '',
  quote_currency TEXT NOT NULL DEFAULT '',
  fx_symbol      TEXT NOT NULL DEFAULT '',
  note           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sector_color (
  sector TEXT PRIMARY KEY,
  hue    TEXT NOT NULL DEFAULT '',
  sat    TEXT NOT NULL DEFAULT ''
);

-- ---------- prices: derived, reproducible from price_source ----------
-- close in the portfolio currency, converted on write; close_raw the untouched quote
CREATE TABLE IF NOT EXISTS price (
  id             TEXT NOT NULL REFERENCES instrument (id) ON DELETE CASCADE,
  date           TEXT NOT NULL,
  close          REAL NOT NULL,
  close_raw      REAL,
  quote_currency TEXT NOT NULL DEFAULT '',
  source         TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (id, date)
) WITHOUT ROWID;

-- the freshest close per instrument (was gen_prices/_latest.csv)
CREATE VIEW IF NOT EXISTS latest_close AS
  SELECT p.id, p.date, p.close, p.source
  FROM price p
  JOIN (SELECT id, MAX(date) AS date FROM price GROUP BY id) m ON m.id = p.id AND m.date = p.date;

-- a price fresher than any close, kept apart from the closes (was gen_prices/_live.csv)
CREATE TABLE IF NOT EXISTS live_price (
  id             TEXT PRIMARY KEY REFERENCES instrument (id) ON DELETE CASCADE,
  date           TEXT NOT NULL,
  at             TEXT NOT NULL,
  price          REAL NOT NULL,
  price_raw      REAL,
  quote_currency TEXT NOT NULL DEFAULT '',
  source         TEXT NOT NULL DEFAULT ''
);

-- daily rates per Yahoo FX symbol, e.g. EURUSD=X (was gen_fx/<PAIR>.csv)
CREATE TABLE IF NOT EXISTS fx_rate (
  pair TEXT NOT NULL,
  date TEXT NOT NULL,
  rate REAL NOT NULL,
  PRIMARY KEY (pair, date)
) WITHOUT ROWID;

-- ---------- profiles: was private-profiles/<p>/ ----------
CREATE TABLE IF NOT EXISTS profile (
  name      TEXT PRIMARY KEY,
  label     TEXT NOT NULL,
  source    TEXT NOT NULL CHECK (source IN ('parqet', 'manual')),
  allow_cli INTEGER NOT NULL DEFAULT 0
);

-- A manual profile's source of truth: every Trade Republic row imported (origin 'tr', kept even
-- when deleted on the page, so a re-import does not revive it) and every hand-entered or traded
-- row (origin 'manual'). `data` is the row as JSON, every column of the export kept.
CREATE TABLE IF NOT EXISTS ledger (
  profile        TEXT NOT NULL REFERENCES profile (name) ON DELETE CASCADE,
  transaction_id TEXT NOT NULL,
  origin         TEXT NOT NULL CHECK (origin IN ('tr', 'manual')),
  datetime       TEXT NOT NULL,
  deleted        INTEGER NOT NULL DEFAULT 0,
  data           TEXT NOT NULL,
  PRIMARY KEY (profile, transaction_id)
);

-- What the page reads: a Parqet profile's export as imported, or what import_tr.py rebuilt from
-- the ledger. Numbers stay text, exactly as written, so the page reads what it always read.
CREATE TABLE IF NOT EXISTS position (
  profile TEXT NOT NULL REFERENCES profile (name) ON DELETE CASCADE,
  depot TEXT, name TEXT, identifier TEXT, assetType TEXT, isSold TEXT, shares TEXT,
  currency TEXT, currentValue TEXT, purchaseValue TEXT, lastPriceDate TEXT, lastPrice TEXT,
  realizedGainNet TEXT, unrealizedGainNet TEXT, earliestActivityDate TEXT, activityCount TEXT
);
CREATE INDEX IF NOT EXISTS position_profile ON position (profile);

CREATE TABLE IF NOT EXISTS activity (
  profile TEXT NOT NULL REFERENCES profile (name) ON DELETE CASCADE,
  depot TEXT, name TEXT, identifier TEXT, type TEXT, datetime TEXT, shares TEXT, price TEXT,
  amount TEXT, amountNet TEXT, fee TEXT, tax TEXT, realizedGains TEXT, realizedGainsNet TEXT,
  currency TEXT, transactionId TEXT
);
CREATE INDEX IF NOT EXISTS activity_profile ON activity (profile);

-- each booking's effect on its account's cash (manual profiles only)
CREATE TABLE IF NOT EXISTS cash (
  profile TEXT NOT NULL REFERENCES profile (name) ON DELETE CASCADE,
  depot TEXT, datetime TEXT, date TEXT, kind TEXT, amount TEXT, transactionId TEXT
);
CREATE INDEX IF NOT EXISTS cash_profile ON cash (profile);

-- ---------- access: who may see and change which profile (server.py's login) ----------
-- Not a profile's own and not the registry: people, managed by scripts/manage-users.py and the
-- login page's "Create account". pw_hash is scrypt (see db.hash_password).
CREATE TABLE IF NOT EXISTS user (
  name    TEXT PRIMARY KEY,
  pw_hash TEXT NOT NULL,
  created TEXT NOT NULL
);

-- what a user may open; can_write for its changes (rename, import, transactions)
CREATE TABLE IF NOT EXISTS user_profile (
  user      TEXT NOT NULL REFERENCES user (name) ON DELETE CASCADE,
  profile   TEXT NOT NULL REFERENCES profile (name) ON DELETE CASCADE,
  can_write INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user, profile)
);

-- one row per logged-in browser; the cookie holds the token, this only its sha256
CREATE TABLE IF NOT EXISTS session (
  token_hash TEXT PRIMARY KEY,
  user       TEXT NOT NULL REFERENCES user (name) ON DELETE CASCADE,
  expires    TEXT NOT NULL
);
