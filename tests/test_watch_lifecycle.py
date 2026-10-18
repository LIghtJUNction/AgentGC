from __future__ import annotations

import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from process_helpers import stop_process

from agentgc.paths import SafetyError, Scope
from agentgc.store import Store
from agentgc.watch_control import WatchControl, control_command, readiness, request_stop, starting_status, watch_status
from agentgc.watch_lifecycle import _children, _reap_completed, _status, start_watch


class WatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.home = self.base / "state"
        self.children = []
        popen = subprocess.Popen

        def captured(*args, **kwargs):
            child = popen(*args, **kwargs)
            self.children.append(child)
            self.addCleanup(stop_process, child)
            return child

        self.addCleanup(_reap_completed)
        self.spawn = patch("agentgc.watch_lifecycle.subprocess.Popen", side_effect=captured)
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        source = str(Path(__file__).resolve().parents[1] / "src")
        environment = patch.dict(os.environ, PYTHONPATH=source, PYTHONDONTWRITEBYTECODE="1")
        environment.start()
        self.addCleanup(environment.stop)

    def start(self, **kwargs):
        return start_watch(self.home, [self.root], backend="poll", interval=0.1,
                           reconcile=1, duration=10, seconds=5, **kwargs)

    def test_real_background_start_and_stop_wait_capture_final_state(self):
        result, code = self.start(excludes=["ignored"])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ready"])
        self.assertFalse(result["start_failed"])
        self.assertTrue(result["coverage_known"])
        self.assertEqual(result["roots"], [str(self.root)])
        self.assertEqual(result["excludes"], ["ignored"])
        self.assertEqual(result["gap_count"], 0)
        self.assertEqual(result["unresolved_event_count"], 0)
        self.assertIsNone(result["command_exit_code"])
        self.assertEqual(result["pid"], self.children[0].pid)
        if os.name == "posix":
            self.assertNotEqual(os.getsid(result["pid"]), os.getsid(0))
        output = self.root / "result.txt"
        output.write_text("final result")
        ignored = self.root / "ignored"
        ignored.write_text("excluded")
        stopped, code = control_command(self.home, result["control"], stop=True, seconds=5)
        self.assertEqual(code, 0, stopped)
        self.assertEqual(stopped["session"], result["session"])
        self.assertEqual(stopped["state"], "finished")
        self.assertEqual(stopped["tracking_status"], "complete")
        self.children[0].wait(timeout=3)
        store = Store(self.home, read_only=True)
        try:
            self.assertEqual(store.file(output)["size"], len("final result"))
            with self.assertRaisesRegex(SafetyError, "Not indexed"):
                store.file(ignored)
        finally:
            store.close()

    def test_timeout_terminates_and_reaps_only_just_started_child(self):
        result, code = start_watch(self.home, [self.root], backend="poll", seconds=0,
                                   duration=10, control="timeout")
        self.assertEqual(code, 1)
        self.assertFalse(result["ready"])
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["start_failed"])
        self.assertIsNotNone(self.children[0].poll())
        self.assertIsNotNone(result["launcher_exit_code"])
        self.assertTrue(Path(result["log"]).is_file())

    def test_unreapable_child_reports_failure_without_exception_command_arguments(self):
        secret = "launch-argument-must-not-escape-93821"
        child = Mock(pid=54321, returncode=None)
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired(["python", secret], 3)
        pending = starting_status(self.home, "unreapable")
        try:
            with patch("agentgc.watch_lifecycle.subprocess.Popen", return_value=child), \
                    patch("agentgc.watch_lifecycle._status", return_value=pending):
                result, code = start_watch(self.home, [self.root], control="unreapable", seconds=0)
            self.assertEqual(code, 1)
            self.assertTrue(result["cleanup_failed"])
            self.assertTrue(result["start_failed"])
            self.assertFalse(result["ready"])
            self.assertIn("may still be running", result["startup_error"])
            self.assertNotIn(secret, str(result))
            self.assertEqual(result["launcher_pid"], child.pid)
            child.terminate.assert_called_once()
            child.kill.assert_called_once()
        finally:
            _children.pop((str(self.home), "unreapable"), None)

    def test_existing_control_cannot_be_reused_or_stop_its_owner(self):
        result, code = self.start(control="owned")
        self.assertEqual(code, 0, result)
        with self.assertRaisesRegex(SafetyError, "cannot be reused"):
            self.start(control="owned")
        self.assertEqual(len(self.children), 1)
        self.assertIsNone(self.children[0].poll())
        self.assertFalse((self.home / "watches/owned/stop").exists())
        stopped, code = control_command(self.home, "owned", stop=True, seconds=5)
        self.assertEqual(code, 0, stopped)

    def locked_initializing_store(self):
        self.home.mkdir()
        database = sqlite3.connect(self.home / 'index.sqlite3', check_same_thread=False)
        self.addCleanup(database.close)
        database.execute('PRAGMA user_version=1')
        database.execute('BEGIN EXCLUSIVE')
        return database

    def test_exclusive_initialization_lock_is_pending_without_faking_readiness(self):
        database = self.locked_initializing_store()
        result = _status(self.home, 'initializing')
        self.assertEqual(result['state'], 'starting')
        self.assertFalse(result['ready'])
        self.assertIsNone(result['session'])
        self.assertIn('temporarily busy', result['pending_reason'])
        self.assertTrue(database.in_transaction)
        self.assertFalse((self.home / 'watches').exists())

    def test_busy_connect_is_also_pending_without_an_initialized_connection(self):
        self.locked_initializing_store()
        busy = sqlite3.OperationalError('database is locked')
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with patch('agentgc.store.sqlite3.connect', side_effect=busy):
            result = _status(self.home, 'opening')
        self.assertFalse(result['ready'])
        self.assertIn('temporarily busy', result['pending_reason'])

    def test_background_start_waits_for_initialization_lock_instead_of_reaping_healthy_child(self):
        database = self.locked_initializing_store()
        release = threading.Timer(0.3, database.rollback)
        release.start()
        try:
            result, code = self.start(control='initializing')
        finally:
            release.join(timeout=2)
        self.assertFalse(release.is_alive())
        self.assertEqual(code, 0, result)
        self.assertTrue(result['ready'])
        self.assertIsNone(self.children[0].poll())
        stopped, code = control_command(self.home, result['control'], stop=True, seconds=5)
        self.assertEqual(code, 0, stopped)

    def test_corrupt_existing_database_is_not_reported_as_pending(self):
        self.home.mkdir()
        (self.home / 'index.sqlite3').write_bytes(b'not a SQLite database')
        with self.assertRaises(sqlite3.DatabaseError):
            _status(self.home, 'corrupt')

    def test_failed_launch_name_also_cannot_be_reused(self):
        result, code = start_watch(self.home, [self.root], backend="poll", seconds=0,
                                   control="failed")
        self.assertEqual(code, 1)
        with self.assertRaises((FileExistsError, SafetyError)):
            self.start(control="failed")
        self.assertEqual(len(self.children), 1)

    def test_logs_do_not_save_launch_arguments_or_file_contents(self):
        secret = "private-content-not-logged-79382"
        (self.root / "payload").write_text(secret)
        result, code = self.start(agent="argument-not-logged-18392")
        self.assertEqual(code, 0, result)
        stopped, code = control_command(self.home, result["control"], stop=True, seconds=5)
        self.assertEqual(code, 0, stopped)
        self.children[0].wait(timeout=3)
        log = Path(result["log"])
        contents = log.read_text()
        self.assertNotIn(secret, contents)
        self.assertNotIn("argument-not-logged-18392", contents)
        self.assertNotIn("--root", contents)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)

    def test_labels_and_exclusions_that_start_with_hyphens_remain_values(self):
        result, code = self.start(agent="--agent-value", excludes=["--exclude-value"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["agent"], "--agent-value")
        self.assertEqual(result["excludes"], ["--exclude-value"])
        stopped, code = control_command(self.home, result["control"], stop=True, seconds=5)
        self.assertEqual(code, 0, stopped)

    def test_multiple_root_order_does_not_change_verified_coverage(self):
        other = self.base / "other"
        other.mkdir()
        roots = [self.root, other]
        parent_order = [str(root) for root in Scope(roots, self.home).roots]

        def reversed_roots(home, control):
            result = _status(home, control)
            if result["ready"]:
                result = {**result, "roots": list(reversed(parent_order))}
            return result

        with patch("agentgc.watch_lifecycle._status", side_effect=reversed_roots):
            result, code = start_watch(self.home, roots, backend="poll", duration=10, seconds=5)
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["roots"]), set(parent_order))
        stopped, code = control_command(self.home, result["control"], stop=True, seconds=5)
        self.assertEqual(code, 0, stopped)

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions required")
    def test_failed_baseline_does_not_publish_ready_or_leave_child_running(self):
        blocked = self.root / "blocked"
        blocked.mkdir()
        blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o700)
        if os.access(blocked, os.R_OK):
            self.skipTest("Current user bypasses directory permission checks")
        result, code = self.start(control="bad-baseline")
        self.assertEqual(code, 1, result)
        self.assertFalse(result["ready"])
        self.assertTrue(result["start_failed"])
        self.assertFalse(result["timed_out"])
        self.assertIsNone(result["session"])
        self.assertFalse((self.home / "watches/bad-baseline/ready.json").exists())
        self.assertIsNotNone(self.children[0].poll())
        self.assertIn("Baseline scan was incomplete", Path(result["log"]).read_text())

    def test_foreign_readiness_never_becomes_our_success_or_receives_stop(self):
        foreign = {"pid": 123456789, "ready": True, "state": "ready", "session": "foreign"}
        with patch("agentgc.watch_lifecycle._status", return_value=foreign):
            result, code = self.start(control="raced")
        self.assertEqual(code, 1)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["session"])
        self.assertIn("another process", result["startup_error"])
        self.assertIsNotNone(self.children[0].poll())
        self.assertFalse((self.home / "watches/raced/stop").exists())

    def test_invalid_settings_fail_before_launch(self):
        for kwargs in ({"seconds": -1}, {"seconds": 301}, {"seconds": float("nan")},
                       {"duration": float("inf")}, {"duration": 0}, {"interval": 0.01},
                       {"interval": 2, "reconcile": 1}, {"control": "../escape"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SafetyError):
                start_watch(self.home, [self.root], **kwargs)
        self.assertEqual(self.children, [])
        self.assertFalse(self.home.exists())

    def test_linked_log_directory_is_rejected_without_writing_its_target(self):
        self.home.mkdir()
        other = self.base / "outside-state"
        other.mkdir()
        try:
            (self.home / "watch-launches").symlink_to(other, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(SafetyError, "Link or reparse"):
            self.start(control="linked-log")
        self.assertEqual(self.children, [])
        self.assertEqual(list(other.iterdir()), [])

    def test_uninitialized_status_has_typed_unknown_evidence_without_creating_state(self):
        result, code = control_command(self.home, "not-started")
        self.assertEqual(code, 1)
        self.assertEqual(result["roots"], [])
        self.assertEqual(result["tracking_status"], "unknown")
        for key in ("session", "command_exit_code", "gap_count", "unresolved_event_count"):
            self.assertIsNone(result[key])
        self.assertFalse(self.home.exists())

    def test_control_and_quality_fields_share_one_read_only_snapshot(self):
        owner = Store(self.home)
        writer = Store(self.home)
        self.addCleanup(owner.close)
        self.addCleanup(writer.close)
        control = WatchControl(owner, "snapshot")
        session = owner.start_session("test", [self.root], "poll")
        control.ready(session)
        reader = Store(self.home, read_only=True)
        self.addCleanup(reader.close)

        def finalized(store, name):
            result = readiness(store, name)
            writer.end_session(session, 0)
            return result

        with patch("agentgc.watch_control.readiness", side_effect=finalized):
            result = watch_status(reader, "snapshot")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["session_status"], "running")
        self.assertEqual(result["state"], "ready")
        result = watch_status(reader, "snapshot")
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["session_status"], "finished")

    def test_request_only_stop_has_stable_fields_without_waiting_or_closing_session(self):
        store = Store(self.home)
        self.addCleanup(store.close)
        control = WatchControl(store, "request-only")
        session = store.start_session("test", [self.root], "poll")
        control.ready(session, Scope([self.root], self.home, ["ignored"]), 60)
        with patch("agentgc.watch_control.time.sleep", side_effect=AssertionError("request-only stop waited")):
            result, code = control_command(self.home, "request-only", stop=True)
        self.assertEqual(code, 0)
        self.assertTrue(result["stop_requested"])
        self.assertEqual(result["session"], session)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["roots"], [str(self.root)])
        self.assertEqual(result["excludes"], ["ignored"])
        self.assertTrue(result["coverage_known"])
        self.assertEqual(result["state"], "stopping")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["session_status"], "running")
        self.assertEqual(result["command_status"], "not-applicable")
        self.assertIsNone(result["command_exit_code"])
        self.assertEqual(result["tracking_status"], "running")
        self.assertEqual(result["gap_count"], 0)
        self.assertEqual(result["unresolved_event_count"], 0)
        self.assertEqual(store.stats()["active_sessions"], 1)
        self.assertTrue(control.stopping())

    def test_request_only_stop_keeps_one_snapshot_during_concurrent_finalization(self):
        owner = Store(self.home)
        writer = Store(self.home)
        self.addCleanup(owner.close)
        self.addCleanup(writer.close)
        control = WatchControl(owner, "request-snapshot")
        session = owner.start_session("test", [self.root], "poll")
        control.ready(session)

        def finalized(store, name):
            result = request_stop(store, name)
            writer.end_session(session, 0)
            return result

        with patch("agentgc.watch_control.request_stop", side_effect=finalized):
            result, code = control_command(self.home, "request-snapshot", stop=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["session_status"], "running")
        self.assertEqual(result["state"], "stopping")
        self.assertEqual(watch_status(owner, "request-snapshot")["status"], "finished")
