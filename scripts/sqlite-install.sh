#!/bin/sh
# Set up this host's database: install the sqlite3 command line tool if it is missing, then create
# data/portfolio.db from scripts/schema.sql and the base it starts from, scripts/seed.sql (the
# registry and the global settings — regenerate that with `python3 scripts/db.py seed`).
#
#   scripts/sqlite-install.sh
#
# Never touches an existing database: move it away first to start over (and restore a backup from
# the NAS with a plain copy instead, if that is what you are after). Prices are not in the seed —
# fetch them afterwards, as printed at the end. $PORTFOLIO_DB creates a database elsewhere.

set -eu
cd "$(dirname "$0")/.."

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "installing sqlite3"
    if command -v brew >/dev/null 2>&1; then brew install sqlite
    elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y sqlite
    elif command -v apt-get >/dev/null 2>&1; then sudo apt-get install -y sqlite3
    else echo "sqlite-install.sh: no brew, dnf or apt-get — install sqlite3 by hand" >&2; exit 1
    fi
fi
python3 -c "import sqlite3" 2>/dev/null || {
    echo "sqlite-install.sh: this python3 was built without its sqlite3 module" >&2; exit 1; }

db=${PORTFOLIO_DB:-data/portfolio.db}
if [ -e "$db" ]; then
    echo "sqlite-install.sh: $db exists — leaving it alone" >&2
    exit 1
fi
mkdir -p "$(dirname "$db")"
sqlite3 -bail "$db" < scripts/schema.sql >/dev/null
sqlite3 -bail "$db" < scripts/seed.sql
echo "created $db: $(sqlite3 "$db" 'SELECT count(*) FROM instrument') instruments, sqlite $(sqlite3 --version | cut -d' ' -f1)"
echo "next — fetch the price history:"
echo "  python3 scripts/update_prices.py --from $(sqlite3 "$db" "SELECT json_extract(value, '$') FROM setting WHERE portfolio = '' AND key = 'timelineStart'")"
