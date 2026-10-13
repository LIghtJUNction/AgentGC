from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agentgc.cli import main
from agentgc.cleanup import mark, plan, quarantine_one, recover, restore
from agentgc.native import Inotify, make_backend
from agentgc.paths import SafetyError, Scope
from agentgc.store import Store
from agentgc.tags import write_tag
from agentgc.tracker import Tracker, scan_once, track


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.home = self.base / "state"
        self.store = Store(self.home)
        self.scope = Scope([self.root], self.home)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def file(self, name="scratch.txt", text="hello"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def scan(self):
        return scan_once(self.store, self.scope)

    def reviewed(self, name="scratch.txt"):
        path = self.file(name)
        self.scan()
        mark(self.store, path, "disposable", "User reviewed: no remaining references")
        return path

    def link(self, target, path, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--home", str(self.home), "--json", *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_baseline_does_not_claim_agent_created_existing_files(self):
        path = self.file()
        result = self.scan()
        row = self.store.file(path)
        self.assertEqual(row["source"], "baseline")
        self.assertEqual(row["label"], "review")
        self.assertEqual(result["files"], 1)
        event = self.store.db.execute("SELECT * FROM events").fetchone()
        self.assertEqual(event["kind"], "observed")

    def test_unchanged_scans_do_not_add_events(self):
        self.file()
        self.scan()
        before = self.store.stats()["events"]
        for _ in range(3):
            self.scan()
        self.assertEqual(self.store.stats()["events"], before)

    def test_create_modify_delete_and_recreate(self):
        sid = self.store.start_session("test", self.scope.roots, "poll")
        tracker = Tracker(self.store, self.scope, sid)
        path = self.file()
        tracker.scan()
        path.write_text("longer content")
        tracker.scan()
        path.unlink()
        tracker.scan()
        self.assertEqual(self.store.file(path)["present"], 0)
        self.file()
        tracker.scan()
        kinds = [r[0] for r in self.store.db.execute("SELECT kind FROM events")]
        self.assertEqual(kinds, ["created", "modified", "deleted", "created"])
        self.assertEqual(self.store.file(path)["source"], "session-correlated")
        self.store.end_session(sid, 0)

    def test_modification_invalidates_disposable_but_preserves_keep(self):
        path = self.reviewed()
        path.write_text("new content")
        self.scan()
        self.assertEqual(self.store.file(path)["label"], "review")
        self.assertIsNone(self.store.file(path)["review_hash"])
        mark(self.store, path, "keep")
        path.write_text("another version")
        self.scan()
        self.assertEqual(self.store.file(path)["label"], "keep")

    def test_size_sort_activity_sort_and_literal_search(self):
        small = self.file("a%_file", "a")
        self.file("large", "b" * 200)
        self.file("ab_file", "cc")
        self.scan()
        self.assertEqual(self.store.list_files(sort="size")[0]["size"], 200)
        self.assertEqual(len(self.store.list_files("%_")), 1)
        small.write_text("changed")
        self.scan()
        self.assertEqual(self.store.list_files(sort="activity")[0]["path"], str(small))
        self.assertEqual(len(self.store.list_files(limit=1, offset=1)), 1)
        with self.assertRaises(SafetyError):
            self.store.list_files(sort="size; DROP TABLE files")

    def test_session_search_includes_retained_events_not_just_latest_session(self):
        path = self.file()
        first = self.scan()["session"]
        path.write_text("changed")
        self.scan()
        self.assertEqual(len(self.store.list_files(session=first)), 1)

    def test_session_membership_uses_composite_index_in_existing_store(self):
        matching = self.file("matching")
        absent = self.file("absent")
        baseline = self.scan()["session"]
        target = self.store.start_session("target", self.scope.roots, "poll")
        match_id = self.store.file(matching)["id"]
        absent_id = self.store.file(absent)["id"]
        with self.store.transaction():
            for _ in range(500):
                self.store.event(str(absent), "modified", "test", baseline, absent_id)
            self.store.event(str(matching), "modified", "test", target, match_id)
        self.store.end_session(target, 0)
        # Simulate an older schema-v1 database, then reopen to check additive upgrade.
        self.store.db.execute("DROP INDEX events_file_session")
        self.store.close()
        self.store = Store(self.home)
        self.assertEqual([r["path"] for r in self.store.list_files(session=target)], [str(matching)])
        self.assertEqual(self.store.list_files(session=target, offset=1), [])
        plan_rows = self.store.db.execute(
            "EXPLAIN QUERY PLAN SELECT f.id FROM files f WHERE EXISTS "
            "(SELECT 1 FROM events e WHERE e.file_id=f.id AND e.session_id=?)", (target,)
        )
        details = [r["detail"] for r in plan_rows]
        self.assertTrue(any("events_file_session" in d and "file_id=?" in d and "session_id=?" in d
                            for d in details), details)

    def test_overlap_roots_are_deduplicated(self):
        nested = self.root / "inner"
        nested.mkdir()
        scope = Scope([nested, self.root, self.root], self.home)
        self.assertEqual(scope.roots, [self.root])

    def test_excludes_and_self_index_are_not_tracked(self):
        self.file(".git/config")
        self.file(".agentgc-quarantine/file")
        self.file("cache/data")
        self.file("good")
        inner = Store(self.root / ".agentgc")
        try:
            scope = Scope([self.root], inner.home, ["cache"])
            scan_once(inner, scope)
            self.assertEqual([r["path"] for r in inner.list_files()], [str(self.root / "good")])
        finally:
            inner.close()

    def test_symlink_and_symlink_directory_are_not_indexed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_text("do not touch")
        self.link(outside, self.root / "linked", True)
        self.link(outside / "secret", self.root / "linked-file")
        self.scan()
        self.assertEqual(self.store.stats()["files"], 0)

    def test_read_failure_does_not_mark_other_paths_deleted(self):
        path = self.file()
        self.scan()
        path.unlink()
        original = self.scope.walk
        def failed_walk(root, errors, directories=False):
            errors.append("permission denied")
            yield from original(root, errors, directories)
        with patch.object(self.scope, "walk", failed_walk):
            result = self.scan()
        self.assertTrue(result["errors"])
        self.assertEqual(self.store.file(path)["present"], 1)
        self.scan()
        self.assertEqual(self.store.file(path)["present"], 0)

    def test_empty_scope_and_bad_root_fail(self):
        with self.assertRaises(SafetyError):
            Scope([], self.home)
        with self.assertRaises(SafetyError):
            Scope([self.home], self.home)

    def test_disposable_requires_note(self):
        path = self.file()
        self.scan()
        with self.assertRaises(SafetyError):
            mark(self.store, path, "disposable")

    def test_temporary_and_keep_do_not_authorize_cleanup(self):
        path = self.file()
        self.scan()
        for label in ("keep", "temporary", "review"):
            mark(self.store, path, label)
            self.assertFalse(plan(self.store, [str(path)])["items"][0]["eligible"])
            self.assertEqual(plan(self.store)["items"], [])

    def test_dry_run_is_non_destructive(self):
        path = self.reviewed()
        result = plan(self.store)
        self.assertEqual(result["bytes_to_quarantine"], 5)
        self.assertEqual(result["bytes_freed"], 0)
        self.assertTrue(path.exists())
        self.assertFalse((path.parent / ".agentgc-quarantine").exists())

    def test_quarantine_and_restore_without_content_loss(self):
        path = self.reviewed()
        item = quarantine_one(self.store, path)
        self.assertFalse(path.exists())
        self.assertEqual(Path(item["destination"]).read_text(), "hello")
        self.assertEqual(self.store.file(path)["present"], 0)
        result = restore(self.store, item["id"])
        self.assertEqual(path.read_text(), "hello")
        self.assertEqual(result["label"], "keep")
        self.assertFalse(Path(item["destination"]).exists())
        with self.assertRaises(SafetyError):
            restore(self.store, item["id"])

    def test_restore_never_overwrites(self):
        path = self.reviewed()
        item = quarantine_one(self.store, path)
        path.write_text("new important output")
        with self.assertRaises(SafetyError):
            restore(self.store, item["id"])
        self.assertEqual(path.read_text(), "new important output")
        self.assertTrue(Path(item["destination"]).exists())

    def test_digest_catches_same_size_mtime_restored_changes(self):
        path = self.reviewed()
        old = path.stat()
        path.write_text("HELLO")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        result = plan(self.store)
        self.assertFalse(result["items"][0]["eligible"])
        with self.assertRaises(SafetyError):
            quarantine_one(self.store, path)
        self.assertEqual(path.read_text(), "HELLO")

    def test_replaced_path_fails_revalidation(self):
        path = self.reviewed()
        replacement = self.file("replacement", "different")
        os.replace(replacement, path)
        self.assertFalse(plan(self.store)["items"][0]["eligible"])

    def test_hard_link_is_protected(self):
        path = self.file()
        try:
            os.link(path, self.root / "second-link")
        except OSError:
            self.skipTest("Hard links unavailable")
        self.scan()
        with self.assertRaises(SafetyError):
            mark(self.store, path, "disposable", "reviewed")

    def test_symlink_replacement_does_not_touch_target(self):
        path = self.reviewed()
        outside = self.base / "valuable"
        outside.write_text("keep this")
        path.unlink()
        self.link(outside, path)
        self.assertFalse(plan(self.store)["items"][0]["eligible"])
        self.assertEqual(outside.read_text(), "keep this")

    def test_parent_symlink_replacement_fails_safe(self):
        path = self.reviewed("nested/file")
        moved = self.root / "original-directory"
        path.parent.rename(moved)
        outside = self.base / "external"
        outside.mkdir()
        (outside / "file").write_text("external")
        self.link(outside, self.root / "nested", True)
        self.assertFalse(plan(self.store)["items"][0]["eligible"])
        self.assertEqual((outside / "file").read_text(), "external")
        self.assertEqual((moved / "file").read_text(), "hello")

    @unittest.skipUnless(shutil.which("git"), "git unavailable")
    def test_git_tracked_files_are_protected(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        path = self.file()
        subprocess.run(["git", "-C", str(self.root), "add", "--", path.name], check=True)
        self.scan()
        with self.assertRaises(SafetyError):
            mark(self.store, path, "disposable", "reviewed")
        untracked = self.file("untracked")
        self.scan()
        mark(self.store, untracked, "disposable", "reviewed")

    def test_active_and_stale_sessions_block_cleanup(self):
        path = self.reviewed()
        sid = self.store.start_session("active", self.scope.roots, "poll")
        self.store.db.execute("UPDATE sessions SET heartbeat=0 WHERE id=?", (sid,))
        self.assertFalse(plan(self.store)["items"][0]["eligible"])
        self.store.end_session(sid, 0)
        self.assertTrue(plan(self.store)["items"][0]["eligible"])

    def test_changed_quarantine_is_not_restored(self):
        item = quarantine_one(self.store, self.reviewed())
        Path(item["destination"]).write_text("unexpected content")
        with self.assertRaises(SafetyError):
            restore(self.store, item["id"])

    def test_recover_prepared_move_after_rename(self):
        path = self.reviewed()
        item = quarantine_one(self.store, path)
        self.store.db.execute("UPDATE quarantine SET status='prepared' WHERE id=?", (item["id"],))
        self.store.db.execute("UPDATE files SET present=1 WHERE path=?", (str(path),))
        preview = recover(self.store)
        self.assertEqual(preview[0]["status"], "quarantined")
        self.assertEqual(self.store.file(path)["present"], 1)
        recover(self.store, True)
        self.assertEqual(self.store.file(path)["present"], 0)
        restore(self.store, item["id"])
        self.assertTrue(path.exists())

    def test_symlinked_quarantine_directory_is_rejected(self):
        path = self.reviewed()
        target = self.base / "other"
        target.mkdir()
        self.link(target, self.root / ".agentgc-quarantine", True)
        with self.assertRaises(SafetyError):
            quarantine_one(self.store, path)
        self.assertTrue(path.exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_database_persists_and_rejects_future_schema(self):
        path = self.file()
        self.scan()
        other = Store(self.home)
        try:
            self.assertEqual(other.file(path)["size"], 5)
        finally:
            other.close()
        self.store.db.execute("PRAGMA user_version=99")
        with self.assertRaises(SafetyError):
            Store(self.home)

    def test_tag_failure_does_not_break_tracking(self):
        with patch("agentgc.tags.regular", side_effect=PermissionError("denied")):
            self.assertTrue(write_tag(self.root / "missing", {"v": 1}).startswith("unavailable"))
        sid = self.store.start_session("test", self.scope.roots, "poll")
        path = self.file()
        with patch("agentgc.tracker.write_tag", return_value="unavailable"):
            Tracker(self.store, self.scope, sid, True).scan()
        self.assertEqual(self.store.file(path)["tag_status"], "unavailable")
        self.store.end_session(sid, 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux xattrs")
    def test_xattr_tag_does_not_create_a_tracking_loop(self):
        path = self.file()
        sid = self.store.start_session("test", self.scope.roots, "poll")
        tracker = Tracker(self.store, self.scope, sid, True)
        tracker.scan()
        before = self.store.stats()["events"]
        tracker.scan()
        self.assertEqual(self.store.stats()["events"], before)
        if self.store.file(path)["tag_status"] == "xattr":
            self.assertEqual(json.loads(os.getxattr(path, "user.agentgc"))["source"], "session-correlated")
        self.store.end_session(sid, 0)

    def test_cli_json_and_error_output(self):
        self.file("测试 file.txt")
        code, out, err = self.cli("scan", "--root", self.root)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["files"], 1)
        code, out, err = self.cli("list")
        self.assertEqual(len(json.loads(out)), 1)
        code, out, err = self.cli("mark", self.root / "missing", "keep")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error", json.loads(err))

    def test_cli_apply_requires_explicit_unused_confirmation(self):
        path = self.reviewed()
        code, _, err = self.cli("gc", "--apply")
        self.assertEqual(code, 2)
        self.assertIn("confirm-unused", err)
        self.assertTrue(path.exists())
        code, out, err = self.cli("gc", "--apply", "--confirm-unused")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["moved"]), 1)

    def test_cli_skill_install_does_not_overwrite(self):
        dest = self.base / "skills"
        code, _, err = self.cli("skill", "--install", dest)
        self.assertEqual(code, 0, err)
        path = dest / "agentgc/SKILL.md"
        path.write_text("customized")
        code, _, _ = self.cli("skill", "--install", dest)
        self.assertEqual(code, 2)
        self.assertEqual(path.read_text(), "customized")

    def test_cli_prune_keeps_file_index_and_active_session_events(self):
        self.file()
        self.scan()
        sid = self.store.start_session("active", self.scope.roots, "poll")
        self.store.event("active", "gap", "tracker", sid)
        self.store.db.execute("UPDATE events SET at=0")
        code, out, err = self.cli("prune", "--days", "1", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["events"], 1)
        self.assertEqual(self.store.stats()["files"], 1)
        self.assertEqual(self.store.stats()["events"], 1)
        self.store.end_session(sid, 0)

    def test_cli_rejects_invalid_ranges(self):
        for args in (("list", "--limit", "-1"), ("history", "--limit", "0"), ("gc", "--limit", "0")):
            code, _, _ = self.cli(*args)
            self.assertEqual(code, 2)

    def test_poll_wrapper_preserves_exit_and_captures_fast_outputs(self):
        path = self.root / "output"
        command = [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('result'); sys.exit(7)", str(path)]
        code, sid = track(self.store, self.scope, "demo", backend="poll", interval=0.1, command=command)
        self.assertEqual(code, 7)
        self.assertEqual(self.store.file(path)["size"], 6)
        row = self.store.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["exit_code"], 7)
        self.assertEqual(row["command"], Path(sys.executable).name)
        self.assertNotIn("write_text", row["command"])

    def test_failed_spawn_closes_session(self):
        with self.assertRaises(FileNotFoundError):
            track(self.store, self.scope, "demo", backend="poll", command=[str(self.base / "nonexistent-executable")])
        self.assertEqual(self.store.stats()["active_sessions"], 0)

    def test_poll_watch_stops_after_duration(self):
        start = time.monotonic()
        code, _ = track(self.store, self.scope, "demo", backend="poll", interval=0.1, duration=0.2)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - start, 3)

    def test_auto_backend_fallback_and_explicit_failure(self):
        with patch("agentgc.native.Inotify", side_effect=OSError("watch limit")):
            backend, warning = make_backend(self.scope, "auto")
            self.assertIsNone(backend)
            self.assertIn("polling", warning)
            with self.assertRaises(SafetyError):
                make_backend(self.scope, "native")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux native backend")
    def test_native_notices_include_short_lived_file(self):
        backend = Inotify(self.scope)
        try:
            path = self.file("ephemeral")
            path.unlink()
            notices, _, _ = backend.read(1)
            self.assertIn((path, "created"), notices)
            self.assertIn((path, "deleted"), notices)
        finally:
            backend.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux native backend")
    def test_native_wrapper_tracks_renamed_directory_and_final_file(self):
        target = self.root / "renamed/final"
        script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); (p/'old').mkdir(); (p/'old'/'final').write_text('x'); (p/'old').rename(p/'renamed')"
        code, _ = track(self.store, self.scope, "native-test", command=[sys.executable, "-c", script, str(self.root)], backend="native")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.file(target)["size"], 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux native backend")
    def test_native_watch_limit_fails_and_closes_descriptor(self):
        (self.root / "sub").mkdir()
        with self.assertRaises(OSError):
            Inotify(self.scope, max_watches=1)

    def test_tracker_does_not_read_file_content_during_scans(self):
        self.file()
        with patch("agentgc.paths.digest", side_effect=AssertionError("content read")):
            self.scan()
            self.scan()

    def subprocess_cli(self, *args, **kwargs):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return subprocess.Popen([sys.executable, "-m", "agentgc", "--home", str(self.home), *map(str, args)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)

    def test_real_cli_run_preserves_stdout_and_passes_session_environment(self):
        output = self.root / "session-id"
        script = "import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(os.environ['AGENTGC_SESSION']); print('child-only')"
        process = self.subprocess_cli("--json", "run", "--root", self.root, "--backend", "poll", "--",
                                      sys.executable, "-c", script, output)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "child-only\n")
        sid = output.read_text()
        self.assertIsNotNone(self.store.db.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone())
        self.assertEqual(self.store.file(output)["source"], "session-correlated")

    @unittest.skipIf(os.name == "nt", "POSIX termination signal")
    def test_sigterm_final_scan_and_exit_status(self):
        output = self.root / "started"
        script = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
        process = self.subprocess_cli("run", "--root", self.root, "--backend", "poll", "--",
                                      sys.executable, "-c", script, output)
        try:
            deadline = time.monotonic() + 5
            while not output.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(output.exists())
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 143, stderr)
            self.assertEqual(self.store.file(output)["size"], 5)
            self.assertEqual(self.store.stats()["active_sessions"], 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    @unittest.skipIf(os.name == "nt", "POSIX SIGKILL")
    def test_killed_watcher_remains_active_until_user_confirms_stop(self):
        self.file()
        process = self.subprocess_cli("watch", "--root", self.root, "--backend", "poll")
        try:
            deadline = time.monotonic() + 5
            sid = None
            while time.monotonic() < deadline:
                row = self.store.db.execute("SELECT id FROM sessions WHERE status='running'").fetchone()
                if row:
                    sid = row[0]
                    break
                time.sleep(0.02)
            self.assertIsNotNone(sid)
            process.kill()
            process.communicate(timeout=5)
            self.assertEqual(self.store.stats()["active_sessions"], 1)
            code, _, _ = self.cli("end-session", sid)
            self.assertEqual(code, 2)
            code, _, err = self.cli("end-session", sid, "--confirm-stopped")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.store.stats()["active_sessions"], 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux native backend")
    def test_native_wrapper_retains_transient_event_without_inventing_size(self):
        path = self.root / "gone"
        script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text('x'); p.unlink()"
        code, sid = track(self.store, self.scope, "demo", backend="native",
                          command=[sys.executable, "-c", script, str(path)])
        self.assertEqual(code, 0)
        rows = list(self.store.db.execute("SELECT * FROM events WHERE path=?", (str(path),)))
        self.assertTrue(any(r["kind"] == "created-unresolved" for r in rows))
        for row in rows:
            if row["kind"].endswith("-unresolved"):
                self.assertIsNone(row["size"])
                self.assertEqual(row["session_id"], sid)

    def test_rollback_does_not_leave_partial_index_changes(self):
        path = self.file()
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.event(str(path), "test", "test")
                raise RuntimeError("simulated crash")
        self.assertEqual(self.store.stats()["events"], 0)

    def test_globals_after_subcommand_are_supported(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["stats", "--home", str(self.home), "--json"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["files"], 0)

    def test_disposable_revalidation_rejects_newly_started_session(self):
        path = self.reviewed()
        from agentgc.cleanup import validate_candidate
        calls = 0
        def raced(store, row):
            nonlocal calls
            calls += 1
            if calls == 2:
                store.start_session("new writer", self.scope.roots, "poll")
            return validate_candidate(store, row)
        with patch("agentgc.cleanup.validate_candidate", side_effect=raced):
            with self.assertRaises(SafetyError):
                quarantine_one(self.store, path)
        self.assertTrue(path.exists())
        self.assertEqual(recover(self.store)[0]["status"], "cancelled")

    def test_cleanup_rejects_unsupported_restore_links_without_losing_copy(self):
        path = self.reviewed()
        item = quarantine_one(self.store, path)
        with patch("agentgc.cleanup.os.link", side_effect=OSError("not supported")):
            with self.assertRaises(OSError):
                restore(self.store, item["id"])
        self.assertFalse(path.exists())
        self.assertEqual(Path(item["destination"]).read_text(), "hello")


if __name__ == "__main__":
    unittest.main()
