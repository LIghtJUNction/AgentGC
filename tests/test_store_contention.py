from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agentgc.paths import Scope
from agentgc.store import Store
from agentgc.tracker import Tracker, track


class StoreContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)
        self.scope = Scope([self.root], self.store.home)
        self.sid = self.store.start_session("test", [self.root], "poll")
        self.tracker = Tracker(self.store, self.scope, self.sid)

    def test_initialized_store_opens_while_another_connection_holds_writer(self):
        # A short busy timeout makes the old unconditional meta insert fail
        # immediately, without waiting ten seconds in a regression test.
        original_connect = sqlite3.connect
        def connect(*args, **kwargs):
            kwargs["timeout"] = 0.001
            db = original_connect(*args, **kwargs)
            db.set_authorizer(lambda action, arg1, arg2, database, trigger:
                              sqlite3.SQLITE_IGNORE if action == sqlite3.SQLITE_PRAGMA
                              and arg1 == "busy_timeout" else sqlite3.SQLITE_OK)
            db.execute("PRAGMA busy_timeout=1")
            return db
        with self.store.transaction(), patch("agentgc.store.sqlite3.connect", side_effect=connect):
            other = Store(self.store.home)
            try:
                self.assertEqual(other.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
            finally:
                other.close()

    def test_walk_never_holds_write_lock_and_commits_bounded_batches(self):
        other = Store(self.store.home)
        self.addCleanup(other.close)
        other.db.execute("PRAGMA busy_timeout=1")
        path = self.root / "file"
        path.write_text("data")
        def walk(root, errors):
            for _ in range(300):
                self.assertFalse(self.store.db.in_transaction)
                with other.transaction():
                    other.db.execute("UPDATE sessions SET heartbeat=heartbeat WHERE id=?", (self.sid,))
                yield path
        sizes = []
        original = self.tracker.sync
        def sync(*args):
            sizes[-1] += 1
            return original(*args)
        @contextlib.contextmanager
        def transaction():
            sizes.append(0)
            with Store.transaction(self.store):
                yield
        with patch.object(self.scope, "walk", side_effect=walk), \
                patch.object(self.store, "transaction", side_effect=transaction), \
                patch.object(self.tracker, "sync", side_effect=sync):
            self.tracker.scan()
        self.assertEqual(sum(sizes), 300)
        self.assertLessEqual(max(sizes), 128)
        self.assertEqual(self.store.file(path)["activity"], 1)

    def test_missing_reconciliation_is_paged_and_preserves_keep(self):
        # Small isolated fixture crossing the page boundary, not a stress tree.
        paths = [self.root / f"file-{i:03}" for i in range(130)]
        for path in paths:
            path.write_text("data")
        self.tracker.scan("baseline")
        self.store.db.execute("UPDATE files SET label='keep'")
        for path in paths[:-1]:
            path.unlink()
        self.tracker.scan()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM files WHERE present=0").fetchone()[0], 129)
        self.assertEqual(self.store.file(paths[-1])["label"], "keep")
        self.assertEqual(self.store.file(paths[-1])["activity"], 1)

    def test_partial_walk_does_not_delete_unseen_files(self):
        path = self.root / "existing"
        path.write_text("data")
        self.tracker.scan("baseline")
        path.unlink()
        def walk(root, errors):
            errors.append("Unreadable directory")
            return iter(())
        with patch.object(self.scope, "walk", side_effect=walk), contextlib.redirect_stderr(io.StringIO()):
            self.tracker.scan()
        self.assertEqual(self.store.file(path)["present"], 1)
        self.assertEqual(self.store.db.execute("SELECT gaps FROM sessions WHERE id=?", (self.sid,)).fetchone()[0], 1)

    def test_tracking_database_error_leaves_gap_even_after_successful_final_scan(self):
        original = Tracker.scan
        calls = 0
        def scan(tracker, source="session-correlated"):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(tracker, source)
        with patch("agentgc.tracker.Tracker.scan", scan), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                track(self.store, self.scope, "failed", backend="poll", duration=0.1)
        row = self.store.db.execute("SELECT status,gaps FROM sessions WHERE agent='failed'").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["gaps"], 1)


if __name__ == "__main__":
    unittest.main()
