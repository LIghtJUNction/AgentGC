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

    def test_file_index_preview_matches_apply_and_preserves_protected_metadata(self):
        names = ['forget', 'recent', 'keep', 'temporary', 'note', 'reviewed', 'tagged',
                 'manual', 'orphan-session', 'journal', 'marked', 'recent-event', 'orphan-event']
        for name in names:
            (self.root / name).write_text('unchanged payload', encoding='utf-8')
        scan_once(self.store, Scope([self.root], self.store.home))
        self.store.db.execute('UPDATE files SET changed=0')
        self.store.db.execute('UPDATE events SET at=0')
        for name, column, value in [('recent', 'changed', time.time()), ('keep', 'label', 'keep'),
                                    ('temporary', 'label', 'temporary'), ('note', 'note', 'important'),
                                    ('reviewed', 'review_hash', 'hash'), ('tagged', 'tag_status', 'written'),
                                    ('manual', 'source', 'manual-observation'),
                                    ('orphan-session', 'session_id', 'unknown')]:
            self.store.db.execute('UPDATE files SET '+column+'=? WHERE path=?', (value, str(self.root / name)))
        fid = self.store.file(self.root / 'journal')['id']
        self.store.db.execute("INSERT INTO quarantine VALUES('q',?,'old','new','sig','hash','restored',0,NULL)", (fid,))
        for name, kind, session in [('marked', 'marked', None), ('recent-event', 'modified', None),
                                    ('orphan-event', 'modified', 'unknown')]:
            row = self.store.file(self.root / name)
            self.store.event(row['path'], kind, 'test', session, row['id'])
        self.store.db.execute("UPDATE events SET at=0 WHERE kind='marked'")
        preview, code = self.prune('--file-index', '--compact')
        self.assertEqual(code, 0)
        self.assertEqual(preview['file_index']['files'], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], len(names))
        result, code = self.prune('--file-index', '--compact', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual(result['file_index']['files'], preview['file_index']['files'])
        remaining = {Path(row[0]).name for row in self.store.db.execute('SELECT path FROM files')}
        self.assertEqual(remaining, set(names) - {'forget'})
        for name in names:
            self.assertEqual((self.root / name).read_text(encoding='utf-8'), 'unchanged payload')
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE kind='marked'").fetchone()[0], 1)

    def test_file_index_protects_active_ancestor_scope_and_unknown_roots(self):
        for name in ['active/file', 'sibling/file']:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('retain payload', encoding='utf-8')
        scan_once(self.store, Scope([self.root], self.store.home))
        self.store.db.execute('UPDATE files SET changed=0')
        self.store.db.execute('UPDATE events SET at=0')
        active = self.store.start_session('active', [self.root / 'active'], 'poll')
        result, code = self.prune('--file-index', '--apply')
        self.assertEqual(code, 0)
        import os
        self.assertEqual(result['file_index']['files'], 0 if os.name == 'nt' else 1)
        self.assertIsNotNone(self.store.file(self.root / 'active/file'))
        self.assertEqual(self.store.db.execute('SELECT status FROM sessions WHERE id=?', (active,)).fetchone()[0], 'running')
        self.store.db.execute("UPDATE sessions SET roots='invalid' WHERE id=?", (active,))
        result, code = self.prune('--file-index', '--apply')
        self.assertEqual(code, 0)
        self.assertTrue(result['file_index']['skipped'])

    def test_file_index_delete_failure_rolls_back_event_pruning(self):
        path = self.root / 'file'
        path.write_text('payload', encoding='utf-8')
        scan_once(self.store, Scope([self.root], self.store.home))
        self.store.db.execute('UPDATE files SET changed=0')
        self.store.db.execute('UPDATE events SET at=0')
        self.store.db.execute("CREATE TRIGGER reject_file_delete BEFORE DELETE ON files BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.prune('--file-index', '--apply')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.assertIsNotNone(self.store.file(path))

    def test_file_index_protects_path_only_manual_evidence_and_automatic_marks(self):
        for name in ['marked', 'automatic']:
            (self.root / name).write_text('payload', encoding='utf-8')
        scan_once(self.store, Scope([self.root], self.store.home))
        self.store.db.execute('UPDATE files SET changed=0')
        self.store.db.execute('UPDATE events SET at=0')
        self.store.event(str(self.root / 'marked'), 'marked', 'user-review')
        self.store.db.execute('UPDATE events SET at=0')
        self.store.db.execute('CREATE TABLE auto_marks(path TEXT)')
        self.store.db.execute('INSERT INTO auto_marks VALUES(?)', (str(self.root / 'automatic'),))
        result, code = self.prune('--file-index', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual(result['file_index']['files'], 0)
