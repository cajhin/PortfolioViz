---
# portfolioviz-kkoc
title: 'SQLite step 2: profiles, ledgers, purge'
status: completed
type: task
priority: normal
created_at: 2026-10-02T16:50:48Z
updated_at: 2026-10-02T17:07:56Z
parent: portfolioviz-wo4g
blocked_by:
    - portfolioviz-uaaf
---

- [x] schema: profile, ledger, position, activity, cash; profile.json keys → setting
- [x] import_tr.py, manual_tx.py, trade.py, manage-accounts.py, server.py on the DB
- [x] scripts/import_parqet.py; REFRESH_PARQET_DATA.md writes staged CSVs and imports them
- [x] manage-accounts.py purge <profile> --yes (backup first; exports moved to backup/)
- [x] page + check_portfolio.js: profile data from api/ routes
- [x] migrate private-profiles/ into the DB; move old files to backup/
- [x] check_portfolio clean for every profile; docs (CLAUDE.md, README, script headers, model header)

## Summary of Changes

- Profiles in the DB: profile (label/source/allow_cli), setting (its own keys), ledger (TR + manual rows as JSON, TR deletions flagged), position/activity/cash.
- import_tr.py, manual_tx.py (change + rebuild in one transaction), trade.py (per-account lock file in data/locks/), manage-accounts.py, server.py on the DB.
- api/profile, api/positions, api/activities, api/cash; page and check harness use them.
- scripts/import_parqet.py with the refresh checks built in; REFRESH_PARQET_DATA.md stages files in private-profiles/<p>/exports/ and imports them.
- manage-accounts.py purge <profile> --yes [--backup-dir]: backs up first, refuses the default profile, moves the leftover folder to backup/purged/.
- Migrated all 7 profiles; rebuilding each manual profile from its DB ledger reproduces the stored rows (apart from today's prices). Old files moved to backup/ (config.json + registry/ via git mv).
- Docs: CLAUDE.md, README.md, REFRESH_PARQET_DATA.md, start.sh, script headers, page comments.
- check_portfolio.js clean for all 7 profiles after the old files were moved away. First DB backup on the NAS.
