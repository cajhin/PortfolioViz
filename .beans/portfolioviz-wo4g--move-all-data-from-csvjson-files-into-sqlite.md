---
# portfolioviz-wo4g
title: Move all data from CSV/JSON files into SQLite
status: completed
type: epic
priority: normal
created_at: 2026-10-02T16:50:35Z
updated_at: 2026-10-02T17:07:56Z
---

Every file the page and the scripts read or write — registry, config, price/FX history, live and
latest prices, profiles (settings, ledgers, positions, activities, cash) — moves into one SQLite
database, data/portfolio.db (gitignored, not in git; backed up by hand to the NAS).

Decisions (agreed with the user, 2026-10-02):
- One DB file. Not committed. Backup is manual: `scripts/db.py backup` → smb toast/nas/bkp/portfolioviz/
  (/Volumes/nas/bkp/portfolioviz on macOS, /mnt/nas/bkp/portfolioviz on Linux).
- No .csv/.json data file stays in use. The old files stay in git, moved to backup/. Exception:
  import files — Trade Republic exports (and the Parqet refresh's staged CSVs) are still read on import.
- config.json and every profile.json move into the DB (table `setting`, profile '' = global).
- scripts/sqlite-install.sh installs the sqlite3 CLI if missing and creates the DB from
  scripts/schema.sql + scripts/seed.sql (current registry + config).
- Page talks to the DB only through server.py (`api/...` routes, CSV/JSON bodies as before);
  scripts/db.py is the only module that opens the DB.
- manage-accounts.py is admin-only (agents use trade.py only); it gets `purge` for obsolete profiles.
- check_portfolio.js answers the api/ routes through the same Python code as server.py; every
  profile's baseline must stay unchanged.

## Summary of Changes

Both steps done (portfolioviz-uaaf, portfolioviz-kkoc). All data is in data/portfolio.db; no data file is read any more except import files (TR exports, staged Parqet refreshes).
