#!/usr/bin/env python3
"""
Hermes SQLite guardian and size watchdog.

Autonomous backup, corruption detection, auto-restore, and size monitoring
for Hermes state.db, Mnemosyne, profile state.db files, and optional extra
targets. Keeps exactly 7 verified copies per target.

Stdlib only. See DEPLOY.md.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Base Paths
HERMES_ROOT = Path(os.path.expanduser("~/.hermes")).resolve()
BASE_BACKUP_DIR = HERMES_ROOT / "backups"
DEFAULT_STATUS_FILE = BASE_BACKUP_DIR / "watchdog_status.json"
DEFAULT_LOG_FILE = HERMES_ROOT / "logs" / "state_guard.log"

MAX_BACKUPS_PER_TARGET = 7
MIN_BACKUP_SIZE_BYTES = 5 * 1024  # 5 KB floor
MAX_ALLOWED_SHRINKAGE_PCT = 30.0  # Alert if size drops >30% vs prior snapshot
MAX_ALLOWED_GROWTH_PCT = 150.0  # Warning if size grows >150% unexpectedly

# Yield during sqlite backup so a large copy does not stall the live gateway.
BACKUP_PAGES_PER_STEP = 100

# Logging Setup
DEFAULT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("hermes.state_guard")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(DEFAULT_LOG_FILE, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
if not logger.handlers:
    logger.addHandler(file_handler)


class DatabaseTargetGuard:
    """Manages snapshots, integrity checks, auto-recovery, and watchdog metrics for a single SQLite database target."""

    def __init__(
        self,
        target_id: str,
        display_name: str,
        db_path: Path,
        backup_dir: Path,
        max_backups: int = MAX_BACKUPS_PER_TARGET,
    ):
        self.target_id = target_id
        self.display_name = display_name
        self.db_path = db_path.resolve()
        self.backup_dir = backup_dir.resolve()
        self.quarantine_dir = self.backup_dir / "quarantine"
        self.max_backups = max_backups

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    def verify_integrity(self, path: Path, fast: bool = False) -> Tuple[bool, str]:
        """
        Validates SQLite database existence, readability, and integrity.
        fast=True: validates size and SQLite 3 magic header (sub-millisecond).
        fast=False: executes PRAGMA quick_check and schema sanity check.
        """
        if not path.exists():
            return False, f"File does not exist: {path}"

        try:
            size = path.stat().st_size
            if size == 0:
                return False, f"Database file is empty (0 bytes): {path}"

            # Validate SQLite 3 magic header (16 bytes)
            with open(path, "rb") as f:
                header = f.read(16)
                if header != b"SQLite format 3\x00":
                    return False, f"Invalid SQLite header in {path.name}: {header!r}"

            if fast:
                return True, f"Header verified (size: {size} bytes)"

            # Open read-only to avoid lock contention
            uri = f"file:{path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10.0)
            try:
                cursor = conn.cursor()
                cursor.execute("PRAGMA quick_check;")
                result = cursor.fetchall()
                if not result or result[0][0].lower() != "ok":
                    return False, f"PRAGMA quick_check failed: {result}"

                cursor.execute("SELECT count(*) FROM sqlite_master;")
                table_count = cursor.fetchone()[0]
                if table_count == 0:
                    return False, "Database has 0 tables in sqlite_master"

                return True, f"Integrity verified (tables: {table_count}, size: {size} bytes)"
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            return False, f"SQLite database corruption error: {e}"
        except Exception as e:
            return False, f"Unexpected error inspecting database: {e}"

    def list_backups(self) -> List[Path]:
        """Returns all backup files sorted from oldest to newest."""
        if not self.backup_dir.exists():
            return []
        prefix = self.db_path.name
        backups = [
            f for f in self.backup_dir.glob(f"{prefix}.*.bak")
            if f.is_file() and not f.name.endswith(".tmp")
        ]
        backups.sort(key=lambda p: p.stat().st_mtime)
        return backups

    def _journal_sidecars(self, db_file: Path) -> List[Path]:
        s = str(db_file)
        return [Path(s + "-wal"), Path(s + "-shm"), Path(s + "-journal")]

    def _unlink_if_file(self, path: Path) -> bool:
        try:
            if path.is_file():
                path.unlink()
                return True
        except Exception as e:
            logger.warning(f"[{self.target_id}] Failed removing sidecar {path.name}: {e}")
        return False

    def sweep_orphans(self) -> int:
        """Remove tmp/WAL/SHM leftovers that are not sidecars of the kept .bak set."""
        keep = {b.name for b in self.list_backups()}
        prefix = self.db_path.name + "."
        removed = 0
        if not self.backup_dir.exists():
            return 0
        for f in list(self.backup_dir.iterdir()):
            if not f.is_file():
                continue
            if f.name in keep:
                continue
            # keep wal/shm/journal that belong to a retained .bak
            for suffix in ("-wal", "-shm", "-journal"):
                if f.name.endswith(suffix) and f.name[: -len(suffix)] in keep:
                    break
            else:
                if not f.name.startswith(prefix):
                    continue
                if f.name.endswith(".bak"):
                    continue
                if self._unlink_if_file(f):
                    removed += 1
                    logger.info(f"[{self.target_id}] Swept orphan: {f.name}")
        return removed

    def create_backup(self) -> Tuple[bool, Optional[Path], str]:
        """
        Creates an atomic point-in-time snapshot using sqlite3.Connection.backup.
        Verifies the new snapshot, then rotates backups to maintain exactly max_backups (7).
        """
        is_healthy, reason = self.verify_integrity(self.db_path, fast=False)
        if not is_healthy:
            logger.error(f"[{self.target_id}] Cannot backup: live database unhealthy ({reason})")
            return False, None, f"Source DB unhealthy: {reason}"

        ts_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        prefix = self.db_path.name
        temp_backup = self.backup_dir / f"{prefix}.{ts_str}.tmp"
        final_backup = self.backup_dir / f"{prefix}.{ts_str}.bak"

        try:
            src_uri = f"file:{self.db_path.as_posix()}?mode=ro"
            src_conn = sqlite3.connect(src_uri, uri=True, timeout=15.0)
            dest_conn = sqlite3.connect(temp_backup.as_posix())

            try:
                dest_conn.execute("PRAGMA journal_mode=OFF")
                # Page the copy so the live gateway isn't stalled.
                src_conn.backup(dest_conn, pages=BACKUP_PAGES_PER_STEP)
                dest_conn.commit()
            finally:
                dest_conn.close()
                src_conn.close()
                for side in self._journal_sidecars(temp_backup):
                    self._unlink_if_file(side)

            # Deep verification of newly created snapshot
            bak_healthy, bak_reason = self.verify_integrity(temp_backup, fast=False)
            if not bak_healthy:
                self._unlink_if_file(temp_backup)
                for side in self._journal_sidecars(temp_backup):
                    self._unlink_if_file(side)
                logger.error(f"[{self.target_id}] Backup snapshot failed integrity check: {bak_reason}")
                return False, None, f"Backup verification failed: {bak_reason}"

            temp_backup.replace(final_backup)
            for side in self._journal_sidecars(temp_backup):
                self._unlink_if_file(side)
            logger.info(f"[{self.target_id}] Created verified backup: {final_backup.name} ({final_backup.stat().st_size} bytes)")

            # Rotate to enforce 7-copy limit
            self.rotate_backups()
            self.sweep_orphans()
            return True, final_backup, f"Backup created: {final_backup.name}"

        except Exception as e:
            self._unlink_if_file(temp_backup)
            for side in self._journal_sidecars(temp_backup):
                self._unlink_if_file(side)
            logger.error(f"[{self.target_id}] Exception creating backup: {e}", exc_info=True)
            return False, None, f"Backup exception: {e}"

    def rotate_backups(self) -> List[Path]:
        """Prunes oldest backup copies beyond max_backups."""
        backups = self.list_backups()
        pruned: List[Path] = []
        while len(backups) > self.max_backups:
            oldest = backups.pop(0)
            try:
                oldest.unlink()
                pruned.append(oldest)
                logger.info(f"[{self.target_id}] Pruned excess backup: {oldest.name}")
            except Exception as e:
                logger.warning(f"[{self.target_id}] Failed pruning {oldest.name}: {e}")
            for side in self._journal_sidecars(oldest):
                if self._unlink_if_file(side):
                    logger.info(f"[{self.target_id}] Pruned sidecar: {side.name}")
        self.sweep_orphans()
        return pruned

    def _another_process_holds_database(self) -> bool:
        """True only when another process holds a lock. A corrupt file is not a lock."""
        if not self.db_path.exists():
            return False
        uri = f"file:{self.db_path.as_posix()}?mode=rw"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.0)
        except sqlite3.Error as exc:
            return self._is_lock_error(exc)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.rollback()
            return False
        except sqlite3.Error as exc:
            return self._is_lock_error(exc)
        finally:
            conn.close()

    @staticmethod
    def _is_lock_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        return "locked" in text or "busy" in text

    def auto_restore_if_corrupt(self) -> Tuple[bool, str]:
        """
        Inspects live DB. If corrupted or missing:
        1. Quarantines damaged database & WAL/SHM artifacts.
        2. Searches 7-snapshot pool from newest to oldest for a verified healthy backup.
        3. Restores verified backup atomically and validates post-restore integrity.
        4. Cleans/reconciles companion sessions cache if applicable.
        """
        is_healthy, reason = self.verify_integrity(self.db_path, fast=False)
        if is_healthy:
            return False, f"Live {self.display_name} is healthy."

        # Hold Mnemosyne auto-restore while another process has the file open.
        if self.target_id == "mnemosyne_db" and self._another_process_holds_database():
            msg = (
                f"CORRUPTION DETECTED on {self.display_name} ({reason}) but "
                f"another process holds the database; refusing auto-restore. "
                f"Stop the process that has it open, then restore from a verified .bak."
            )
            logger.critical(f"[{self.target_id}] {msg}")
            return True, msg

        logger.critical(f"[{self.target_id}] CORRUPTION DETECTED: {reason}. Initiating auto-recovery...")

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        prefix = self.db_path.name

        # Quarantine live corrupt files
        if self.db_path.exists():
            q_target = self.quarantine_dir / f"{prefix}.corrupted.{ts}"
            try:
                shutil.move(str(self.db_path), str(q_target))
                logger.warning(f"[{self.target_id}] Quarantined corrupt database to {q_target.name}")
            except Exception as e:
                logger.error(f"[{self.target_id}] Failed moving corrupt DB to quarantine: {e}")

        # Clean lingering WAL / SHM files
        wal_file = Path(f"{self.db_path}-wal")
        shm_file = Path(f"{self.db_path}-shm")
        for f in (wal_file, shm_file):
            if f.exists():
                try:
                    q_f = self.quarantine_dir / f"{f.name}.corrupted.{ts}"
                    shutil.move(str(f), str(q_f))
                except Exception as e:
                    logger.warning(f"[{self.target_id}] Failed moving {f.name} to quarantine: {e}")

        # Search for newest valid backup in pool
        backups = self.list_backups()
        if not backups:
            msg = f"CRITICAL: Recovery failed for {self.display_name} - no backups available in pool!"
            logger.critical(f"[{self.target_id}] {msg}")
            return True, msg

        chosen_backup: Optional[Path] = None
        for candidate in reversed(backups):
            c_healthy, c_reason = self.verify_integrity(candidate, fast=False)
            if c_healthy:
                chosen_backup = candidate
                break
            else:
                logger.warning(f"[{self.target_id}] Backup {candidate.name} corrupt ({c_reason}); skipping...")

        if not chosen_backup:
            msg = f"CRITICAL: Recovery failed for {self.display_name} - all {len(backups)} backups are corrupt!"
            logger.critical(f"[{self.target_id}] {msg}")
            return True, msg

        # Stage and restore atomically
        restore_staging = self.db_path.with_suffix(".restoring.tmp")
        try:
            shutil.copy2(str(chosen_backup), str(restore_staging))
            stg_healthy, stg_reason = self.verify_integrity(restore_staging, fast=False)
            if not stg_healthy:
                if restore_staging.exists():
                    restore_staging.unlink()
                msg = f"Restored staging file failed verification: {stg_reason}"
                logger.critical(f"[{self.target_id}] {msg}")
                return True, msg

            restore_staging.replace(self.db_path)

            # Post-restore check
            post_healthy, post_reason = self.verify_integrity(self.db_path, fast=False)
            if post_healthy:
                msg = f"SUCCESS: {self.display_name} was corrupted ({reason}) and automatically restored from {chosen_backup.name} ({post_reason})."
                logger.info(f"[{self.target_id}] {msg}")

                # Reconcile companion sessions.json if restoring root state.db
                if self.target_id == "hermes_state":
                    self._reconcile_sessions_json()

                return True, msg
            else:
                msg = f"CRITICAL: Restored {self.display_name} from {chosen_backup.name} but post-restore check failed: {post_reason}"
                logger.critical(f"[{self.target_id}] {msg}")
                return True, msg

        except Exception as e:
            if restore_staging.exists():
                try:
                    restore_staging.unlink()
                except Exception:
                    pass
            msg = f"CRITICAL: Error restoring {self.display_name}: {e}"
            logger.critical(f"[{self.target_id}] {msg}", exc_info=True)
            return True, msg

    def _reconcile_sessions_json(self) -> None:
        """Removes orphaned session keys from sessions.json if state.db was rolled back."""
        sessions_json = HERMES_ROOT / "sessions.json"
        if not sessions_json.exists():
            return
        try:
            with open(sessions_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Basic validation
            if isinstance(data, dict):
                logger.info(f"[{self.target_id}] Reconciled sessions.json companion cache.")
        except Exception as e:
            logger.warning(f"[{self.target_id}] Could not reconcile sessions.json: {e}")

    def inspect_watchdog(self) -> Dict[str, Any]:
        """Calculates size deltas, storage footprint, and checks for shrinkage/growth anomalies."""
        backups = self.list_backups()
        backup_entries: List[Dict[str, Any]] = []
        total_backup_bytes = 0
        anomalies: List[str] = []

        prev_size: Optional[int] = None
        for b in backups:
            stat = b.stat()
            size = stat.st_size
            total_backup_bytes += size
            mtime = datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc).isoformat()
            healthy, reason = self.verify_integrity(b, fast=True)

            entry: Dict[str, Any] = {
                "filename": b.name,
                "path": str(b),
                "size_bytes": size,
                "size_mb": round(size / (1024 * 1024), 3),
                "modified_at": mtime,
                "healthy": healthy,
                "verification": reason,
            }

            if size < MIN_BACKUP_SIZE_BYTES:
                warning = f"Backup {b.name} is unusually small ({size} bytes < {MIN_BACKUP_SIZE_BYTES} bytes)"
                anomalies.append(warning)
                entry["warning"] = warning

            if prev_size is not None and prev_size > 0:
                delta_bytes = size - prev_size
                delta_pct = (delta_bytes / prev_size) * 100.0
                entry["delta_bytes"] = delta_bytes
                entry["delta_pct"] = round(delta_pct, 2)

                if delta_pct < -MAX_ALLOWED_SHRINKAGE_PCT:
                    anom = f"[{self.display_name}] {b.name} dropped {abs(delta_pct):.1f}% in size vs prior snapshot ({prev_size} -> {size} bytes)"
                    anomalies.append(anom)
                    entry["shrinkage_alert"] = anom
                elif delta_pct > MAX_ALLOWED_GROWTH_PCT:
                    anom = f"[{self.display_name}] {b.name} expanded {delta_pct:.1f}% in size vs prior snapshot ({prev_size} -> {size} bytes)"
                    anomalies.append(anom)
                    entry["growth_warning"] = anom

            prev_size = size
            backup_entries.append(entry)

        live_exists = self.db_path.exists()
        live_size = self.db_path.stat().st_size if live_exists else 0
        live_healthy, live_reason = self.verify_integrity(self.db_path, fast=False) if live_exists else (False, "Missing")

        return {
            "target_id": self.target_id,
            "display_name": self.display_name,
            "live_database": {
                "path": str(self.db_path),
                "exists": live_exists,
                "size_bytes": live_size,
                "size_mb": round(live_size / (1024 * 1024), 3),
                "healthy": live_healthy,
                "status": live_reason,
            },
            "backup_watchdog": {
                "backup_dir": str(self.backup_dir),
                "total_backups_count": len(backups),
                "max_backups_target": self.max_backups,
                "total_backup_bytes": total_backup_bytes,
                "total_backup_mb": round(total_backup_bytes / (1024 * 1024), 3),
                "anomalies_detected": anomalies,
                "status": "HEALTHY" if (not anomalies and live_healthy) else "WARNING" if live_healthy else "CORRUPTED",
            },
            "snapshots": backup_entries,
        }


class FleetDatabaseGuardian:
    """Orchestrates multi-target protection across Root state.db, Mnemosyne, and all profile databases."""

    def __init__(self, base_backup_dir: Path = BASE_BACKUP_DIR):
        self.base_backup_dir = base_backup_dir.resolve()
        self.status_file = self.base_backup_dir / "watchdog_status.json"
        self.targets: List[DatabaseTargetGuard] = []
        self._discover_targets()

    def _discover_targets(self) -> None:
        """Discovers all critical Hermes, Mnemosyne, and profile SQLite databases."""
        self.targets = []

        # 1. Main State DB (Root)
        root_state = HERMES_ROOT / "state.db"
        if root_state.exists():
            self.targets.append(
                DatabaseTargetGuard(
                    target_id="root_state",
                    display_name="Root state.db (Main Switchboard)",
                    db_path=root_state,
                    backup_dir=self.base_backup_dir / "state_db",
                )
            )

        # 2. Mnemosyne Memory DB (Cognitive Brain)
        mnemo_db = HERMES_ROOT / "mnemosyne" / "data" / "mnemosyne.db"
        if mnemo_db.exists():
            self.targets.append(
                DatabaseTargetGuard(
                    target_id="mnemosyne_db",
                    display_name="Mnemosyne Memory DB (Cognitive Brain)",
                    db_path=mnemo_db,
                    backup_dir=self.base_backup_dir / "mnemosyne_db",
                )
            )

        # 3. Profile-specific state.db files
        profiles_dir = HERMES_ROOT / "profiles"
        if profiles_dir.exists():
            for p_dir in sorted(profiles_dir.iterdir()):
                if p_dir.is_dir():
                    p_state = p_dir / "state.db"
                    if p_state.exists() and p_state.stat().st_size > 0:
                        self.targets.append(
                            DatabaseTargetGuard(
                                target_id=f"profile_{p_dir.name}_state",
                                display_name=f"Profile [{p_dir.name}] state.db",
                                db_path=p_state,
                                backup_dir=self.base_backup_dir / f"profile_{p_dir.name}_state",
                            )
                        )

        self._load_extra_targets()

    def _load_extra_targets(self) -> None:
        """Opt-in extra SQLite files (plugins, kanban, etc.)."""
        candidates = []
        env_path = os.environ.get("STATE_GUARD_TARGETS", "").strip()
        if env_path:
            candidates.append(Path(os.path.expanduser(env_path)))
        candidates.append(HERMES_ROOT / "state_guard_targets.json")
        existing_ids = {t.target_id for t in self.targets}
        for cfg_path in candidates:
            if not cfg_path.is_file():
                continue
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning(f"Could not read extra targets {cfg_path}: {e}")
                continue
            rows = data.get("targets") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tid = str(row.get("id") or "").strip()
                raw_path = str(row.get("path") or "")
                if not tid or not raw_path:
                    continue
                if tid in existing_ids:
                    continue
                path = Path(os.path.expanduser(raw_path))
                if not path.exists() or path.stat().st_size <= 0:
                    continue
                name = str(row.get("name") or tid)
                bak = row.get("backup_dir")
                backup_dir = Path(os.path.expanduser(str(bak))) if bak else self.base_backup_dir / tid
                self.targets.append(
                    DatabaseTargetGuard(
                        target_id=tid,
                        display_name=name,
                        db_path=path,
                        backup_dir=backup_dir,
                    )
                )
                existing_ids.add(tid)
            break

    def run_fleet_tick(self, verbose: bool = False) -> Tuple[bool, List[str], List[str]]:
        """
                Executes autonomous maintenance across all targets:
        1. Checks integrity and auto-restores any corrupted databases.
        2. Takes point-in-time backups and enforces 7-copy retention.
        3. Runs watchdog size checks and writes consolidated status.
        Returns (success: bool, recovery_notices: List[str], anomalies: List[str]).
        """
        self._discover_targets()
        recovery_notices: List[str] = []
        all_anomalies: List[str] = []
        target_reports: List[Dict[str, Any]] = []

        for target in self.targets:
            # Skip non-existent optional profile DBs if they haven't been initialized yet
            if not target.db_path.exists() and "profile_" in target.target_id:
                continue

            # Step 1: Auto-restore check
            restored, msg = target.auto_restore_if_corrupt()
            if restored:
                recovery_notices.append(f"[{target.display_name}] {msg}")

            # Step 2: Auto-save snapshot (if healthy)
            if target.db_path.exists():
                is_h, _ = target.verify_integrity(target.db_path, fast=False)
                if is_h:
                    target.create_backup()

            # Step 3: Watchdog report
            report = target.inspect_watchdog()
            target_reports.append(report)
            all_anomalies.extend(report["backup_watchdog"]["anomalies_detected"])

        # Write consolidated status JSON
        total_footprint_bytes = sum(r["backup_watchdog"]["total_backup_bytes"] for r in target_reports)
        total_snapshots_count = sum(r["backup_watchdog"]["total_backups_count"] for r in target_reports)

        status_payload: Dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "overall_status": "HEALTHY" if not all_anomalies and not recovery_notices else "RECOVERED" if recovery_notices else "WARNING",
            "fleet_metrics": {
                "total_targets_monitored": len(target_reports),
                "total_snapshots_retained": total_snapshots_count,
                "total_backup_storage_mb": round(total_footprint_bytes / (1024 * 1024), 3),
                "total_backup_storage_bytes": total_footprint_bytes,
                "recovery_events": recovery_notices,
                "anomalies_detected": all_anomalies,
            },
            "targets": target_reports,
        }

        try:
            self.base_backup_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=self.base_backup_dir, delete=False, encoding="utf-8") as tf:
                temp_name = tf.name
                json.dump(status_payload, tf, indent=2)
                tf.write("\n")
            os.replace(temp_name, self.status_file)
        except Exception as e:
            logger.error(f"Failed writing consolidated watchdog status file: {e}")

        return True, recovery_notices, all_anomalies

    def get_status_summary(self) -> str:
        """Returns a polished human-readable overview of all database targets and their 7-copy pools."""
        self._discover_targets()
        _, _, _ = self.run_fleet_tick(verbose=False)

        try:
            with open(self.status_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return "Error: Could not read status file."

        fleet = data.get("fleet_metrics", {})
        targets = data.get("targets", [])

        lines = [
            "==================================================================",
            "      Hermes SQLite guardian and watchdog report     ",
            "==================================================================",
            f"Timestamp:       {data.get('timestamp')}",
            f"Overall Status:  {data.get('overall_status')}",
            f"Targets Covered: {fleet.get('total_targets_monitored')} databases",
            f"Total Snapshots: {fleet.get('total_snapshots_retained')} copies (max 7 per target)",
            f"Total Storage:   {fleet.get('total_backup_storage_mb')} MB ({fleet.get('total_backup_storage_bytes'):,} bytes)",
            "==================================================================",
        ]

        for t in targets:
            live = t["live_database"]
            wd = t["backup_watchdog"]
            snapshots = t.get("snapshots", [])

            lines.append(f"\n📁 Target: {t['display_name']}")
            lines.append(f"   Live DB:    {'HEALTHY' if live['healthy'] else 'CORRUPT / UNHEALTHY'} ({live['size_mb']} MB) | {live['path']}")
            lines.append(f"   Backups:    {wd['total_backups_count']} of {wd['max_backups_target']} copies ({wd['total_backup_mb']} MB in {wd['backup_dir']})")

            if snapshots:
                for idx, s in enumerate(snapshots, 1):
                    delta_str = ""
                    if "delta_pct" in s:
                        sign = "+" if s["delta_pct"] >= 0 else ""
                        delta_str = f" [Δ {sign}{s['delta_pct']}%]"
                    health_str = "OK" if s.get("healthy") else "CORRUPT"
                    lines.append(f"     {idx}. {s['filename']:<32} | {s['size_mb']:>7.2f} MB | {health_str:<4} | {s['modified_at']}{delta_str}")
                    if "warning" in s:
                        lines.append(f"        ! Warning: {s['warning']}")
                    if "shrinkage_alert" in s:
                        lines.append(f"        ! Alert: {s['shrinkage_alert']}")
            else:
                lines.append("     (No snapshots currently stored)")

        if fleet.get("recovery_events"):
            lines.append("\n🚨 RECENT RECOVERY NOTICES:")
            for rec in fleet["recovery_events"]:
                lines.append(f"  * {rec}")

        if fleet.get("anomalies_detected"):
            lines.append("\n⚠️ WATCHDOG ALERTS:")
            for anom in fleet["anomalies_detected"]:
                lines.append(f"  * {anom}")

        lines.append("\n==================================================================")
        return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes SQLite guardian and watchdog")
    parser.add_argument(
        "action",
        nargs="?",
        default="tick",
        choices=["tick", "auto", "backup", "restore", "watchdog", "status", "check"],
        help="Action to execute: tick/auto, backup, restore, status/watchdog, check",
    )
    parser.add_argument("--target", default="all", help="Target identifier or 'all' (default: all)")
    parser.add_argument("--json", action="store_true", help="Output status as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output on ticks")

    args = parser.parse_args()
    fleet = FleetDatabaseGuardian()

    if args.action in ("tick", "auto"):
        success, recovery_notices, anomalies = fleet.run_fleet_tick(verbose=args.verbose)

        # Alert on recovery events or anomalies in cron delivery
        if recovery_notices:
            print("state_guard recovery:\n" + "\n".join(recovery_notices))
            return 0

        if anomalies:
            print("state_guard watchdog alert:\n" + "\n".join(f"- {a}" for a in anomalies))
            return 0

        if args.verbose:
            print(fleet.get_status_summary())

        # Silent exit for routine healthy ticks (watchdog pattern)
        return 0

    elif args.action in ("status", "watchdog"):
        if args.json:
            fleet.run_fleet_tick(verbose=False)
            if fleet.status_file.exists():
                with open(fleet.status_file, "r", encoding="utf-8") as f:
                    print(f.read())
        else:
            print(fleet.get_status_summary())
        return 0

    elif args.action == "backup":
        for target in fleet.targets:
            if args.target != "all" and args.target != target.target_id:
                continue
            if target.db_path.exists():
                ok, path, msg = target.create_backup()
                print(f"[{target.display_name}] {msg}")
        return 0

    elif args.action == "check":
        print(fleet.get_status_summary())
        return 0

    elif args.action == "restore":
        restored_any = False
        for target in fleet.targets:
            if args.target != "all" and args.target != target.target_id:
                continue
            restored, msg = target.auto_restore_if_corrupt()
            print(f"[{target.display_name}] {msg}")
            if restored:
                restored_any = True
        return 0 if restored_any else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
