from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentgc.native import Inotify
from agentgc.paths import SafetyError, Scope
from agentgc.store import Store
from agentgc.tracker import Tracker, scan_once, track
from agentgc.watch_control import WatchControl, watch_status


class IdleObserver:
    name = "fake-native"

    def __init__(self, clock, dirty=False):
        self.clock = clock
        self.timeouts = []
        self.dirty = dirty
        self.refreshes = 0

    def watch_control(self, path):
        pass

    def read(self, timeout):
        if timeout:
            self.timeouts.append(timeout)
            self.clock[0] += 0.2 if self.dirty else timeout
        dirty, self.dirty = self.dirty, False
        return [], dirty, False

    def pending(self):
        return False

    def refresh(self):
        self.refreshes += 1

    def close(self):
        pass


class BackgroundEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)
        self.scope = Scope([self.root], self.store.home)

    def fake_track(self, duration=11, dirty=False, reconcile=60):
        clock = [0.0]
        observer = IdleObserver(clock, dirty)
        statements = []
        self.store.db.set_trace_callback(statements.append)
        with patch("agentgc.tracker.make_backend", return_value=(observer, None)), \
                patch("agentgc.tracker.time.monotonic", side_effect=lambda: clock[0]), \
                contextlib.redirect_stderr(io.StringIO()):
            code, sid = track(self.store, self.scope, "test", duration=duration, reconcile=reconcile)
        return observer, statements, code, sid

    @unittest.skipUnless(sys.platform.startswith("linux"), "native signal wakeup is Linux-only")
    def test_idle_waits_follow_heartbeat_and_duration_without_empty_transactions(self):
        observer, statements, code, _ = self.fake_track()
        self.assertEqual(code, 0)
        self.assertEqual(observer.timeouts, [5, 5, 1])
        self.assertEqual(sum(sql == "BEGIN IMMEDIATE" for sql in statements), 2)
        with self.assertRaises(OSError):
            os.fstat(observer.wake_fd)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native signal wakeup is Linux-only")
    def test_reconciliation_and_dirty_scan_deadlines_are_not_delayed(self):
        observer, statements, _, _ = self.fake_track(duration=8, reconcile=3)
        self.assertEqual(observer.timeouts, [3, 2, 1, 2])
        self.assertEqual(sum(sql == "BEGIN IMMEDIATE" for sql in statements), 4)
        observer, _, _, _ = self.fake_track(duration=2, dirty=True)
        self.assertAlmostEqual(observer.timeouts[1], 0.8)
        self.assertEqual(observer.refreshes, 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native signal wakeup is Linux-only")
    def test_signal_wakeup_failure_keeps_bounded_short_waits(self):
        with patch("agentgc.tracker.signal.set_wakeup_fd", side_effect=ValueError("unavailable")):
            observer, _, code, _ = self.fake_track(duration=2)
        self.assertEqual(code, 0)
        self.assertEqual(observer.timeouts, [1, 1])

    @unittest.skipUnless(sys.platform.startswith("linux"), "native signal wakeup is Linux-only")
    def test_previous_signal_wakeup_fd_and_handlers_are_restored(self):
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        previous = signal.set_wakeup_fd(write_fd)
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            self.fake_track(duration=1)
            self.assertEqual(signal.set_wakeup_fd(-1), write_fd)
            for sig, handler in handlers.items():
                self.assertEqual(signal.getsignal(sig), handler)
        finally:
            signal.set_wakeup_fd(previous)
            os.close(read_fd)
            os.close(write_fd)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux inotify required")
    def test_control_registration_and_refresh_respect_watch_budget(self):
        control = self.store.home / "control"
        control.mkdir()
        observer = Inotify(self.scope, max_watches=1)
        try:
            with self.assertRaises(OSError):
                observer.watch_control(control)
            self.assertIsNone(observer.control_wd)
            os.fstat(observer.fd)
        finally:
            observer.close()
        observer = Inotify(self.scope, max_watches=2)
        try:
            observer.watch_control(control)
            with self.assertRaises(SafetyError):
                observer.watch_control(control)
            (self.root / "child").mkdir()
            with self.assertRaises(OSError):
                observer.refresh()
            self.assertEqual(len(observer.watches), 1)
        finally:
            observer.close()

    @unittest.skipIf(sys.platform == "win32", "Windows retains case-insensitive reconciliation fallback")
    def test_missing_file_reconciliation_is_scoped_and_uses_path_index(self):
        nested = self.root / "nested"
        nested.mkdir()
        owned = nested / "gone"
        owned.write_text("owned")
        other = self.base / "work-extra"
        other.mkdir()
        unrelated = other / "not-ours"
        unrelated.write_text("unrelated")
        scan_once(self.store, Scope([nested, other], self.store.home))
        owned.unlink()
        unrelated.unlink()
        statements = []
        self.store.db.set_trace_callback(statements.append)
        scan_once(self.store, self.scope)
        self.assertEqual(self.store.file(owned)["present"], 0)
        self.assertEqual(self.store.file(unrelated)["present"], 1)
        queries = [sql for sql in statements if sql.startswith("SELECT path FROM files")]
        self.assertEqual(len(queries), 1)
        self.assertIn("path>=", queries[0])
        plan = self.store.db.execute(
            "EXPLAIN QUERY PLAN SELECT path FROM files WHERE path>=? AND path<? AND present=1 "
            "AND (scan_token IS NULL OR scan_token<>?)", (str(self.root)+os.sep, str(self.root)+"0", "unused")
        ).fetchall()
        self.assertTrue(any("INDEX" in row[3] for row in plan))

    def test_slow_successful_baseline_publishes_fresh_readiness(self):
        clock = [1000.0]
        control = WatchControl(self.store, "slow-baseline")
        original_scan, original_ready = Tracker.scan, control.ready
        published = []
        def scanned(tracker, source="session-correlated"):
            original_scan(tracker, source)
            if source == "baseline":
                clock[0] += 20
        def ready(*args):
            original_ready(*args)
            published.append(watch_status(self.store, "slow-baseline"))
        def advance(seconds):
            clock[0] += seconds
        with patch("agentgc.tracker.make_backend", return_value=(None, None)), \
                patch("agentgc.tracker.Tracker.scan", scanned), \
                patch("agentgc.tracker.time.time", side_effect=lambda: clock[0]), \
                patch("agentgc.tracker.time.monotonic", side_effect=lambda: clock[0]), \
                patch("agentgc.tracker.time.sleep", side_effect=advance), \
                patch.object(control, "ready", side_effect=ready), \
                contextlib.redirect_stderr(io.StringIO()):
            code, _ = track(self.store, self.scope, "test", backend="poll", duration=1, control=control)
        self.assertEqual(code, 0)
        self.assertEqual(len(published), 1)
        self.assertTrue(published[0]["ready"])
        self.assertEqual(published[0]["heartbeat_age_seconds"], 0)

    def case_renamed_scope(self):
        original = self.root / "Work"
        original.mkdir()
        gone = original / "gone"
        gone.write_text("owned")
        scan_once(self.store, Scope([original], self.store.home))
        middle = self.root / "intermediate"
        renamed = self.root / "work"
        original.rename(middle)
        middle.rename(renamed)
        (renamed / "gone").unlink()
        return gone, Scope([renamed], self.store.home)

    def assert_case_deletion(self, gone):
        row = self.store.db.execute("SELECT id,present FROM files WHERE path=?", (str(gone),)).fetchone()
        self.assertEqual(row["present"], 0)
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM events WHERE file_id=? AND kind='deleted'", (row["id"],)
        ).fetchone()[0], 1)

    @unittest.skipIf(sys.platform == "win32", "real Windows path behavior is tested separately")
    def test_windows_fallback_preserves_case_changed_missing_file_candidates(self):
        gone, scope = self.case_renamed_scope()
        prefix = (str(scope.roots[0]) + os.sep).casefold()
        with patch("agentgc.tracker.sys.platform", "win32"), \
                patch.object(scope, "includes", side_effect=lambda path: str(path).casefold().startswith(prefix)):
            scan_once(self.store, scope)
        self.assert_case_deletion(gone)

    @unittest.skipUnless(sys.platform == "win32", "real case-insensitive Windows paths required")
    def test_windows_case_rename_is_reconciled_using_original_indexed_spelling(self):
        gone, scope = self.case_renamed_scope()
        scan_once(self.store, scope)
        self.assert_case_deletion(gone)
