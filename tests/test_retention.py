import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agentgc.cli import execute, parser
from agentgc.paths import Scope
from agentgc.store import Store
from agentgc.tracker import scan_once


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "work"
        self.root.mkdir()
        self.store = Store(self.root.parent / "state")
        self.addCleanup(self.store.close)
        self.store.db.execute("PRAGMA busy_timeout=0")

    def prune(self, *args):
        return execute(parser().parse_args(["prune", "--days", "1", *args]), self.store)

    def test_prune_preserves_recent_orphan_and_running_history(self):
        active = self.store.start_session("active", [self.root], "poll")
        ended = self.store.start_session("ended", [self.root], "poll")
        self.store.end_session(ended, 0)
        self.store.db.execute("DELETE FROM events")
        cases = [("old", ended, 0), ("unattributed", None, 0),
                 ("active", active, 0), ("orphan", "unknown-session", 0),
                 ("recent", ended, time.time())]
        self.store.db.executemany(
            "INSERT INTO events(path,session_id,at,kind,source) VALUES(?,?,?,'observed','test')", cases)
        preview, code = self.prune("--compact")
        self.assertEqual(code, 0)
        self.assertEqual(preview["events"], 2)
        self.assertEqual(preview["compaction"]["status"], "preview")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 5)
        result, code = self.prune("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(result["events"], 2)
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT path FROM events ORDER BY id")],
                         ["active", "orphan", "recent"])
        self.assertEqual(self.store.db.execute("SELECT status FROM sessions WHERE id=?", (active,)).fetchone()[0], "running")

    def test_preview_does_not_block_an_existing_writer(self):
        writer = Store(self.store.home)
        self.addCleanup(writer.close)
        with writer.transaction():
            result, code = self.prune("--compact")
        self.assertEqual(code, 0)
        self.assertEqual(result["events"], 0)
        self.assertEqual(result["compaction"]["status"], "preview")

    def test_failed_delete_rolls_back_history(self):
        self.store.event("old", "observed", "test")
        self.store.db.execute("UPDATE events SET at=0")
        self.store.db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.prune("--apply")
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.db.execute("SELECT path FROM events").fetchone()[0], "old")

    def test_compaction_reclaims_bytes_and_keeps_index_labels_and_journal(self):
        (self.root / "result.txt").write_text("deliverable", encoding="utf-8")
        scan_once(self.store, Scope([self.root], self.store.home))
        self.store.db.execute("UPDATE files SET label='keep',note='retain this result'")
        self.store.db.execute("INSERT INTO quarantine VALUES('q',1,'old','new','sig','hash','prepared',0,NULL)")
        before = {table: [tuple(r) for r in self.store.db.execute("SELECT * FROM " + table)]
                  for table in ("files", "sessions", "session_results", "quarantine", "meta")}
        with self.store.transaction():
            self.store.db.executemany(
                "INSERT INTO events(path,at,kind,source,detail) VALUES('old',0,'observed','test',?)",
                [("x" * 8192,)] * 256)
        self.store.db.execute("UPDATE events SET at=0")
        result, code = self.prune("--apply", "--compact")
        self.assertEqual(code, 0)
        self.assertEqual(result["events"], 257)
        compaction = result["compaction"]
        self.assertEqual(compaction["status"], "complete")
        self.assertGreater(compaction["before_bytes"], compaction["after_bytes"])
        self.assertGreater(compaction["reclaimed_bytes"], 1024 * 1024)
        for table, rows in before.items():
            self.assertEqual([tuple(r) for r in self.store.db.execute("SELECT * FROM " + table)], rows)
        self.assertEqual(self.store.db.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_busy_compaction_reports_pruning_separately_and_can_retry(self):
        self.store.event("old", "observed", "test")
        self.store.db.execute("UPDATE events SET at=0")
        reader = Store(self.store.home)
        self.addCleanup(reader.close)
        reader.db.execute("BEGIN")
        reader.db.execute("SELECT * FROM events").fetchall()
        try:
            result, code = self.prune("--apply", "--compact")
            self.assertEqual(code, 1)
            self.assertEqual(result["events"], 1)
            self.assertEqual(result["compaction"]["status"], "failed")
            self.assertIn("busy", result["compaction"]["error"].lower())
        finally:
            reader.db.execute("ROLLBACK")
        result, code = self.prune("--apply", "--compact")
        self.assertEqual(code, 0)
        self.assertEqual(result["events"], 0)
        self.assertEqual(result["compaction"]["status"], "complete")
