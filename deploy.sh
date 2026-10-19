#!/usr/bin/env bash
# Update this host's Portfolioviz to what is on origin/main — for prod, where the database must stay
# intact. Code comes from git, fast-forward only; the data (data/portfolio.db) is never replaced,
# only migrated in place, after a backup.
#
#   ./deploy.sh            back up, pull, migrate, restart the service, check it answers
#   ./deploy.sh -n         only show what would change (the commits), touch nothing
#
#   PORTFOLIOVIZ_SERVICE   the systemd unit to restart (default: portfolioviz)
#   PORTFOLIOVIZ_SYSTEMCTL how to call systemctl (default: "sudo systemctl"; "systemctl --user" for a
#                          user unit)
#   PORTFOLIOVIZ_PORT      where the server listens, for the check afterwards (default: 8000)
#
# Steps, each stopping the deploy on failure:
#   1. refuse unless on main with no local changes to tracked files — prod is never edited in place
#   2. git fetch; nothing new: done
#   3. scripts/db.py backup — a consistent copy to the NAS, made by the *old* code, before anything
#   4. git pull --ff-only
#   5. migrate now (any script opening the database does it) — so a failure shows here, not on the
#      first page request
#   6. restart the service, check it is active and answers
# Rollback — the old commit and that backup, together (migrations only go forward): printed at the end.

set -euo pipefail
cd "$(dirname "$0")"

service=${PORTFOLIOVIZ_SERVICE:-portfolioviz}
systemctl=${PORTFOLIOVIZ_SYSTEMCTL:-sudo systemctl}
port=${PORTFOLIOVIZ_PORT:-8000}
dry=0
case "${1:-}" in
    -n|--dry-run) dry=1 ;;
    "") ;;
    *) echo "usage: $0 [-n]" >&2; exit 2 ;;
esac

say() { printf '\n== %s\n' "$*"; }
schema() { python3 scripts/db.py query "SELECT value FROM meta WHERE key = 'schema_version'" 2>/dev/null \
           | python3 -c 'import json, sys; r = json.load(sys.stdin); print(r[0]["value"] if r else "?")' 2>/dev/null || echo "?"; }

say "checking the work tree"
branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = main ] || { echo "on branch $branch, not main — refusing" >&2; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short --untracked-files=no
    echo "local changes to tracked files — prod is not edited in place; commit them elsewhere or" \
         "'git restore' them, then deploy again" >&2
    exit 1
fi

say "fetching"
git fetch --quiet origin main
old=$(git rev-parse HEAD)
new=$(git rev-parse origin/main)
if [ "$old" = "$new" ]; then echo "already at origin/main ($(git log -1 --format='%h %s'))"; exit 0; fi
git merge-base --is-ancestor "$old" "$new" \
    || { echo "origin/main is not ahead of this checkout (diverged?) — refusing" >&2; exit 1; }
echo "$(git rev-list --count "$old..$new") new commit(s):"
git log --oneline "$old..$new"
[ "$dry" = 1 ] && { echo; echo "(dry run — nothing changed)"; exit 0; }

say "backing up the database"
schema_old=$(schema)
backup=$(python3 scripts/db.py backup | tail -1)
[ -s "$backup" ] || { echo "no backup made ($backup) — stopping before any change" >&2; exit 1; }
echo "$backup  (schema $schema_old)"

say "pulling"
git pull --ff-only --quiet origin main
echo "now at $(git log -1 --format='%h %s')"

say "migrating"
python3 scripts/db.py config > /dev/null        # opening the database migrates it
schema_new=$(schema)
echo "schema $schema_old -> $schema_new"

say "restarting $service"
$systemctl restart "$service"
sleep 2
$systemctl is-active --quiet "$service" || { $systemctl status "$service" --no-pager | tail -20; exit 1; }
for path in login.html agents.md; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/$path" || true)
    echo "/$path: $code"
    [ "$code" = 200 ] || { echo "the server does not answer as it should — see: $systemctl status $service" >&2; exit 1; }
done

say "done: ${old:0:7} -> ${new:0:7}, schema $schema_old -> $schema_new"
cat <<EOF
To roll back, code and data together (newer migrations cannot be undone by older code):
  $systemctl stop $service
  git reset --hard $old
  cp '$backup' data/portfolio.db
  $systemctl start $service
EOF
