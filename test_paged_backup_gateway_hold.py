#!/usr/bin/env python3
"""
Stdlib tests for state_guard.py safety fixes.

  1. create_backup calls sqlite3 Connection.backup with pages=BACKUP_PAGES_PER_STEP (100).
  2. When the systemctl gateway probe returns 0, a corrupt mnemosyne_db file
     is not moved and the message contains 'refusing auto-restore'.
  3. When the probe returns non-zero, the hold does not block (file is quarantined).

These tests never open /root/.hermes databases: HOME is redirected to a temp
directory before the module is imported.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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


class TestGatewayHold(unittest.TestCase):
    def _make_target(self, tmpdir):
        db_path = tmpdir / "mnemosyne.db"
        target = state_guard.DatabaseTargetGuard(
            target_id="mnemosyne_db",
            display_name="Mnemosyne Memory DB",
            db_path=db_path,
            backup_dir=tmpdir / "backups",
        )
        return db_path, target

    def test_hold_blocks_when_gateway_active(self):
        """Probe returncode 0: file not moved, message contains 'refusing auto-restore'."""
        tmpdir = Path(tempfile.mkdtemp(prefix="sg_hold_active_"))
        db_path, target = self._make_target(tmpdir)
        make_corrupt_db(db_path)

        fake_proc = MagicMock(returncode=0)
        with patch.object(state_guard.subprocess, "run", return_value=fake_proc) as mock_run:
            restored, msg = target.auto_restore_if_corrupt()

        self.assertTrue(mock_run.called, "systemctl probe should have been invoked")
        self.assertTrue(restored, "auto_restore_if_corrupt must return True for cron notice")
        self.assertIn("refusing auto-restore", msg)
        self.assertTrue(db_path.exists(), "corrupt file must not be moved while gateway is up")

    def test_hold_does_not_block_when_gateway_inactive(self):
        """Probe non-zero: hold does not block, file is quarantined, no refusal message."""
        tmpdir = Path(tempfile.mkdtemp(prefix="sg_hold_inactive_"))
        db_path, target = self._make_target(tmpdir)
        make_corrupt_db(db_path)

        fake_proc = MagicMock(returncode=3)
        with patch.object(state_guard.subprocess, "run", return_value=fake_proc) as mock_run:
            restored, msg = target.auto_restore_if_corrupt()

        self.assertTrue(mock_run.called, "systemctl probe should have been invoked")
        self.assertFalse(db_path.exists(), "corrupt file should be quarantined when gateway is down")
        self.assertNotIn("refusing auto-restore", msg)


if __name__ == "__main__":
    unittest.main()
