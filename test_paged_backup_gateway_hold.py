#!/usr/bin/env python3
"""
Stdlib tests for state_guard.py safety fixes.

  1. create_backup calls sqlite3 Connection.backup with pages=BACKUP_PAGES_PER_STEP (100).
  2. When another process holds mnemosyne.db, a corrupt file is left in place
     and the message contains 'refusing auto-restore'.
  3. When the file is free, or the error is not a lock, restore proceeds.

These tests never open /root/.hermes databases: HOME is redirected to a temp
directory before the module is imported.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Redirect HOME to a throwaway temp dir so module-level code
# (~/.hermes resolution, log file handler) never touches /root/.hermes.
_TMP_HOME = tempfile.mkdtemp(prefix="sg_test_home_")
os.environ["HOME"] = _TMP_HOME
os.environ.pop("HERMES_HOME", None)

# Import the module under test
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state_guard  # noqa: E402


def make_corrupt_db(path: Path):
    """Write a file that fails verify_integrity (bad SQLite magic header)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real sqlite database file content here")


class TestPagedBackup(unittest.TestCase):
    def test_backup_called_with_pages_100(self):
        """create_backup passes pages=BACKUP_PAGES_PER_STEP (100) to src_conn.backup."""
        tmpdir = Path(tempfile.mkdtemp(prefix="sg_paged_"))
        db_path = tmpdir / "test.db"
        backup_dir = tmpdir / "backups"

        # Create a small valid SQLite DB to snapshot
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        target = state_guard.DatabaseTargetGuard(
            target_id="test_target",
            display_name="Test DB",
            db_path=db_path,
            backup_dir=backup_dir,
        )

        calls = []
        original_backup = sqlite3.Connection.backup

        class SpyConnection(sqlite3.Connection):
            def backup(self, *args, **kwargs):
                calls.append(kwargs)
                return original_backup(self, *args, **kwargs)

        original_connect = sqlite3.connect

        def fake_connect(*args, **kwargs):
            kwargs.setdefault("factory", SpyConnection)
            return original_connect(*args, **kwargs)

        with patch.object(state_guard.sqlite3, "connect", fake_connect):
            ok, _, msg = target.create_backup()

        self.assertTrue(ok, msg)
        self.assertTrue(calls, "sqlite3 Connection.backup was never called")
        for kw in calls:
            self.assertIn("pages", kw, f"backup() called without pages kwarg: {kw}")
            self.assertEqual(kw["pages"], state_guard.BACKUP_PAGES_PER_STEP)
            self.assertEqual(kw["pages"], 100)


class TestLockHold(unittest.TestCase):
    def _make_target(self, tmpdir, target_id="mnemosyne_db"):
        db_path = tmpdir / "mnemosyne.db"
        target = state_guard.DatabaseTargetGuard(
            target_id=target_id,
            display_name="Mnemosyne Memory DB",
            db_path=db_path,
            backup_dir=tmpdir / "backups",
        )
        return db_path, target

    def _valid_db(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

    def test_hold_blocks_when_database_locked(self):
        """A live exclusive lock leaves the file in place and alerts."""
        tmpdir = Path(tempfile.mkdtemp(prefix="sg_hold_locked_"))
        db_path, target = self._make_target(tmpdir)
        self._valid_db(db_path)
        holder = sqlite3.connect(str(db_path))
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with patch.object(target, "verify_integrity", return_value=(False, "quick_check failed")):
                restored, msg = target.auto_restore_if_corrupt()
        finally:
            holder.rollback()
            holder.close()

        self.assertTrue(restored, "auto_restore_if_corrupt must return True for cron notice")
        self.assertIn("refusing auto-restore", msg)
        self.assertNotIn("systemctl", msg)
        self.assertTrue(db_path.exists(), "locked file must not be moved")

    def test_hold_does_not_block_when_database_free(self):
        """A free corrupt file is quarantined. No refusal message."""
        tmpdir = Path(tempfile.mkdtemp(prefix="sg_hold_free_"))
        db_path, target = self._make_target(tmpdir)
        make_corrupt_db(db_path)

        restored, msg = target.auto_restore_if_corrupt()

        self.assertFalse(db_path.exists(), "free corrupt file should be quarantined")
        self.assertNotIn("refusing auto-restore", msg)
        self.assertIsInstance(restored, bool)

    def test_not_a_database_is_not_a_lock(self):
        """'file is not a database' must not be treated as a held lock."""
        tmpdir = Path(tempfile.mkdtemp(prefix="sg_hold_nota_"))
        db_path, target = self._make_target(tmpdir)
        make_corrupt_db(db_path)

        held = target._another_process_holds_database()

        self.assertFalse(held)

    def test_other_target_restores_even_if_locked(self):
        """The lock hold applies only to mnemosyne_db."""
        tmpdir = Path(tempfile.mkdtemp(prefix="sg_hold_other_"))
        db_path, target = self._make_target(tmpdir, target_id="state_db")
        self._valid_db(db_path)
        holder = sqlite3.connect(str(db_path))
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with patch.object(target, "verify_integrity", return_value=(False, "quick_check failed")):
                restored, msg = target.auto_restore_if_corrupt()
        finally:
            holder.rollback()
            holder.close()

        self.assertNotIn("refusing auto-restore", msg)
        self.assertFalse(db_path.exists(), "a non-Mnemosyne target must still be quarantined")
        self.assertIsInstance(restored, bool)

    def test_script_does_not_call_systemctl(self):
        source = Path(state_guard.__file__).read_text()
        self.assertNotIn("systemctl", source)
        self.assertNotIn("subprocess", source)


if __name__ == "__main__":
    unittest.main()
