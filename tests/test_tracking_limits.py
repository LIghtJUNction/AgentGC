from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentgc.cli import main
from agentgc.limits import MIB, TrackingLimit, TrackingLimits, check_state_location, memory_filesystem
from agentgc.paths import SafetyError, Scope
from agentgc.store import Store
from agentgc.tracker import scan_once, track
from agentgc.watch_lifecycle import _reap_completed, start_watch
from process_helpers import stop_process


class TrackingLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'work'
        self.root.mkdir()
        self.home = self.base / 'state'

    def store(self):
        store = Store(self.home)
        self.addCleanup(store.close)
        return store

    def files(self, count, root=None):
        root = root or self.root
        for index in range(count):
            (root / f'file-{index}').write_text('fixture', encoding='utf-8')

    def invoke(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--home', str(self.home), '--json', '--no-project-rules', *map(str, arguments)])
        records = [json.loads(line) for line in (out.getvalue() + err.getvalue()).splitlines()
                   if line.startswith(('{', '['))]
        self.assertEqual(len(records), 1)
        return code, records[0]

    def test_memory_state_is_rejected_before_creating_a_store_or_launcher(self):
        for action in ('scan', 'run', 'watch', 'watch-start'):
            with self.subTest(action=action), patch('agentgc.limits.memory_filesystem', return_value=True), \
                    patch('agentgc.cli.Store') as store, patch('agentgc.cli.start_watch') as launch:
                code, result = self.invoke(action, '--root', self.root)
                self.assertEqual(code, 2)
                self.assertIn('tmpfs/ramfs', result['error'])
                store.assert_not_called()
                launch.assert_not_called()
                self.assertFalse(self.home.exists())

    def test_explicit_memory_state_opt_in_still_enforces_file_budget(self):
        self.files(2)
        with patch('agentgc.limits.memory_filesystem', return_value=True):
            code, result = self.invoke('scan', '--root', self.root, '--allow-memory-state', '--max-files', '1')
        self.assertEqual(code, 2)
        self.assertIn('--max-files 1', result['error'])
        self.assertEqual(result['session_status'], 'failed')
        self.assertEqual(result['tracking_status'], 'failed')
        self.assertEqual(result['gap_count'], 1)

    def test_baseline_limit_never_starts_child_and_is_not_retried_on_shutdown(self):
        self.files(3)
        store = self.store()
        scope = Scope([self.root], self.home)
        with patch('agentgc.tracker.subprocess.Popen') as child, \
                patch.object(scope, 'walk', wraps=scope.walk) as walk, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(TrackingLimit) as caught:
                track(store, scope, 'test', backend='poll', command=['must-not-run'],
                      limits=TrackingLimits(max_files=2))
        child.assert_not_called()
        self.assertEqual(walk.call_count, 1)
        sid = caught.exception.agentgc_session
        row = store.db.execute('SELECT status,gaps FROM sessions WHERE id=?', (sid,)).fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['gaps'], 1)
        result = store.db.execute('SELECT * FROM session_results WHERE session_id=?', (sid,)).fetchone()
        self.assertEqual(result['command_status'], 'not-started')
        self.assertIsNone(result['command_exit_code'])
        self.assertEqual(result['tracking_status'], 'failed')
        detail = store.db.execute("SELECT detail FROM events WHERE kind='gap'").fetchone()[0]
        self.assertIn('--max-files 2', detail)

    def test_limit_preserves_unvisited_indexed_files_and_manual_labels(self):
        self.files(3)
        store = self.store()
        scope = Scope([self.root], self.home)
        scan_once(store, scope)
        store.db.execute("UPDATE files SET label='keep'")
        missing = self.root / 'file-0'
        missing.unlink()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(TrackingLimit):
            scan_once(store, scope, TrackingLimits(max_files=1))
        self.assertEqual(store.file(missing)['present'], 1)
        self.assertEqual(store.file(missing)['label'], 'keep')
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE kind='deleted'").fetchone()[0], 0)

    def test_budget_is_shared_across_repeated_roots(self):
        other = self.base / 'other'
        other.mkdir()
        self.files(2)
        self.files(2, other)
        store = self.store()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(TrackingLimit):
            scan_once(store, Scope([self.root, other], self.home), TrackingLimits(max_files=3))

    def test_large_baseline_retains_only_bounded_committed_batches(self):
        self.files(130)
        store = self.store()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(TrackingLimit):
            scan_once(store, Scope([self.root], self.home), TrackingLimits(max_files=128))
        self.assertEqual(store.stats()['files'], 128)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE kind='observed'").fetchone()[0], 128)

    def test_same_store_reuses_baseline_and_explicit_larger_budget_works(self):
        self.files(3)
        code, result = self.invoke('scan', '--root', self.root, '--max-files', '2')
        self.assertEqual(code, 2)
        for _ in range(3):
            code, result = self.invoke('scan', '--root', self.root, '--max-files', '3')
            self.assertEqual(code, 0, result)
        store = self.store()
        self.assertEqual(store.stats()['files'], 3)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE kind='observed'").fetchone()[0], 3)

    def test_state_budget_counts_database_and_all_sidecars_without_opening_sqlite(self):
        self.home.mkdir()
        for suffix in ('', '-wal', '-shm', '-journal'):
            (self.home / ('index.sqlite3' + suffix)).write_bytes(b'x' * 16)
        TrackingLimits(max_state_mib=65 / MIB).check_state(self.home)
        with self.assertRaises(TrackingLimit):
            TrackingLimits(max_state_mib=64 / MIB).check_state(self.home)

    def test_oversized_state_refuses_tracking_but_remains_queryable(self):
        self.files(1)
        store = self.store()
        scan_once(store, Scope([self.root], self.home))
        count = store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        code, result = self.invoke('scan', '--root', self.root, '--max-state-mib', '0.001')
        self.assertEqual(code, 2)
        self.assertIn('state budget reached', result['error'])
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], count)
        code, result = self.invoke('list', '--root', self.root)
        self.assertEqual(code, 0)
        self.assertTrue(result)

    def test_state_growth_during_tracking_stops_with_a_visible_gap(self):
        self.files(1)
        store = self.store()
        original = TrackingLimits.check_state
        calls = 0

        def grows(limits, home):
            nonlocal calls
            calls += 1
            if calls >= 5:
                raise TrackingLimit('Tracking state budget reached during reconciliation')
            return original(limits, home)

        with patch.object(TrackingLimits, 'check_state', grows), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(TrackingLimit):
                track(store, Scope([self.root], self.home), 'test', backend='poll',
                      interval=0.1, reconcile=0.1, duration=1)
        row = store.db.execute('SELECT status,gaps FROM sessions').fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertGreater(row['gaps'], 0)
        result = store.db.execute('SELECT tracking_status FROM session_results').fetchone()[0]
        self.assertEqual(result, 'partial')

    def test_linked_sidecar_is_not_followed(self):
        self.home.mkdir()
        link = self.home / 'index.sqlite3-wal'
        try:
            link.symlink_to(self.root / 'missing')
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable')
        with self.assertRaises(SafetyError):
            TrackingLimits().check_state(self.home)

    def test_linked_home_is_rejected_before_inspecting_its_database(self):
        target = self.base / 'target'
        target.mkdir()
        (target / 'index.sqlite3').write_text('must stay untouched')
        try:
            self.home.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable')
        with self.assertRaisesRegex(SafetyError, 'Link or reparse'):
            TrackingLimits().check_state(self.home)

    def test_invalid_budgets_are_rejected(self):
        for options in ({'max_files': 0}, {'max_files': True}, {'max_state_mib': 0},
                        {'max_state_mib': float('inf')}, {'max_state_mib': float('nan')}):
            with self.subTest(options=options), self.assertRaises(SafetyError):
                TrackingLimits(**options)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux mount metadata')
    def test_mount_detection_obeys_boundaries_nested_mounts_and_escapes(self):
        metadata = ('1 0 8:1 / / rw - ext4 /dev/root rw\n'
                    '2 1 0:1 / /tmp rw - tmpfs tmpfs rw\n'
                    '3 2 8:2 / /tmp/disk rw - ext4 /dev/disk rw\n'
                    '4 1 0:2 / /with\\040space rw - ramfs ramfs rw\n')
        for path, expected in (('/tmp/new/state', True), ('/tmpish/state', False),
                               ('/tmp/disk/state', False), ('/with space/state', True)):
            with self.subTest(path=path), patch('agentgc.limits.absolute', side_effect=lambda value: value), \
                    patch.object(Path, 'open', return_value=io.StringIO(metadata)):
                self.assertEqual(memory_filesystem(Path(path)), expected)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux mount metadata')
    def test_mount_inspection_failure_is_explicit(self):
        with patch.object(Path, 'open', side_effect=PermissionError('unavailable')):
            with self.assertRaisesRegex(SafetyError, 'Cannot inspect state filesystem'):
                check_state_location(self.home)

    def test_background_launch_propagates_limits_and_never_publishes_false_readiness(self):
        self.files(3)
        source = str(Path(__file__).resolve().parents[1] / 'src')
        popen = subprocess.Popen
        children = []

        def captured(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            self.addCleanup(stop_process, child)
            return child

        self.addCleanup(_reap_completed)
        with patch.dict(os.environ, PYTHONPATH=source, PYTHONDONTWRITEBYTECODE='1'), \
                patch('agentgc.watch_lifecycle.subprocess.Popen', side_effect=captured):
            result, code = start_watch(self.home, [self.root], backend='poll', seconds=5,
                                       limits=TrackingLimits(max_files=2), allow_memory_state=True)
        self.assertEqual(code, 1, result)
        self.assertFalse(result['ready'])
        self.assertTrue(result['start_failed'])
        self.assertFalse((self.home / 'watches' / result['control'] / 'ready.json').exists())
        self.assertIsNotNone(children[0].poll())
        self.assertIn('--max-files', children[0].args)
        self.assertIn('--max-state-mib', children[0].args)
        self.assertIn('--allow-memory-state', children[0].args)
        self.assertIn('--max-files 2', Path(result['log']).read_text())
