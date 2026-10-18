# Deploy state_guard on a Hermes host

## 1. Copy the script

On the Hermes host (example paths; set `HERMES_HOME` if yours is not `~/.hermes`):

```bash
install -d ~/.hermes/scripts ~/.hermes/logs ~/.hermes/backups
install -m 0755 state_guard.py ~/.hermes/scripts/state_guard.py
```

The script reads `HERMES_HOME` (default `~/.hermes`). Logs go to
`$HERMES_HOME/logs/state_guard.log`. Snapshots go under
`$HERMES_HOME/backups/<target_id>/`.

## 2. Smoke test

```bash
python3 ~/.hermes/scripts/state_guard.py --status
python3 ~/.hermes/scripts/state_guard.py --verbose
```

First run creates verified `*.bak` files. Confirm:

- each target has a `*.YYYYMMDD_HHMMSS.bak`
- no pile of `.tmp-wal` / `.tmp-shm` next to them
- `watchdog_status.json` exists under `$HERMES_HOME/backups/`

## 3. Hermes cron (`no_agent`)

Do not put an LLM on this job. Integrity is not a prompt.

Example job shape (Hermes cron / `jobs.json`):

- `name`: `state-guard`
- `script`: `state_guard.py` (resolved under `~/.hermes/scripts/`)
- `no_agent`: `true`
- `schedule`: `*/15 * * * *`
- `delivery`: only if you want recovery/alert stdout; healthy ticks print nothing

If you install without Hermes cron, systemd timer or system cron is fine:

```
*/15 * * * * /usr/bin/python3 $HOME/.hermes/scripts/state_guard.py
```

## 4. Extra targets (plugins and other `.db` files)

Default discovery does **not** glob the whole tree.

Create `$HERMES_HOME/state_guard_targets.json` (or set `STATE_GUARD_TARGETS`
to another path). See `examples/state_guard_targets.json`.

Rules:

- Durable SQLite only. Caches, FTS scratch, huge feeds: no.
- Path must exist and be non-empty or it is skipped until the next tick.
- Unique `id` per target. Do not reuse `root_state` / `mnemosyne_db`.
- Same 7-copy pool and size watchdog (30% shrink / 150% growth vs prior snapshot).

Plugin authors: declare the durable path in your plugin README and ship a
snippet for `state_guard_targets.json`. Do not instruct operators to copy
`*.db` while WAL is open.

## 5. Restore behavior (read this before you need it)

On corrupt or missing live DB:

1. Quarantine live file plus `-wal`/`-shm` with a timestamp suffix.
2. Try snapshots newest first; restore the first that still verifies.
3. Verify again after atomic replace.

Rolling back ~90 minutes can disagree with session files on disk. There is no
full session surgeon in this script. Treat auto-restore as last resort, then
inspect quarantined files under the live DB directory.

If every snapshot fails `quick_check`, the script does not invent a database.

If Mnemosyne is corrupt while another process holds the database, the script refuses auto-restore and alerts instead of performing a live swap. The check is a short exclusive lock. It does not call systemctl.

## 6. What this is not

- Not 7 days of backups.
- Not a substitute for VM snapshots or off-box copies.
- Not file integrity monitoring.
- Not a license to snapshot every SQLite file a plugin creates.
