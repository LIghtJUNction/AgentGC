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

from process_helpers import stop_process

from agentgc.cli import main
from agentgc.paths import Scope
from agentgc.store import Store
from agentgc.tracker import Tracker
from agentgc.watch_control import WatchControl
from agentgc.watch_lifecycle import _reap_completed


class QualityCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.home = self.base / "state"
        source = str(Path(__file__).resolve().parents[1] / "src")
        environment = patch.dict(os.environ, PYTHONPATH=source, PYTHONDONTWRITEBYTECODE="1")
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, *arguments, machine=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        options = ["--home", str(self.home), "--no-project-rules"]
        if machine:
            options.append("--json")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([*options, *map(str, arguments)])
        output, errors = stdout.getvalue(), stderr.getvalue()
        if not machine:
            return code, output, errors
        payloads = [json.loads(line) for line in (output + errors).splitlines()
                    if line.startswith(("{", "["))]
        self.assertEqual(len(payloads), 1, (output, errors))
        return code, payloads[0], output, errors

    def run_command(self, child_code=0, *options):
        return self.invoke("run", "--root", self.root, "--backend", "poll", "--interval", "0.1",
                           *options, "--", sys.executable, "-c", f"raise SystemExit({child_code})")

    def assert_quality(self, result, *, command, command_code, tracking, gaps, unresolved):
        self.assertEqual(result["schema_version"], 1)
        self.assertIsInstance(result["roots"], list)
        self.assertTrue(all(isinstance(root, str) for root in result["roots"]))
        self.assertEqual(result["command_status"], command)
        self.assertEqual(result["command_exit_code"], command_code)
        self.assertEqual(result["tracking_status"], tracking)
        self.assertEqual(result["gap_count"], gaps)
        self.assertEqual(result["unresolved_event_count"], unresolved)
        self.assertEqual(result["count_scope"], {
            "gap_count": "session-cumulative", "unresolved_event_count": "retained-events",
        })

    def test_successful_child_with_partial_tracking_keeps_exit_unless_strict(self):
        class BrokenFinalObserver:
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

        for options, expected in (((), 0), (("--strict-tracking",), 1)):
            with self.subTest(options=options), \
                    patch("agentgc.tracker.make_backend", return_value=(BrokenFinalObserver(), None)), \
                    patch("agentgc.tracker.subprocess.Popen", return_value=CompletedChild()):
                code, result, stdout, _ = self.run_command(0, *options)
            self.assertEqual(code, expected)
            self.assertEqual(stdout, "")
            self.assertEqual(result["cli_exit_code"], expected)
            self.assertEqual(result["session_status"], "failed")
            self.assert_quality(result, command="succeeded", command_code=0, tracking="partial",
                                gaps=1, unresolved=0)
            for arguments in (("session", result["session"]),
                              ("diagnostics", "--session", result["session"]),
                              ("sessions", "--root", self.root)):
                _, queried, _, _ = self.invoke(*arguments)
                if isinstance(queried, list):
                    queried = next(row for row in queried if row["session"] == result["session"])
                self.assert_quality(queried, command="succeeded", command_code=0, tracking="partial",
                                    gaps=1, unresolved=0)

    def test_failed_child_keeps_complete_tracking_and_real_exit_code(self):
        code, result, stdout, _ = self.run_command(7, "--strict-tracking")
        self.assertEqual((code, result["cli_exit_code"]), (7, 7))
        self.assertEqual(stdout, "")
        self.assertEqual(result["session_status"], "failed")
        self.assert_quality(result, command="failed", command_code=7, tracking="complete",
                            gaps=0, unresolved=0)
        query_code, queried, _, _ = self.invoke("session", result["session"])
        self.assertEqual(query_code, 0)
        self.assert_quality(queried, command="failed", command_code=7, tracking="complete",
                            gaps=0, unresolved=0)

    def test_baseline_failure_never_spawns_or_reports_a_child_exit(self):
        scan = Tracker.scan

        def incomplete(tracker, source="session-correlated"):
            scan(tracker, source)
            if source == "baseline":
                tracker.errors.append("injected baseline permission failure")
                with tracker.store.transaction():
                    tracker.gap("Incomplete scan: injected baseline permission failure")

        with patch("agentgc.tracker.Tracker.scan", incomplete), \
                patch("agentgc.tracker.subprocess.Popen") as spawn:
            code, result, stdout, _ = self.run_command()
        spawn.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Baseline scan was incomplete", result["error"])
        self.assert_quality(result, command="not-started", command_code=None, tracking="failed",
                            gaps=result["gap_count"], unresolved=0)
        self.assertGreater(result["gap_count"], 0)

    def test_spawn_failure_keeps_command_exit_null(self):
        code, result, stdout, _ = self.invoke("run", "--root", self.root, "--backend", "poll",
                                              "--", self.base / "missing-executable")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assert_quality(result, command="not-started", command_code=None, tracking="failed",
                            gaps=result["gap_count"], unresolved=0)
        self.assertGreater(result["gap_count"], 0)

    def test_final_scan_exception_preserves_observed_child_exit_by_default(self):
        scan = Tracker.scan

        class CompletedChild:
            returncode = 0

            def poll(self):
                return self.returncode

        def final_failure(tracker, source="session-correlated"):
            if source != "baseline":
                raise PermissionError("injected final reconciliation failure")
            scan(tracker, source)

        for options, expected in (((), 0), (("--strict-tracking",), 1)):
            with self.subTest(options=options), \
                    patch("agentgc.tracker.Tracker.scan", final_failure), \
                    patch("agentgc.tracker.subprocess.Popen", return_value=CompletedChild()):
                code, result, stdout, _ = self.run_command(0, *options)
            self.assertEqual(stdout, "")
            self.assertEqual(code, expected)
            self.assertEqual(result["cli_exit_code"], expected)
            self.assert_quality(result, command="succeeded", command_code=0, tracking="partial",
                                gaps=1, unresolved=0)
            self.assertIn("injected final reconciliation failure", result["error"])

    def seed_watcher(self, *, control="fixture", finished=False, gaps=2, unresolved=3):
        store = Store(self.home)
        try:
            scope = Scope([self.root], self.home)
            sid = store.start_session("fixture", scope.roots, "poll")
            for index in range(gaps):
                store.event("", "gap", "tracker", sid, detail=f"Permission denied fixture {index}")
            store.db.execute("UPDATE sessions SET gaps=? WHERE id=?", (gaps, sid))
            for index in range(unresolved):
                store.event(str(self.root / "gone"), "created-unresolved", "filesystem-event", sid)
            WatchControl(store, control).ready(sid, scope, 30)
            if finished:
                store.tracking_finished(sid, "partial")
                store.end_session(sid, 1, "failed")
            return sid
        finally:
            store.close()

    def test_session_queries_and_control_status_share_types_and_count_scope(self):
        sid = self.seed_watcher(finished=True)
        for arguments, expected_code in ((("session", sid), 0),
                                         (("sessions", "--root", self.root), 0),
                                         (("diagnostics", "--session", sid), 0),
                                         (("watch-status", "fixture"), 1)):
            with self.subTest(arguments=arguments):
                code, result, _, _ = self.invoke(*arguments)
                self.assertEqual(code, expected_code)
                if isinstance(result, list):
                    result = result[0]
                self.assertEqual(result["session"], sid)
                self.assertEqual(result["session_status"], "failed")
                self.assertEqual(result["roots"], [str(self.root)])
                self.assert_quality(result, command="not-applicable", command_code=None,
                                    tracking="partial", gaps=2, unresolved=3)
        code, text, errors = self.invoke("sessions", machine=False)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("session_status=", text)
        self.assertIn("failed", text)

    def test_watch_stop_no_wait_reports_request_and_typed_current_evidence(self):
        sid = self.seed_watcher()
        with patch("agentgc.watch_control.wait_status") as wait:
            code, result, _, _ = self.invoke("watch-stop", "fixture", "--no-wait")
        wait.assert_not_called()
        self.assertEqual(code, 0)
        self.assertTrue(result["stop_requested"])
        self.assertEqual(result["session"], sid)
        self.assertEqual(result["session_status"], "running")
        self.assertEqual(result["state"], "stopping")
        self.assert_quality(result, command="not-applicable", command_code=None,
                            tracking="running", gaps=2, unresolved=3)
        self.assertTrue((self.home / "watches/fixture/stop").is_file())

    def test_watch_stop_strictness_changes_cli_exit_without_faking_command_failure(self):
        self.seed_watcher()
        _, running, _, _ = self.invoke("watch-status", "fixture")
        finalized = {**running, "state": "failed", "status": "failed", "session_status": "failed",
                     "ready": False, "tracking_status": "partial", "timed_out": False}
        for options, expected in (((), 1), (("--no-strict-tracking",), 0)):
            with self.subTest(options=options), \
                    patch("agentgc.watch_control.wait_status", return_value=finalized) as wait:
                code, result, _, _ = self.invoke("watch-stop", "fixture", *options)
                self.assertEqual(code, expected)
                self.assertTrue(result["stop_requested"])
                self.assertEqual(result["session_status"], "failed")
                self.assert_quality(result, command="not-applicable", command_code=None,
                                    tracking="partial", gaps=2, unresolved=3)
                self.assertEqual(wait.call_args.args[2], "finished")
                self.assertGreater(wait.call_args.args[3], 0)

    def test_real_watch_start_and_default_stop_wait_return_final_quality(self):
        children = []
        popen = subprocess.Popen

        def capture(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        try:
            with patch("agentgc.watch_lifecycle.subprocess.Popen", side_effect=capture):
                code, started, _, _ = self.invoke("watch-start", "--root", self.root,
                                                   "--backend", "poll", "--interval", "0.1",
                                                   "--duration", "20", "--wait-ready", "5")
            self.assertEqual(code, 0, started)
            self.assertTrue(started["ready"])
            self.assertTrue(started["coverage_known"])
            self.assertFalse(started["start_failed"])
            self.assertEqual(len(children), 1)
            self.assertEqual(started["pid"], children[0].pid)
            self.assert_quality(started, command="not-applicable", command_code=None,
                                tracking="running", gaps=0, unresolved=0)
            output = self.root / "result.txt"
            output.write_text("final observed output", encoding="utf-8")
            code, stopped, _, _ = self.invoke("watch-stop", started["control"])
            self.assertEqual(code, 0, stopped)
            self.assertEqual(stopped["state"], "finished")
            self.assertEqual(stopped["session"], started["session"])
            self.assertFalse(stopped["timed_out"])
            self.assert_quality(stopped, command="not-applicable", command_code=None,
                                tracking="complete", gaps=0, unresolved=0)
            children[0].wait(timeout=3)
            store = Store(self.home, read_only=True)
            try:
                self.assertEqual(store.file(output)["size"], len("final observed output"))
            finally:
                store.close()
        finally:
            for child in children:
                stop_process(child)
            _reap_completed()

    def test_diagnostics_defaults_to_compact_and_details_preserve_raw_evidence(self):
        store = Store(self.home)
        try:
            sid = store.start_session("fixture", [self.root], "poll")
            details = [f"Incomplete scan: Permission denied long-original-detail-{index:03}"
                       for index in range(100)]
            for detail in details:
                store.event("", "gap", "tracker", sid, detail=detail)
            store.db.execute("UPDATE sessions SET gaps=100 WHERE id=?", (sid,))
            store.tracking_finished(sid, "partial")
            store.end_session(sid, 1, "failed")
        finally:
            store.close()
        code, compact, errors = self.invoke("diagnostics", "--session", sid, machine=False)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("partial", compact)
        self.assertIn("gap_count=100", compact)
        self.assertIn("--details diagnostics --session", compact)
        self.assertNotIn("long-original-detail", compact)
        code, expanded, errors = self.invoke("--details", "diagnostics", "--session", sid,
                                             machine=False)
        self.assertEqual((code, errors), (0, ""))
        for detail in details:
            self.assertIn(detail, expanded)
        _, raw, _, _ = self.invoke("diagnostics", "--session", sid)
        self.assertEqual({group["detail"] for group in raw["groups"]}, set(details))
        self.assertEqual(raw["retained_gap_events"], 100)


if __name__ == "__main__":
    unittest.main()
