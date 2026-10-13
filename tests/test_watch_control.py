from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from agentgc.paths import SafetyError
from agentgc.store import Store
from agentgc.watch_control import WatchControl, request_stop


class ControlledWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.store = Store(self.base / "state")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_control_names_cannot_escape_or_be_reused(self):
        for name in ("", "../outside", "/absolute", "a/b", "x" * 65, "中文"):
            with self.assertRaises(SafetyError):
                WatchControl(self.store, name)
        WatchControl(self.store, "task-1")
        with self.assertRaises(FileExistsError):
            WatchControl(self.store, "task-1")

    def test_stop_request_does_not_close_session(self):
        control = WatchControl(self.store, "task")
        sid = self.store.start_session("test", [self.root], "poll")
        control.ready(sid)
        self.assertTrue(request_stop(self.store, "task")["stop_requested"])
        self.assertTrue(request_stop(self.store, "task")["stop_requested"])
        self.assertTrue(control.stopping())
        self.assertEqual(self.store.stats()["active_sessions"], 1)
        self.store.end_session(sid, 0)
        with self.assertRaises(SafetyError):
            request_stop(self.store, "task")

    def test_stop_requires_valid_readiness(self):
        control = WatchControl(self.store, "task")
        with self.assertRaises(FileNotFoundError):
            request_stop(self.store, "task")
        ready = control.path / "ready.json"
        for content in ("[]", "{}", '{"session": 1}', " " * 4097):
            ready.write_text(content)
            with self.assertRaises(SafetyError):
                request_stop(self.store, "task")

    def test_linked_control_parent_is_rejected(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        try:
            (self.store.home / "watches").symlink_to(elsewhere, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(SafetyError):
            WatchControl(self.store, "task")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_linked_stop_file_is_rejected(self):
        control = WatchControl(self.store, "task")
        sid = self.store.start_session("test", [self.root], "poll")
        control.ready(sid)
        target = self.root / "user-file"
        target.write_text("preserve")
        try:
            (control.path / "stop").symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(SafetyError):
            request_stop(self.store, "task")
        with self.assertRaises(SafetyError):
            control.stopping()
        self.assertEqual(target.read_text(), "preserve")

    def test_real_watcher_ready_stop_and_final_scan(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        process = subprocess.Popen(
            [sys.executable, "-m", "agentgc", "--home", str(self.store.home), "--json",
             "watch", "--root", str(self.root), "--control", "task", "--backend", "poll",
             "--interval", "0.1", "--duration", "10"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            ready = self.store.home / "watches/task/ready.json"
            deadline = time.monotonic() + 5
            record = None
            while time.monotonic() < deadline:
                if ready.exists():
                    try:
                        record = json.loads(ready.read_text())
                        break
                    except json.JSONDecodeError:
                        pass
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(record, "watcher did not become ready")
            output = self.root / "output.txt"
            output.write_text("final result")
            stopped = request_stop(self.store, "task")
            self.assertEqual(stopped["session"], record["session"])
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["session"], record["session"])
            row = self.store.db.execute("SELECT status FROM sessions WHERE id=?", (record["session"],)).fetchone()
            self.assertEqual(row["status"], "finished")
            self.assertEqual(self.store.file(output)["size"], len("final result"))
            self.assertEqual(self.store.file(output)["source"], "session-correlated")
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)

    def test_failed_baseline_never_publishes_readiness(self):
        from unittest.mock import patch
        from agentgc.paths import Scope
        from agentgc.tracker import track
        control = WatchControl(self.store, "task")

        def incomplete(tracker, source="session-correlated"):
            tracker.errors = ["unreadable directory"]

        with patch("agentgc.tracker.Tracker.scan", incomplete):
            with self.assertRaisesRegex(SafetyError, "Baseline scan was incomplete"):
                track(self.store, Scope([self.root], self.store.home), "test",
                      backend="poll", duration=0.1, control=control)
        self.assertFalse((control.path / "ready.json").exists())
        self.assertEqual(self.store.stats()["active_sessions"], 0)

    def test_bundled_skill_requires_proactive_tracking_and_safe_cleanup(self):
        from importlib.resources import files
        skill = files("agentgc").joinpath("skills/agentgc/SKILL.md").read_text()
        for phrase in ("before editing code or documents", "watch-stop", "ready.json",
                       "Do not quarantine automatically", "not cleanup", "Read-only questions"):
            self.assertIn(phrase, skill)
