import tempfile
from pathlib import Path
import unittest

from agentgc.diagnostics import summarize
from agentgc.output import markdown
from agentgc.paths import SafetyError
from agentgc.store import Store


class DiagnosticRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'state'
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.sid = self.store.start_session('test', [Path(self.tmp.name)], 'poll')

    def gap(self, detail):
        self.store.event('', 'gap', 'tracker', self.sid, detail=detail)
        self.store.db.execute('UPDATE sessions SET gaps=gaps+1 WHERE id=?', (self.sid,))

    def test_compound_categories_overlap_without_inflating_gap_count(self):
        for _ in range(2):
            self.gap('Permission denied; Link or reparse point is not allowed: /x')
        self.gap('unknown failure')
        result = summarize(self.store, self.sid)
        self.assertEqual(result['retained_gap_events'], 3)
        self.assertEqual(result['missing_gap_details'], 0)
        self.assertTrue(result['gap_count_consistent'])
        self.assertEqual(result['categories'], [
            {'category': 'link-boundary', 'events': 2},
            {'category': 'permission', 'events': 2},
            {'category': 'tracking-failure', 'events': 1},
        ])
        text = markdown(result, 'diagnostics', row_limit=1)
        self.assertIn('overlapping counts', text)
        self.assertIn('tracking-failure', text)
        self.assertEqual(self.store.stats()['events'], 3)

    def test_removed_detail_does_not_hide_cumulative_gaps(self):
        self.gap('Permission denied')
        self.gap('Native event queue overflow')
        self.store.end_session(self.sid, 0)
        self.store.db.execute('DELETE FROM events WHERE session_id=?', (self.sid,))
        result = summarize(self.store, self.sid)
        self.assertEqual((result['gaps'], result['retained_gap_events'], result['missing_gap_details']), (2, 0, 2))
        self.assertEqual(result['categories'], [])
        self.assertIn('Missing history', markdown(result, 'diagnostics'))

    def test_inconsistent_counter_is_unknown_not_negative_missing_history(self):
        self.store.event('', 'gap', 'tracker', self.sid, detail='Permission denied')
        result = summarize(self.store, self.sid)
        self.assertFalse(result['gap_count_consistent'])
        self.assertIsNone(result['missing_gap_details'])
        self.assertIn('Inconsistent evidence', markdown(result, 'diagnostics'))

    def test_snapshot_remains_coherent_with_concurrent_writer(self):
        other = Store(self.home)
        self.addCleanup(other.close)
        inserted = False
        def concurrent_write(sql):
            nonlocal inserted
            if sql.startswith('SELECT detail') and not inserted:
                inserted = True
                with other.transaction():
                    other.event('', 'gap', 'tracker', self.sid, detail='Permission denied')
                    other.db.execute('UPDATE sessions SET gaps=gaps+1 WHERE id=?', (self.sid,))
        self.store.db.set_trace_callback(concurrent_write)
        try:
            result = summarize(self.store, self.sid)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertTrue(inserted)
        self.assertEqual((result['gaps'], result['retained_gap_events']), (0, 0))
        self.assertEqual(summarize(self.store, self.sid)['retained_gap_events'], 1)
        self.assertFalse(self.store.db.in_transaction)

    def test_read_only_summary_does_not_write_state(self):
        self.gap('Permission denied')
        reader = Store(self.home, read_only=True)
        self.addCleanup(reader.close)
        before = reader.db.total_changes
        self.assertEqual(summarize(reader, self.sid)['retained_gap_events'], 1)
        self.assertEqual(reader.db.total_changes, before)
        self.assertFalse(reader.db.in_transaction)

    def test_outer_transaction_and_missing_session_are_preserved(self):
        with self.store.transaction():
            self.gap('Permission denied')
            summarize(self.store, self.sid)
            self.assertTrue(self.store.db.in_transaction)
            with self.assertRaises(SafetyError):
                summarize(self.store, 'does-not-exist')
            self.assertTrue(self.store.db.in_transaction)
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(summarize(self.store, self.sid)['retained_gap_events'], 1)
