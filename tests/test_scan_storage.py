import tempfile
import unittest
from pathlib import Path

from agentgc.paths import Scope
from agentgc.store import Store
from agentgc.tracker import scan_once


class ScanStorageTests(unittest.TestCase):
    def test_unchanged_scan_does_not_rewrite_persistent_file_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            root = base / 'work'
            root.mkdir()
            path = root / 'file'
            path.write_text('payload is never stored', encoding='utf-8')
            store = Store(base / 'state')
            try:
                scope = Scope([root], store.home)
                scan_once(store, scope)
                before = dict(store.file(path))
                statements = []
                store.db.set_trace_callback(statements.append)
                scan_once(store, scope)
                self.assertEqual(dict(store.file(path)), before)
                self.assertFalse(any(sql.startswith('UPDATE files') for sql in statements))
                self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
                path.unlink()
                scan_once(store, scope)
                self.assertEqual(store.file(path)['present'], 0)
                self.assertEqual(store.db.execute("SELECT count(*) FROM events WHERE kind='deleted'").fetchone()[0], 1)
            finally:
                store.close()

    def test_generated_trees_are_opt_in_and_existing_records_are_not_marked_deleted(self):
        from agentgc.paths import GENERATED_EXCLUDES
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            root = base / 'work'
            root.mkdir()
            for name in GENERATED_EXCLUDES:
                (root / name).mkdir()
                (root / name / 'intermediate').write_text('generated', encoding='utf-8')
            (root / 'source.py').write_text('source', encoding='utf-8')
            (root / 'dist').mkdir()
            (root / 'dist' / 'deliverable').write_text('keep visible', encoding='utf-8')
            store = Store(base / 'state')
            try:
                scan_once(store, Scope([root], store.home))
                self.assertEqual(store.db.execute('SELECT count(*) FROM files').fetchone()[0], 2)
                scan_once(store, Scope([root], store.home, include_generated=True))
                self.assertEqual(store.db.execute('SELECT count(*) FROM files').fetchone()[0], 2 + len(GENERATED_EXCLUDES))
                for name in GENERATED_EXCLUDES:
                    (root / name / 'intermediate').unlink()
                from agentgc.limits import TrackingLimits
                # Old excluded rows cannot consume the new narrow scan budget.
                scan_once(store, Scope([root], store.home), TrackingLimits(max_files=2))
                for name in GENERATED_EXCLUDES:
                    self.assertEqual(store.file(root / name / 'intermediate')['present'], 1)
                # An explicitly selected generated root is an intentional scope.
                scan_once(store, Scope([root / 'target'], store.home))
                self.assertEqual(store.file(root / 'target' / 'intermediate')['present'], 0)
            finally:
                store.close()
