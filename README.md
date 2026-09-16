# state-guard

Stdlib-only SQLite watchdog for Hermes.

Every 15 minutes it integrity-checks live databases, takes a verified snapshot
with the SQLite backup API, keeps exactly 7 copies per target, and auto-restores
from the newest good copy if the live file is missing or fails `PRAGMA quick_check`.

Default targets:

- `~/.hermes/state.db`
- `~/.hermes/mnemosyne/data/mnemosyne.db`
- each non-empty `~/.hermes/profiles/*/state.db`

Optional extra files (plugins, kanban, mail indexes) via `state_guard_targets.json`.
Do not glob every `*.db`. Caches and huge feeds do not belong here.

## What 7 copies means

`*/15` plus keep-last 7 is about 1 hour 45 minutes of history, not 7 days.
This is a crash/corruption window. Hypervisor dumps and off-box tarballs are
the longer layer. Do not confuse them.

## Quick start

```bash
python3 state_guard.py --status
python3 state_guard.py --verbose
```

Exit 0 on a healthy tick. Recovery and size alerts print to stdout so a
Hermes `no_agent` cron can deliver them. Green ticks stay quiet.

See [DEPLOY.md](DEPLOY.md) for install, cron, restore behavior, and plugin opt-in.

## Safety notes

- Destination backup uses `PRAGMA journal_mode=OFF` and unlinks `-wal`/`-shm`.
- Prune only `*.bak`, then `sweep_orphans()` so leftover `.tmp-*` cannot fill the disk.
- Never snapshot a source that already failed `quick_check`.
- Restore quarantines the live file first. If all 7 copies fail checks, it stops.
- `cp` of a live WAL database is not a backup. This script uses `Connection.backup()`.
