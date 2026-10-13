"""Track explicitly selected roots and keep attribution uncertainty visible."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .native import make_backend
from .paths import Scope, SafetyError, regular, signature
from .store import Store
from .tags import write_tag
from .watch_control import WatchControl
from .output import inline


class Tracker:
    def __init__(self, store: Store, scope: Scope, session: str, tag_files: bool = False):
        self.store, self.scope, self.session = store, scope, session
        self.tag_files = tag_files
        self.errors: list[str] = []
        self.last_gap: str | None = None

    def sync(self, path: Path, source: str = "session-correlated", token: str | None = None) -> None:
        if not self.scope.includes(path):
            return
        old = self.store.db.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
        try:
            info = regular(path)
        except FileNotFoundError:
            if old is not None and old["present"]:
                self.store.db.execute(
                    "UPDATE files SET present=0,changed=?,activity=activity+1,review_hash=NULL,"
                    "review_signature=NULL,label=CASE WHEN label='disposable' THEN 'review' ELSE label END WHERE id=?",
                    (time.time(), old["id"]),
                )
                self.store.event(str(path), "deleted", source, self.session, old["id"], old["size"])
            return
        except (OSError, SafetyError) as exc:
            if len(self.errors) < 20:
                self.errors.append(f"{path}: {exc}")
            return
        stamp = signature(info)
        now = time.time()
        root = str(self.scope.root_for(path))
        if old is None:
            cursor = self.store.db.execute(
                "INSERT INTO files(path,root,size,signature,first_seen,changed,activity,session_id,source,scan_token) "
                "VALUES(?,?,?,?,?,?,1,?,?,?)",
                (str(path), root, info.st_size, stamp, now, now, self.session, source, token),
            )
            fid = cursor.lastrowid
            kind = "observed" if source == "baseline" else "created"
        elif not old["present"] or old["signature"] != stamp:
            fid = old["id"]
            kind = "created" if not old["present"] else "modified"
            self.store.db.execute(
                "UPDATE files SET root=?,size=?,signature=?,present=1,changed=?,activity=activity+1,"
                "session_id=?,source=?,scan_token=?,review_hash=NULL,review_signature=NULL,"
                "label=CASE WHEN label='keep' THEN 'keep' ELSE 'review' END WHERE id=?",
                (root, info.st_size, stamp, now, self.session, source, token, fid),
            )
        else:
            if token:
                self.store.db.execute("UPDATE files SET scan_token=?,root=? WHERE id=?", (token, root, old["id"]))
            return
        self.store.event(str(path), kind, source, self.session, fid, info.st_size)
        if self.tag_files and source != "baseline":
            row = self.store.db.execute("SELECT label FROM files WHERE id=?", (fid,)).fetchone()
            status = write_tag(path, self.store.tag_payload(fid, row["label"], source, self.session))
            self.store.db.execute("UPDATE files SET tag_status=? WHERE id=?", (status, fid))

    def scan(self, source: str = "session-correlated") -> None:
        token = str(uuid.uuid4())
        self.errors = []
        with self.store.transaction():
            for root in self.scope.roots:
                for path in self.scope.walk(root, self.errors):
                    self.sync(path, source, token)
            # A failed walk is not evidence of deletion. Suppress all missing-file
            # reconciliation for this pass rather than erasing inaccessible paths.
            if not self.errors:
                rows = self.store.db.execute(
                    "SELECT path FROM files WHERE present=1 AND (scan_token IS NULL OR scan_token<>?)", (token,)
                )
                for row in rows:
                    path = Path(row["path"])
                    if self.scope.includes(path):
                        self.sync(path, source, token)
            if self.errors:
                self.gap("Incomplete scan: " + "; ".join(self.errors))
            else:
                self.last_gap = None

    def gap(self, detail: str) -> None:
        if detail == self.last_gap:
            return
        self.last_gap = detail
        self.store.db.execute("UPDATE sessions SET gaps=gaps+1 WHERE id=?", (self.session,))
        self.store.event("", "gap", "tracker", self.session, detail=detail)
        print(f"- agcc gap: {inline(detail)}", file=sys.stderr)


def track(store: Store, scope: Scope, agent: str, backend: str = "auto", interval: float = 1,
          reconcile: float = 60, command: list[str] | None = None, duration: float | None = None,
          tag_files: bool = False, control: WatchControl | None = None) -> tuple[int, str]:
    if interval < 0.1 or reconcile < interval or (duration is not None and duration <= 0):
        raise SafetyError("interval must be >= 0.1; reconcile >= interval; duration > 0")
    observer, warning = make_backend(scope, backend)
    if warning:
        print(f"- agcc warning: {inline(warning)}", file=sys.stderr)
    sid = store.start_session(agent, scope.roots, observer.name if observer else "poll",
                              Path(command[0]).name if command else None)
    tracker = Tracker(store, scope, sid, tag_files)
    process: subprocess.Popen | None = None
    code, interrupted, pending_scan = 0, False, False
    final_failed = False
    old_handlers: dict = {}

    def stop(signum, _frame):
        nonlocal interrupted, code
        interrupted, code = True, 128 + signum
        if process is not None and process.poll() is None:
            try:
                # Keep the child's process group and inherited terminal intact.
                # On SIGINT terminals normally signal both; forwarding is harmless.
                process.send_signal(signum if os.name != "nt" else signal.SIGTERM)
            except ProcessLookupError:
                pass

    try:
        tracker.scan("baseline")
        if tracker.errors:
            raise SafetyError("Baseline scan was incomplete; command was not started")
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, stop)
        if command:
            env = dict(os.environ, AGENTGC_SESSION=sid, AGENTGC_HOME=str(store.home))
            process = subprocess.Popen(command, env=env)  # No shell; preserve argv and exit status.
        print(f"agcc: session {sid}; backend {observer.name if observer else 'poll'}", file=sys.stderr)
        if control:
            control.ready(sid)
        start = last_scan = last_beat = time.monotonic()
        while not interrupted:
            if control and control.stopping():
                break
            if process is not None and process.poll() is not None:
                code = process.returncode
                break
            now = time.monotonic()
            if duration is not None and now - start >= duration:
                break
            timeout = min(interval, 0.25) if process else min(interval, 1)
            if duration is not None:
                timeout = min(timeout, max(0, duration - (now - start)))
            if observer:
                notices, dirty, overflow = observer.read(timeout)
                pending_scan |= dirty
                with store.transaction():
                    for path, kind in notices:
                        tracker.sync(path)
                        # Retain evidence of short-lived files even if their metadata
                        # is already gone. Do not invent a size or process identity.
                        if not path.exists():
                            store.event(str(path), kind + "-unresolved", "filesystem-event", sid)
                    if tracker.errors:
                        tracker.gap("Incomplete event read: " + "; ".join(tracker.errors))
                        tracker.errors = []
                    if overflow:
                        tracker.gap("Native event queue overflow; reconciling surviving files")
            else:
                time.sleep(timeout)
            now = time.monotonic()
            due = interval if observer is None or pending_scan else reconcile
            if now - last_scan >= due:
                tracker.scan()
                if observer and pending_scan:
                    try:
                        observer.refresh()
                    except OSError as exc:
                        tracker.gap(f"Native watch refresh failed; switching to polling: {exc}")
                        observer.close()
                        observer = None
                        store.db.execute("UPDATE sessions SET backend='poll' WHERE id=?", (sid,))
                pending_scan, last_scan = False, now
            if now - last_beat >= 5:
                store.db.execute("UPDATE sessions SET heartbeat=? WHERE id=?", (time.time(), sid))
                last_beat = now
        if interrupted and process is not None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    except BaseException:
        code = 1
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        try:
            if observer:
                # Each read is bounded. Drain queued batches, but do not let an
                # unrelated continuing writer postpone shutdown indefinitely.
                deadline = time.monotonic() + 1
                try:
                    for batch in range(32):
                        notices, _, overflow = observer.read(0)
                        with store.transaction():
                            for path, kind in notices:
                                tracker.sync(path)
                                if not path.exists():
                                    store.event(str(path), kind + "-unresolved", "filesystem-event", sid)
                            if tracker.errors:
                                tracker.gap("Incomplete final event read: " + "; ".join(tracker.errors))
                                tracker.errors = []
                                final_failed = True
                            if overflow:
                                tracker.gap("Queue overflow at session end")
                        if not observer.pending():
                            break
                        if batch == 31 or time.monotonic() >= deadline:
                            tracker.gap("Native event drain limit reached at session end; queued events may be lost")
                            final_failed = True
                            break
                except OSError as exc:
                    tracker.gap(f"Final native event read failed: {exc}")
                    final_failed = True
            # Even a failed native read must not suppress reconciliation.
            tracker.scan()
            final_failed |= bool(tracker.errors)
        except BaseException:
            final_failed = True
            raise
        finally:
            if observer:
                observer.close()
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            if final_failed and command is None and not interrupted:
                code = 1
            store.end_session(sid, code, "interrupted" if interrupted else
                              "failed" if final_failed or code != 0 else "finished")
    return (128 - code if code < 0 else code), sid


def scan_once(store: Store, scope: Scope) -> dict:
    sid = store.start_session("scan", scope.roots, "snapshot")
    tracker = Tracker(store, scope, sid)
    try:
        tracker.scan("baseline")
    finally:
        store.end_session(sid, 1 if tracker.errors else 0)
    return {"session": sid, "errors": tracker.errors, **store.stats()}
