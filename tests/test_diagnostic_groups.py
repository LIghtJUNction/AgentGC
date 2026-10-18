import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentgc.diagnostics import summarize
from agentgc.store import Store


class DiagnosticGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'work'
        self.root.mkdir()
        self.store = Store(self.base / 'state')
        self.addCleanup(self.store.close)
        self.sid = self.store.start_session('test', [self.root], 'poll')

    def gap(self, detail, path=''):
        self.store.event(str(path), 'gap', 'tracker', self.sid, detail=detail)
        self.store.db.execute('UPDATE sessions SET gaps=gaps+1 WHERE id=?', (self.sid,))

    def notice(self, kind, path, detail=''):
        self.store.event(str(path), kind, 'filesystem-event', self.sid, detail=detail)

    def compact(self):
        return summarize(self.store, self.sid)['compact_groups']

    def test_separates_boundaries_metadata_coverage_and_unknown(self):
        self.gap('Link or reparse point is not allowed: /unknown/link')
        self.gap('Incomplete scan: [Errno 2] No such file or directory: /unknown/temp')
        self.gap('Native event queue overflow; reconciling surviving files')
        self.gap('Final native event read failed: bad descriptor')
        self.gap('Incomplete scan: [Errno 13] Permission denied')
        self.gap('Opaque tracking problem')
        result = summarize(self.store, self.sid)
        groups = {group['classification']: group for group in result['compact_groups']}
        self.assertEqual(set(groups), {
            'expected-boundary', 'metadata-unavailable', 'coverage-loss', 'unknown',
        })
        self.assertEqual(groups['expected-boundary']['severity'], 'info')
        self.assertEqual(groups['metadata-unavailable']['severity'], 'warning')
        self.assertEqual(groups['coverage-loss']['severity'], 'error')
        self.assertEqual(groups['coverage-loss']['gap_events'], 3)
        self.assertEqual(groups['unknown']['severity'], 'error')
        self.assertEqual(result['retained_gap_events'], 6)
        self.assertEqual(len(result['groups']), 6)
        for group in groups.values():
            self.assertEqual(group['evidence_source'], 'text-derived')
            self.assertEqual(group['path_source'], 'unattributed')
            self.assertIsNone(group['root'])
            self.assertIsNone(group['prefix'])
            self.assertIsNone(group['paths'])
            self.assertTrue(group['impact'])

    def test_failed_spawn_and_listener_enoent_are_not_short_lived_metadata(self):
        self.gap('Tracking interrupted by FileNotFoundError; intermediate history may be incomplete')
        self.gap('Final native event read failed: [Errno 2] No such file or directory')
        groups = self.compact()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['classification'], 'coverage-loss')
        self.assertEqual(groups[0]['gap_events'], 2)

    def test_compound_record_keeps_all_hints_without_inflating_raw_count(self):
        detail = ('Incomplete event read: Permission denied; '
                  'Link or reparse point is not allowed: /a; No such file: /b')
        self.gap(detail)
        result = summarize(self.store, self.sid)
        self.assertEqual(result['retained_gap_events'], 1)
        self.assertEqual(result['groups'][0]['detail'], detail)
        self.assertEqual(len(result['compact_groups']), 3)
        self.assertTrue(all(group['gap_events'] == 1 for group in result['compact_groups']))
        self.assertIn('overlap', result['compact_note'])

    def test_unregistered_native_directory_is_coverage_loss_even_with_enoent(self):
        detail = ('Partial native coverage; affected directories will be retried: '
                  '[Errno 2] No such file or directory: /unknown/build')
        self.gap(detail)
        groups = self.compact()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['classification'], 'coverage-loss')
        self.assertEqual(groups[0]['severity'], 'error')
        self.assertEqual(groups[0]['gap_events'], 1)
        self.assertEqual(summarize(self.store, self.sid)['groups'][0]['detail'], detail)

    def test_unresolved_counts_events_and_distinct_paths_across_kinds(self):
        a, b = self.root / 'build' / 'a.o', self.root / 'build' / 'b.o'
        for kind, path in (
            ('created-unresolved', a), ('created-unresolved', a),
            ('modified-unresolved', a), ('created-unresolved', b),
            ('deleted-unresolved', a), ('deleted-unresolved', a),
        ):
            self.notice(kind, path)
        result = summarize(self.store, self.sid)
        groups = {group['classification']: group for group in result['compact_groups']}
        missing, removed = groups['metadata-unavailable'], groups['removal-notice']
        self.assertEqual((missing['unresolved_events'], missing['paths']), (4, 2))
        self.assertEqual(missing['kinds'], ['created-unresolved', 'modified-unresolved'])
        self.assertEqual((removed['unresolved_events'], removed['paths']), (2, 1))
        self.assertEqual(removed['severity'], 'info')
        self.assertIn('normal removal', removed['impact'])
        self.assertEqual((result['retained_unresolved_events'], result['unresolved_paths']), (6, 2))
        self.assertEqual(missing['prefix'], 'build')
        self.assertEqual(missing['root'], str(self.root))

    def test_deleted_unresolved_alone_does_not_claim_metadata_loss(self):
        self.notice('deleted-unresolved', self.root / 'removed')
        group, = self.compact()
        self.assertEqual(group['classification'], 'removal-notice')
        self.assertEqual(group['gap_events'], 0)
        self.assertEqual(group['notice_events'], 0)
        self.assertEqual(group['unresolved_events'], 1)
        self.assertIn('not a lost-file count', group['impact'])

    def test_unknown_unresolved_kind_stays_unknown(self):
        self.notice('future-unresolved', self.root / 'file')
        group, = self.compact()
        self.assertEqual((group['classification'], group['severity']), ('unknown', 'error'))

    def test_typed_notices_group_real_paths_without_incrementing_gaps(self):
        for path in ('a', 'a', 'nested/b'):
            self.notice('link-boundary-notice', self.root / 'dkms' / path)
        self.notice('coverage-error-notice', self.root / 'private' / 'file')
        self.gap('Link or reparse point is not allowed: /unattributed')
        result = summarize(self.store, self.sid)
        typed = [group for group in result['compact_groups']
                 if group['evidence_source'] == 'event-kind']
        self.assertEqual(len(typed), 2)
        boundary = next(group for group in typed if group['classification'] == 'expected-boundary')
        self.assertEqual((boundary['prefix'], boundary['notice_events'], boundary['paths']), ('dkms', 3, 2))
        self.assertEqual((boundary['gap_events'], boundary['unresolved_events']), (0, 0))
        self.assertEqual(boundary['path_source'], 'event-path')
        self.assertEqual(result['retained_gap_events'], 1)
        self.assertEqual(result['retained_unresolved_events'], 0)
        self.assertIn('typed notices may share a cause with gaps', result['compact_note'])

    def test_spaces_semicolons_and_controls_remain_in_stored_event_prefix(self):
        prefix = 'space name;line\nbidi\u202e'
        self.notice('created-unresolved', self.root / prefix / 'nested' / 'a')
        self.notice('modified-unresolved', self.root / prefix / 'b')
        group, = self.compact()
        self.assertEqual(group['prefix'], prefix)
        self.assertEqual((group['paths'], group['unresolved_events']), (2, 2))
        self.assertEqual(json.loads(json.dumps(group))['prefix'], prefix)

    def test_legacy_text_never_invents_paths_from_deceptive_filename_text(self):
        detail = (f'Incomplete event read: {self.root}/space name; Permission denied\n'
                  f'{self.root}/other: Link or reparse point is not allowed: /elsewhere')
        self.gap(detail)
        result = summarize(self.store, self.sid)
        for group in result['compact_groups']:
            self.assertIsNone(group['root'])
            self.assertIsNone(group['prefix'])
            self.assertIsNone(group['paths'])
            self.assertEqual(group['evidence_source'], 'text-derived')
        self.assertEqual(result['groups'][0]['detail'], detail)

    def test_path_membership_is_lexical_and_respects_directory_boundaries(self):
        self.notice('created-unresolved', self.root / 'link' / 'file')
        self.notice('created-unresolved', self.root / 'root-file')
        self.notice('created-unresolved', self.base / 'work-old' / 'file')
        self.notice('created-unresolved', str(self.root) + '/inside/../outside')
        self.notice('created-unresolved', 'relative/file')
        # Summaries must still work when every attempted filesystem inspection
        # or alias resolution would fail; only stored strings are relevant.
        with patch('pathlib.Path.resolve', side_effect=AssertionError('resolved a path')), \
                patch('os.stat', side_effect=AssertionError('statted a path')), \
                patch('os.scandir', side_effect=AssertionError('scanned a path')):
            groups = self.compact()
        scoped = {group['prefix']: group for group in groups if group['root'] is not None}
        self.assertEqual(set(scoped), {'.', 'link'})
        outside, = [group for group in groups if group['root'] is None]
        self.assertEqual((outside['paths'], outside['unresolved_events']), (3, 3))
        self.assertEqual(outside['path_source'], 'event-path')
        self.assertIsNone(outside['prefix'])

    def test_nonabsolute_roots_do_not_create_path_attribution(self):
        self.notice('created-unresolved', self.root / 'build' / 'file')
        for roots in ('[]', '["relative"]', json.dumps([str(self.root) + '/../elsewhere'])):
            with self.subTest(roots=roots):
                self.store.db.execute('UPDATE sessions SET roots=? WHERE id=?', (roots, self.sid))
                group, = self.compact()
                self.assertIsNone(group['root'])
                self.assertIsNone(group['prefix'])
                self.assertEqual(group['paths'], 1)

    def test_standard_session_fields_keep_command_success_separate_from_partial_tracking(self):
        self.sid = self.store.start_session('test', [self.root], 'poll', command='python')
        self.store.command_started(self.sid)
        self.store.command_finished(self.sid, 0)
        self.gap('Native event queue overflow')
        self.notice('created-unresolved', self.root / 'temporary')
        self.store.tracking_finished(self.sid, 'partial')
        self.store.end_session(self.sid, 0, 'failed')
        result = summarize(self.store, self.sid)
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['session'], self.sid)
        self.assertEqual(result['roots'], [str(self.root)])
        self.assertEqual(result['command_status'], 'succeeded')
        self.assertEqual(result['command_exit_code'], 0)
        self.assertEqual(result['tracking_status'], 'partial')
        self.assertEqual(result['session_status'], result['status'])
        self.assertEqual(result['gap_count'], result['gaps'])
        self.assertEqual(result['unresolved_event_count'], result['retained_unresolved_events'])
        self.assertEqual(result['count_scope'], {
            'gap_count': 'session-cumulative', 'unresolved_event_count': 'retained-events',
        })

    def test_standard_counts_share_diagnostic_snapshot_with_concurrent_writer(self):
        other = Store(self.store.home)
        self.addCleanup(other.close)
        inserted = False

        def concurrent_write(sql):
            nonlocal inserted
            if sql.startswith('SELECT detail') and not inserted:
                inserted = True
                with other.transaction():
                    other.event('', 'gap', 'tracker', self.sid, detail='Native event queue overflow')
                    other.event(str(self.root / 'temp'), 'created-unresolved', 'filesystem-event', self.sid)
                    other.db.execute('UPDATE sessions SET gaps=gaps+1 WHERE id=?', (self.sid,))

        self.store.db.set_trace_callback(concurrent_write)
        try:
            result = summarize(self.store, self.sid)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertTrue(inserted)
        self.assertEqual((result['gap_count'], result['gaps'], result['retained_gap_events']), (0, 0, 0))
        self.assertEqual((result['unresolved_event_count'], result['retained_unresolved_events']), (0, 0))
        self.assertEqual(result['compact_groups'], [])
        latest = summarize(self.store, self.sid)
        self.assertEqual((latest['gap_count'], latest['unresolved_event_count']), (1, 1))

    def test_missing_and_inconsistent_history_remain_visible(self):
        self.gap('Link or reparse point is not allowed: /x')
        self.store.db.execute('DELETE FROM events WHERE session_id=?', (self.sid,))
        result = summarize(self.store, self.sid)
        self.assertEqual(result['compact_groups'], [])
        self.assertEqual(result['missing_gap_details'], 1)
        self.assertTrue(result['gap_count_consistent'])
        self.store.event('', 'gap', 'tracker', self.sid, detail='Link or reparse point is not allowed: /x')
        self.store.event('', 'gap', 'tracker', self.sid, detail='Link or reparse point is not allowed: /y')
        result = summarize(self.store, self.sid)
        self.assertFalse(result['gap_count_consistent'])
        self.assertIsNone(result['missing_gap_details'])
        self.assertEqual((result['gaps'], result['retained_gap_events']), (1, 2))
        self.assertIn('inconsistent history remains unknown', result['compact_note'])
