# Changelog

## 2026-10-18

### Changed
- Mnemosyne auto-restore now refuses while another process holds the database. The check is a short exclusive SQLite lock. The script no longer calls `systemctl`.
- SQLite backups copy 100 pages at a time so a large copy does not stall the live process.

### Fixed
- A missing `systemctl` binary can no longer abort a restore check, because that probe is gone.
- A corrupt file that is not locked still restores. Only a lock refusal leaves the live file in place and alerts.
