import contextlib
import ctypes
import errno
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentgc.native import Inotify
from agentgc.paths import Scope
from agentgc.store import Store
from agentgc.tracker import track


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux inotify')
class PartialNativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'work'
        self.root.mkdir()
        self.bad = self.root / 'restricted'
        self.bad.mkdir()
        self.nested = self.bad / 'nested'
        self.nested.mkdir()
        self.good = self.root / 'good'
        self.good.mkdir()
        self.store = Store(self.root.parent / 'state')
        self.addCleanup(self.store.close)
        self.scope = Scope([self.root], self.store.home)

    def observer(self):
        observer = Inotify(self.scope)
        self.addCleanup(observer.close)
        return observer

    def test_unreadable_subtree_retains_healthy_events_and_existing_watches(self):
        observer = self.observer()
        before = dict(observer.watches)
        original = os.scandir
        def restricted(path):
            if Path(path) == self.bad:
                raise PermissionError(errno.EACCES, 'Permission denied', str(path))
            return original(path)
        with patch('agentgc.paths.os.scandir', side_effect=restricted):
            observer.refresh()
        self.assertEqual(observer.watches, before)
        self.assertTrue(observer.coverage_errors)
        file = self.good / 'new'
        file.write_text('data')
        notices, _, _ = observer.read(0.2)
        self.assertIn(file, [p for p, _ in notices])
        observer.refresh()
        self.assertEqual(observer.coverage_errors, [])

    def test_local_watch_registration_failure_is_retried(self):
        observer = self.observer()
        # Add a previously unwatched path after the initial walk.
        new = self.root / 'new-directory'
        new.mkdir()
        original = observer.lib.inotify_add_watch
        def denied(fd, path, mask):
            if os.fsdecode(path) == str(new):
                ctypes.set_errno(errno.EACCES)
                return -1
            return original(fd, path, mask)
        with patch.object(observer.lib, 'inotify_add_watch', side_effect=denied):
            observer.refresh()
        self.assertNotIn(new, observer.watches.values())
        self.assertIn(self.good, observer.watches.values())
        self.assertTrue(observer.coverage_errors)
        observer.refresh()
        self.assertIn(new, observer.watches.values())
        self.assertFalse(observer.coverage_errors)

    def test_kernel_resource_errors_still_fail(self):
        observer = self.observer()
        def exhausted(*args):
            ctypes.set_errno(errno.ENOSPC)
            return -1
        with patch.object(observer.lib, 'inotify_add_watch', side_effect=exhausted):
            with self.assertRaises(OSError):
                observer.refresh()

    def test_inaccessible_existing_watches_still_count_against_budget(self):
        observer = self.observer()
        observer.max_watches = len(observer.watches)
        (self.root / 'extra').mkdir()
        with self.assertRaises(OSError):
            observer.refresh()
        self.assertLessEqual(len(observer.watches), observer.max_watches)

    def test_replaced_directory_does_not_leak_a_stale_registration(self):
        observer = self.observer()
        count = len(observer.watches)
        self.good.rmdir()
        self.good.mkdir()
        observer.refresh()
        self.assertEqual(len(observer.watches), count)
        self.assertEqual(list(observer.watches.values()).count(self.good), 1)

    def test_new_link_replacing_subtree_does_not_retain_old_watch(self):
        observer = self.observer()
        self.nested.rmdir()
        self.bad.rmdir()
        self.bad.symlink_to(self.good, target_is_directory=True)
        observer.refresh()
        self.assertNotIn(self.bad, observer.watches.values())
        self.assertNotIn(self.nested, observer.watches.values())

    def test_tracker_keeps_partial_backend_and_retries_without_dirty_event(self):
        observer = self.observer()
        observer.coverage_errors = ['restricted: Permission denied']
        with patch('agentgc.tracker.make_backend', return_value=(observer, None)), \
                patch.object(observer, 'refresh', wraps=observer.refresh) as refresh, \
                contextlib.redirect_stderr(io.StringIO()):
            code, sid = track(self.store, self.scope, 'test', interval=0.1, reconcile=0.1, duration=0.25)
        row = self.store.db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        self.assertEqual(code, 0)
        self.assertEqual(row['backend'], 'inotify')
        self.assertGreater(row['gaps'], 0)
        self.assertGreaterEqual(refresh.call_count, 1)

    def test_persistent_partial_coverage_finalizes_failed_not_silently_complete(self):
        observer = self.observer()
        observer.coverage_errors = ['restricted: Permission denied']
        with patch('agentgc.tracker.make_backend', return_value=(observer, None)), \
                contextlib.redirect_stderr(io.StringIO()):
            code, sid = track(self.store, self.scope, 'test', interval=0.1, reconcile=1, duration=0.1)
        row = self.store.db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        self.assertEqual((code, row['status'], row['backend']), (1, 'failed', 'inotify'))
        self.assertGreater(row['gaps'], 0)
