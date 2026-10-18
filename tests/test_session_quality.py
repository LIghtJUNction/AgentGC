from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentgc.paths import SafetyError, Scope
from agentgc.session_state import session_result
from agentgc.store import Store
from agentgc.tracker import Tracker, track


class SessionQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)
        self.scope = Scope([self.root], self.store.home)

    def command(self, code):
        return [sys.executable, "-c", f"raise SystemExit({code})"]

    def test_failed_command_has_successful_tracking(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, sid = track(self.store, self.scope, "test", backend="poll", interval=0.1,
                              command=self.command(7))
        result = session_result(self.store, sid)
        self.assertEqual(code, 7)
        self.assertEqual(result["session_status"], "failed")
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("failed", 7))
        self.assertEqual((result["tracking_status"], result["gap_count"]), ("complete", 0))
        self.assertEqual(result["roots"], [str(self.root)])

    def test_final_event_read_failure_keeps_successful_child(self):
        class Observer:
            name = "test-native"
            coverage_errors = []
            def read(self, timeout):
                raise OSError("injected final event read failure")
            def close(self):
                pass

        class CompletedChild:
            returncode = 0
            def poll(self):
                return self.returncode

        with patch("agentgc.tracker.make_backend", return_value=(Observer(), None)), \
                patch("agentgc.tracker.subprocess.Popen", return_value=CompletedChild()), \
                contextlib.redirect_stderr(io.StringIO()):
            code, sid = track(self.store, self.scope, "test", command=self.command(0))
        result = session_result(self.store, sid)
        self.assertEqual(code, 0)
        self.assertEqual(result["session_status"], "failed")
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("succeeded", 0))
        self.assertEqual((result["tracking_status"], result["gap_count"]), ("partial", 1))

    def test_incomplete_baseline_never_starts_command_or_fakes_child_exit(self):
        original = Tracker.scan
        def incomplete(tracker, source="session-correlated"):
            original(tracker, source)
            if source == "baseline":
                tracker.errors.append("injected unreadable directory")
                with tracker.store.transaction():
                    tracker.gap("Incomplete scan: injected unreadable directory")

        with patch("agentgc.tracker.Tracker.scan", incomplete), \
                patch("agentgc.tracker.subprocess.Popen") as spawn, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SafetyError, "Baseline scan was incomplete") as caught:
                track(self.store, self.scope, "test", backend="poll", command=self.command(0))
        spawn.assert_not_called()
        sid = self.store.db.execute("SELECT id FROM sessions").fetchone()[0]
        self.assertEqual(caught.exception.agentgc_session, sid)
        result = session_result(self.store, sid)
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("not-started", None))
        self.assertEqual(result["tracking_status"], "failed")
        self.assertGreater(result["gap_count"], 0)

    def test_failed_spawn_is_not_child_exit_one(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(FileNotFoundError):
                track(self.store, self.scope, "test", backend="poll", command=[str(self.base / "missing")])
        sid = self.store.db.execute("SELECT id FROM sessions").fetchone()[0]
        result = session_result(self.store, sid)
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("not-started", None))
        self.assertEqual(result["tracking_status"], "failed")

    def test_final_scan_exception_keeps_child_result_and_exposes_session(self):
        original = Tracker.scan
        def scan(tracker, source="session-correlated"):
            if source != "baseline":
                raise PermissionError("injected final scan failure")
            original(tracker, source)
        with patch("agentgc.tracker.Tracker.scan", scan), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(PermissionError, "injected final scan failure") as caught:
                track(self.store, self.scope, "test", backend="poll", command=self.command(0))
        result = session_result(self.store, caught.exception.agentgc_session)
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("succeeded", 0))
        self.assertEqual((result["tracking_status"], result["gap_count"]), ("partial", 1))

    def test_link_and_permission_notices_keep_real_paths_without_following_links(self):
        sid = self.store.start_session("test", [self.root], "poll")
        tracker = Tracker(self.store, self.scope, sid)
        target = self.base / "outside-scope"
        target.write_text("must not be indexed")
        link = self.root / "link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        tracker.sync(link)
        denied = self.root / "denied"
        with patch("agentgc.tracker.regular", side_effect=PermissionError("injected metadata denial")):
            tracker.sync(denied)
        self.assertEqual(self.store.stats()["files"], 0)
        self.assertEqual(len(tracker.errors), 2)
        rows = [dict(row) for row in self.store.db.execute("SELECT kind,path FROM events ORDER BY id")]
        self.assertEqual(rows, [
            {"kind": "link-boundary-notice", "path": str(link)},
            {"kind": "coverage-error-notice", "path": str(denied)},
        ])
        before = self.store.stats()["events"]
        tracker.sync(self.root / "gone")
        self.assertEqual(self.store.stats()["events"], before)

    def test_immediate_gap_message_is_compact_but_sqlite_keeps_original_detail(self):
        sid = self.store.start_session("test", [self.root], "poll")
        tracker = Tracker(self.store, self.scope, sid)
        detail = "Incomplete scan: " + "; ".join(
            f"{self.root}/long-build-path-{number:03}/output: Permission denied" for number in range(100)
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.store.transaction():
            tracker.gap(detail)
        notice = stderr.getvalue()
        self.assertLess(len(notice), 250)
        self.assertIn("permission", notice)
        self.assertIn("gap_count=1", notice)
        self.assertIn("agcc history --session " + sid, notice)
        self.assertNotIn("long-build-path", notice)
        row = self.store.db.execute("SELECT detail FROM events WHERE kind='gap'").fetchone()
        self.assertEqual(row["detail"], detail)

    def test_transient_unresolved_event_makes_completed_tracking_partial(self):
        path = self.root / "already-gone"
        class Observer:
            name = "test-native"
            coverage_errors = []
            def __init__(self):
                self.delivered = False
            def read(self, timeout):
                notices = [] if self.delivered else [(path, "created")]
                self.delivered = True
                return notices, False, False
            def pending(self):
                return False
            def close(self):
                pass

        with patch("agentgc.tracker.make_backend", return_value=(Observer(), None)), \
                contextlib.redirect_stderr(io.StringIO()):
            _, sid = track(self.store, self.scope, "test", duration=0.01)
        result = session_result(self.store, sid)
        self.assertEqual(result["session_status"], "finished")
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("not-applicable", None))
        self.assertEqual((result["tracking_status"], result["gap_count"], result["unresolved_event_count"]),
                         ("partial", 0, 1))
        self.assertEqual(result["count_scope"]["unresolved_event_count"], "retained-events")
        self.store.db.execute("DELETE FROM events WHERE session_id=?", (sid,))
        self.assertEqual(session_result(self.store, sid)["tracking_status"], "partial")
        self.assertEqual(session_result(self.store, sid)["unresolved_event_count"], 0)

    def test_old_read_only_database_does_not_guess_child_or_tracking_result(self):
        sid = self.store.start_session("legacy", [self.root], "poll", "python")
        self.store.end_session(sid, 1, "failed")
        self.store.db.execute("DROP TABLE session_results")
        reader = Store(self.store.home, read_only=True)
        self.addCleanup(reader.close)
        before = reader.db.total_changes
        result = session_result(reader, sid)
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("unknown", None))
        self.assertEqual(result["tracking_status"], "unknown")
        self.assertEqual((result["gap_count"], result["unresolved_event_count"]), (0, 0))
        self.assertEqual(reader.db.total_changes, before)
        self.assertFalse(reader.db.in_transaction)
        self.assertIsNone(reader.db.execute(
            "SELECT name FROM sqlite_master WHERE name='session_results'"
        ).fetchone())

    def test_running_and_manual_closure_keep_unobserved_child_exit_unknown(self):
        sid = self.store.start_session("test", [self.root], "poll", "python")
        self.store.command_started(sid)
        result = session_result(self.store, sid)
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("running", None))
        self.assertEqual(result["tracking_status"], "running")
        self.store.end_session(sid, 1, "closed-by-user")
        result = session_result(self.store, sid)
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("unknown", None))
        self.assertEqual(result["tracking_status"], "unknown")

    def test_raw_signal_returncode_is_separate_from_wrapper_exit(self):
        sid = self.store.start_session("test", [self.root], "poll", "python")
        self.store.command_started(sid)
        self.store.command_finished(sid, -15)
        self.store.tracking_finished(sid, "interrupted")
        self.store.end_session(sid, 143, "interrupted")
        result = session_result(self.store, sid)
        self.assertEqual((result["command_status"], result["command_exit_code"]), ("failed", -15))
        self.assertEqual(result["tracking_status"], "interrupted")

    def test_snapshot_and_outer_transaction_are_preserved(self):
        sid = self.store.start_session("test", [self.root], "poll")
        with self.store.transaction():
            self.store.event(str(self.root / "gone"), "created-unresolved", "filesystem-event", sid)
            result = session_result(self.store, {"id": sid})
            self.assertEqual(result["unresolved_event_count"], 1)
            self.assertTrue(self.store.db.in_transaction)
            with self.assertRaises(SafetyError):
                session_result(self.store, "missing-session")
            self.assertTrue(self.store.db.in_transaction)
        self.assertFalse(self.store.db.in_transaction)

    def test_final_tracking_and_lifecycle_are_published_together(self):
        reader = Store(self.store.home, read_only=True)
        self.addCleanup(reader.close)
        observations = []
        def inspect_before_lifecycle_write(sql):
            if sql.startswith("UPDATE sessions SET status="):
                sid = reader.db.execute("SELECT id FROM sessions").fetchone()[0]
                observations.append(session_result(reader, sid))
        self.store.db.set_trace_callback(inspect_before_lifecycle_write)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                _, sid = track(self.store, self.scope, "test", backend="poll", command=self.command(0))
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual(len(observations), 1)
        self.assertEqual((observations[0]["session_status"], observations[0]["tracking_status"]),
                         ("running", "running"))
        result = session_result(reader, sid)
        self.assertEqual((result["session_status"], result["tracking_status"]), ("finished", "complete"))


if __name__ == "__main__":
    unittest.main()
