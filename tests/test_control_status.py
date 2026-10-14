from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from process_helpers import stop_process

from agentgc.cli import main, parser
from agentgc.paths import SafetyError, Scope
from agentgc.store import Store
from agentgc.watch_control import WatchControl, request_stop, wait_status, watch_status


class ControlStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)
        self.scope = Scope([self.root], self.store.home, ["ignored"])

    def ready(self):
        control = WatchControl(self.store, "task")
        sid = self.store.start_session("test", [self.root], "poll")
        control.ready(sid, self.scope, 60)
        return control, sid

    def test_atomic_readiness_includes_coverage_and_never_overwrites(self):
        control = WatchControl(self.store, "task")
        sid = self.store.start_session("test", [self.root], "poll")
        publish = os.link
        def complete(source, destination):
            self.assertFalse(destination.exists())
            record = json.loads(source.read_text())
            self.assertEqual(record["roots"], [str(self.root)])
            self.assertEqual(record["excludes"], ["ignored"])
            publish(source, destination)
        with patch("agentgc.watch_control.os.link", side_effect=complete):
            control.ready(sid, self.scope, 60)
        self.assertFalse((control.path / "ready.tmp").exists())
        original = (control.path / "ready.json").read_bytes()
        with self.assertRaises(FileExistsError):
            control.ready(sid, self.scope, 60)
        self.assertEqual((control.path / "ready.json").read_bytes(), original)
        status = watch_status(self.store, "task")
        self.assertTrue(status["ready"])
        self.assertTrue(status["coverage_known"])
        self.assertEqual(status["excludes"], ["ignored"])

    def test_stale_and_expiring_health_never_close_a_session(self):
        control, sid = self.ready()
        self.store.db.execute("UPDATE sessions SET heartbeat=? WHERE id=?", (time.time()-100, sid))
        self.assertEqual(watch_status(self.store, "task")["state"], "stale")
        self.assertEqual(self.store.active_for(self.root), [sid])
        ready = control.path / "ready.json"
        record = json.loads(ready.read_text())
        record["expires_at"] = time.time()-1
        ready.write_text(json.dumps(record))
        self.assertEqual(watch_status(self.store, "task")["state"], "expiring")
        self.assertEqual(self.store.stats()["active_sessions"], 1)

    def test_invalid_identity_roots_and_linked_readiness_are_rejected(self):
        control, _ = self.ready()
        ready = control.path / "ready.json"
        record = json.loads(ready.read_text())
        for data in ({**record, "pid": record["pid"]+1}, {**record, "roots": []},
                     {**record, "excludes": "*"}, {**record, "expires_at": float("nan")},
                     {"session": record["session"]}, []):
            ready.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaises(SafetyError):
                watch_status(self.store, "task")
        ready.unlink()
        target = self.root / "unselected.json"
        target.write_text(json.dumps(record))
        try:
            ready.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(SafetyError):
            watch_status(self.store, "task")

    def test_legacy_readiness_cannot_establish_exclusion_coverage(self):
        control, sid = self.ready()
        (control.path / "ready.json").write_text(json.dumps({"session": sid, "pid": os.getpid()}))
        result = watch_status(self.store, "task")
        self.assertTrue(result["ready"])
        self.assertFalse(result["coverage_known"])
        self.assertIsNone(result["excludes"])

    def test_bounded_waits_timeout_without_mutating_or_faking_completion(self):
        self.assertTrue(wait_status(self.store, "missing", "ready", 0)["timed_out"])
        self.assertFalse((self.store.home / "watches/missing").exists())
        _, sid = self.ready()
        request_stop(self.store, "task")
        result = wait_status(self.store, "task", "finished", 0)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["state"], "stopping")
        self.assertEqual(self.store.stats()["active_sessions"], 1)
        self.store.end_session(sid, 1, "failed")
        result = wait_status(self.store, "task", "finished", 0)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["state"], "failed")
        for value in (-1, 301, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(SafetyError):
                wait_status(self.store, "task", "ready", value)

    def test_client_wait_backs_off_locally_and_emits_no_progress(self):
        clock = [0.0]
        sleeps = []
        def advance(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with patch("agentgc.watch_control.watch_status", return_value={"state": "starting", "ready": False}) as query, \
                patch("agentgc.watch_control.time.monotonic", side_effect=lambda: clock[0]), \
                patch("agentgc.watch_control.time.sleep", side_effect=advance):
            result = wait_status(self.store, "task", "ready", 4)
        self.assertTrue(result["timed_out"])
        self.assertEqual(query.call_count, 8)
        for actual, expected in zip(sleeps, (0.1, 0.2, 0.4, 0.8, 1, 1, 0.5)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(len(sleeps), 7)

    def test_cli_emits_one_result_and_nonzero_for_incomplete_wait(self):
        self.ready()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--home", str(self.store.home), "--json", "watch-status", "task", "--wait-ready", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertTrue(json.loads(out.getvalue())["ready"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--home", str(self.store.home), "--json", "watch-stop", "task", "--wait", "0.01"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(out.getvalue())["timed_out"])
        for flag, command in (("--wait", "watch-stop"), ("--wait-ready", "watch-status")):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args([command, "task", flag, "301"])

    def test_read_only_store_never_initializes_schema_or_allows_database_writes(self):
        statements = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch("agentgc.store.sqlite3.connect", side_effect=traced):
            reader = Store(self.store.home, read_only=True)
        try:
            self.assertEqual(reader.db.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(reader.db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(reader.stats()["files"], 0)
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("DELETE FROM sessions")
        finally:
            reader.close()
        self.assertFalse(any(sql.startswith(("CREATE", "INSERT", "UPDATE", "BEGIN")) for sql in statements))
        self.assertNotIn("PRAGMA journal_mode=WAL", statements)
        self.assertNotIn("PRAGMA user_version=1", statements)

    def test_cli_status_and_stop_waits_are_not_blocked_by_baseline_writer_lock(self):
        control = WatchControl(self.store, "task")
        sid = self.store.start_session("test", [self.root], "poll")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONDONTWRITEBYTECODE="1")
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            for command, flag in (("watch-status", "--wait-ready"), ("watch-stop", "--wait")):
                if command == "watch-stop":
                    control.ready(sid, self.scope, 60)
                start = time.monotonic()
                completed = subprocess.run(
                    [sys.executable, "-B", "-m", "agentgc", "--home", str(self.store.home), "--json",
                     command, "task", flag, "0.1"],
                    env=env, capture_output=True, text=True, timeout=3,
                )
                self.assertLess(time.monotonic()-start, 2)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertTrue(result["timed_out"])
                self.assertEqual(result["state"], "starting" if command == "watch-status" else "stopping")
                self.assertEqual(len(completed.stdout.splitlines()), 1)
        finally:
            self.store.db.execute("ROLLBACK")
        self.assertTrue(control.stopping())
        self.assertEqual(self.store.stats()["active_sessions"], 1)

    def test_missing_or_uninitialized_state_is_not_created_or_migrated_by_status(self):
        missing = self.base / "missing-state"
        pending = self.base / "pending-state"
        pending.mkdir()
        sqlite3.connect(pending / "index.sqlite3").close()
        for home in (missing, pending):
            out = io.StringIO()
            with self.subTest(home=home), contextlib.redirect_stdout(out):
                code = main(["--home", str(home), "--json", "watch-status", "task", "--wait-ready", "0.01"])
            self.assertEqual(code, 1)
            self.assertTrue(json.loads(out.getvalue())["timed_out"])
            self.assertEqual(json.loads(out.getvalue())["state"], "starting")
            self.assertFalse((home / "watches").exists())
        self.assertFalse(missing.exists())
        db = sqlite3.connect(pending / "index.sqlite3")
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)
        finally:
            db.close()

    def test_wait_budget_includes_read_only_store_opening(self):
        clock = [0.0]
        def opened(home, **kwargs):
            clock[0] += 0.2
            return Store(home, **kwargs)
        out = io.StringIO()
        with patch("agentgc.watch_control.Store", side_effect=opened), \
                patch("agentgc.watch_control.time.monotonic", side_effect=lambda: clock[0]), \
                patch("agentgc.watch_control.time.sleep", side_effect=AssertionError("wait budget was exceeded")), \
                contextlib.redirect_stdout(out):
            code = main(["--home", str(self.store.home), "--json", "watch-status", "task", "--wait-ready", "0.1"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(out.getvalue())["timed_out"])

    def test_manually_closed_sessions_are_terminal_but_not_success(self):
        _, sid = self.ready()
        self.store.end_session(sid, 1, "closed-by-user")
        with patch("agentgc.watch_control.time.sleep", side_effect=AssertionError("terminal state must not wait")):
            for goal in ("ready", "finished"):
                result = wait_status(self.store, "task", goal, 30)
                self.assertFalse(result["timed_out"])
                self.assertEqual(result["state"], "closed-by-user")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["--home", str(self.store.home), "--json", "watch-status", "task", "--wait-ready", "30"])
        self.assertEqual(code, 1)

    def start_native(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "agentgc", "--home", str(self.store.home), "--json",
             "watch", "--root", str(self.root), "--control", "task", "--backend", "native",
             "--reconcile", "300", "--duration", "30"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(stop_process, process)
        ready = wait_status(self.store, "task", "ready", 5)
        self.assertTrue(ready["ready"], ready)
        time.sleep(0.15)  # Let the watcher enter its maintenance-deadline wait.
        return process, ready

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux inotify required")
    def test_native_stop_wakes_idle_wait_and_finishes_final_scan(self):
        process, ready = self.start_native()
        path = self.root / "result"
        path.write_text("final result")
        start = time.monotonic()
        request_stop(self.store, "task")
        stdout, stderr = process.communicate(timeout=3)
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["session"], ready["session"])
        self.assertEqual(watch_status(self.store, "task")["state"], "finished")
        self.assertEqual(self.store.file(path)["size"], len("final result"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?",
                                              (str(self.store.home)+"%",)).fetchone()[0], 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux signal wakeup required")
    def test_sigterm_wakes_idle_wait_and_preserves_interrupted_status(self):
        process, _ = self.start_native()
        start = time.monotonic()
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=3)
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(process.returncode, 128+signal.SIGTERM, stderr)
        self.assertEqual(watch_status(self.store, "task")["state"], "interrupted")
