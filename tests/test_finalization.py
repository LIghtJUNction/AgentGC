from __future__ import annotations

from collections import deque
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agentgc.paths import Scope
from agentgc.store import Store
from agentgc.tracker import track


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.scope = Scope([self.root], self.store.home)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_track(self, observer, command=None):
        control = Mock()
        control.stopping.return_value = command is None
        with patch("agentgc.tracker.make_backend", return_value=(observer, None)), \
                contextlib.redirect_stderr(io.StringIO()):
            code, sid = track(self.store, self.scope, "test", control=control, command=command)
        row = self.store.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return code, row

    def observer(self):
        observer = Mock()
        observer.name = "fake-native"
        observer.pending.return_value = False
        return observer

    def test_shutdown_drains_all_batches_including_empty_directory_batch(self):
        observer = self.observer()
        batches = deque([
            ([(self.root / "first-gone", "created")], False, False),
            ([], True, False),
            ([(self.root / "last-gone", "deleted")], False, False),
        ])
        observer.read.side_effect = lambda timeout: batches.popleft()
        observer.pending.side_effect = lambda: bool(batches)
        code, row = self.run_track(observer)
        self.assertEqual(code, 0)
        self.assertEqual(row["status"], "finished")
        events = list(self.store.db.execute("SELECT path,kind,size FROM events ORDER BY id"))
        self.assertEqual([(e["path"], e["kind"]) for e in events], [
            (str(self.root / "first-gone"), "created-unresolved"),
            (str(self.root / "last-gone"), "deleted-unresolved"),
        ])
        self.assertTrue(all(e["size"] is None for e in events))
        observer.close.assert_called_once()

    def test_shutdown_drain_has_batch_budget_and_visible_gap(self):
        observer = self.observer()
        observer.read.return_value = ([], True, False)
        observer.pending.return_value = True
        with patch("agentgc.tracker.time.monotonic", return_value=10):
            code, row = self.run_track(observer)
        self.assertEqual(observer.read.call_count, 32)
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["gaps"], 1)
        gap = self.store.db.execute("SELECT detail FROM events WHERE kind='gap'").fetchone()[0]
        self.assertIn("drain limit", gap)

    def test_shutdown_drain_has_time_budget(self):
        observer = self.observer()
        observer.read.return_value = ([], True, False)
        observer.pending.return_value = True
        with patch("agentgc.tracker.time.monotonic", side_effect=[0, 0, 2]):
            code, row = self.run_track(observer)
        self.assertEqual(observer.read.call_count, 1)
        self.assertEqual(code, 1)
        self.assertEqual(row["gaps"], 1)

    def test_final_read_failure_still_scans_and_reports_failure(self):
        observer = self.observer()
        path = self.root / "final-output"
        def fail_read(timeout):
            path.write_text("surviving output")
            raise OSError("fake read failure")
        observer.read.side_effect = fail_read
        code, row = self.run_track(observer)
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["gaps"], 1)
        self.assertEqual(self.store.file(path)["present"], 1)
        observer.close.assert_called_once()

    def test_final_read_failure_preserves_child_exit_code(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                observer = self.observer()
                observer.read.side_effect = OSError("fake read failure")
                process = Mock()
                process.poll.return_value = exit_code
                process.returncode = exit_code
                with patch("agentgc.tracker.subprocess.Popen", return_value=process):
                    code, row = self.run_track(observer, ["fake-command"])
                self.assertEqual(code, exit_code)
                self.assertEqual(row["exit_code"], exit_code)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["gaps"], 1)

    def test_final_event_metadata_error_is_not_hidden_by_final_scan(self):
        observer = self.observer()
        path = self.root / "unreadable"
        observer.read.return_value = ([(path, "modified")], False, False)
        with patch("agentgc.tracker.regular", side_effect=PermissionError("denied")):
            code, row = self.run_track(observer)
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["gaps"], 1)

    def test_final_scan_incomplete_fails_without_claiming_deletion(self):
        observer = self.observer()
        observer.read.return_value = ([], False, False)
        path = self.root / "protected-output"
        path.write_text("existing")
        original_walk = self.scope.walk
        calls = 0
        def walk(root, errors, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield from original_walk(root, errors, **kwargs)
            else:
                errors.append("fake final walk denied")
        with patch.object(self.scope, "walk", side_effect=walk):
            code, row = self.run_track(observer)
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["gaps"], 1)
        self.assertEqual(self.store.file(path)["present"], 1)

    def test_pending_readiness_failure_still_reconciles(self):
        observer = self.observer()
        path = self.root / "readiness-output"
        def read(timeout):
            path.write_text("surviving output")
            return [], False, False
        observer.read.side_effect = read
        observer.pending.side_effect = OSError("fake readiness failure")
        code, row = self.run_track(observer)
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["gaps"], 1)
        self.assertEqual(self.store.file(path)["present"], 1)
        observer.close.assert_called_once()

    def test_native_pending_uses_nonblocking_readiness(self):
        from agentgc.native import Inotify
        observer = object.__new__(Inotify)
        observer.fd = 123
        with patch("agentgc.native.select.select", return_value=([123], [], [])) as readiness:
            self.assertTrue(observer.pending())
            readiness.assert_called_once_with([123], [], [], 0)
        with patch("agentgc.native.select.select", return_value=([], [], [])):
            self.assertFalse(observer.pending())
