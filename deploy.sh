#!/usr/bin/env bash
# Run on dev to copy the working versions of Git-tracked files to production.
# Requires rsync locally and on cube, and SSH access to jjj@cube.
#
#   ./deploy.sh       check clean Git, back up prod, copy, migrate, restart, check
#   ./deploy.sh -n    preview the transfer
#
# Git metadata and untracked files are excluded. Existing production files that
# aren't in the transfer (including the database) are left in place.
# Remote database commands run as jjj; restarting the system service needs sudo.
# Uses the production service portfolioviz and its local port 8000.

set -euo pipefail
cd "$(dirname "$0")"

target='jjj@cube:/mnt/data/web/portfolioviz/'
args=(--itemize-changes)
dry=0
case "${1:-}" in
    -n|--dry-run) args+=(--dry-run); dry=1 ;;
    "") ;;
    *) echo "usage: $0 [-n|--dry-run]" >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { echo "usage: $0 [-n|--dry-run]" >&2; exit 2; }

echo "Checking the local Git working tree"
status=$(git status --porcelain --untracked-files=all)
if [ -n "$status" ]; then
    printf '%s\n' "$status" >&2
    echo "deploy: local Git working tree is not clean — commit or stash changes before deploying" >&2
    exit 1
fi

command -v rsync >/dev/null 2>&1 || { echo "deploy: rsync not found" >&2; exit 1; }
manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT
# NUL separators also handle spaces and newlines in tracked filenames.
git ls-files -z > "$manifest"
[ -s "$manifest" ] || { echo "deploy: no tracked files found" >&2; exit 1; }

if [ "$dry" = 0 ]; then
    echo "Backing up the production database before copying files"
    ssh jjj@cube 'bash -se' <<'REMOTE_BACKUP'
set -euo pipefail
cd /mnt/data/web/portfolioviz
backup=$(python3 scripts/db.py backup | tail -1)
[ -s "$backup" ] || { echo "deploy: no production backup made — stopping" >&2; exit 1; }
printf 'Production backup: %s\n' "$backup"
REMOTE_BACKUP
fi

printf 'Copying Git-tracked working files to %s\n' "$target"
rsync -a --from0 --files-from="$manifest" \
    -e ssh "${args[@]}" ./ "$target"

if [ "$dry" = 1 ]; then
    echo "Dry run: a real deploy backs up before copying, then migrates, restarts portfolioviz, and checks HTTP on cube."
    exit 0
fi

echo "Migrating the production database"
ssh jjj@cube 'bash -se' <<'REMOTE_MIGRATE'
set -euo pipefail
cd /mnt/data/web/portfolioviz
python3 scripts/db.py config > /dev/null
REMOTE_MIGRATE

echo "Restarting portfolioviz and checking production"
# A terminal lets sudo prompt for a password when needed.
ssh -t jjj@cube '
    set -eu
    sudo systemctl restart portfolioviz
    sleep 2
    sudo systemctl is-active --quiet portfolioviz
    for path in login.html agents.md; do
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://127.0.0.1:8000/$path")
        printf "/%s: %s\n" "$path" "$code"
        [ "$code" = 200 ] || exit 1
    done
'
echo "Deploy complete."
