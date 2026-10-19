"""Track explicitly selected roots and keep attribution uncertainty visible."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .native import make_backend
from .diagnostics import classify
from .paths import Scope, SafetyError, regular, signature
from .store import Store
from .tags import write_tag
from .watch_control import WatchControl
from .output import inline
from .limits import TrackingLimit, TrackingLimits


class Tracker:
    def __init__(self, store: Store, scope: Scope, session: str, tag_files: bool = False,
                 limits: TrackingLimits | None = None):
        self.store, self.scope, self.session = store, scope, session
        self.tag_files = tag_files
        self.errors: list[str] = []
        self.last_gap: str | None = None
        self.limits = limits or TrackingLimits()
        self.limit_error: TrackingLimit | None = None

    def check_limits(self, files: int = 0) -> None:
        try:
            self.limits.check_scan(files)
            self.limits.check_state(self.store.home)
        except TrackingLimit as exc:
            self.limit_error = exc
            raise

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
            kind = ("link-boundary-notice" if isinstance(exc, SafetyError)
                    and "link or reparse" in str(exc).lower() else "coverage-error-notice")
            self.store.event(str(path), kind, source, self.session, detail=str(exc))
            return
        stamp = signature(info)
        now = time.time()
        root = str(self.scope.root_for(path))
        if old is None:
            cursor = self.store.db.execute(
                "INSERT INTO files(path,root,size,signature,first_seen,changed,activity,session_id,source,scan_token) "
                "VALUES(?,?,?,?,?,?,1,?,?,?)",
                (str(path), root, info.st_size, stamp, now, now, self.session, source, None),
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
                (root, info.st_size, stamp, now, self.session, source, None, fid),
            )
        else:
            if token:
                self.store.db.execute("INSERT OR IGNORE INTO temp.scan_seen VALUES(?)", (old["id"],))
            return
        if token:
            self.store.db.execute("INSERT OR IGNORE INTO temp.scan_seen VALUES(?)", (fid,))
        self.store.event(str(path), kind, source, self.session, fid, info.st_size)
        if self.tag_files and source != "baseline":
            row = self.store.db.execute("SELECT label FROM files WHERE id=?", (fid,)).fetchone()
            status = write_tag(path, self.store.tag_payload(fid, row["label"], source, self.session))
            self.store.db.execute("UPDATE files SET tag_status=? WHERE id=?", (status, fid))

    def scan(self, source: str = "session-correlated") -> None:
        token = str(uuid.uuid4())
        self.errors = []
        self.check_limits()
        # Only integer identities are needed to reconcile this scan. Rewriting a
        # UUID into every persistent row amplified idle scans into large WALs.
        self.store.db.execute("CREATE TEMP TABLE IF NOT EXISTS scan_seen(id INTEGER PRIMARY KEY)")
        self.store.db.execute("DELETE FROM temp.scan_seen")
        seen = 0
        # Walk outside the write transaction and bound each commit. A full tree
        # scan used to monopolize the shared WAL writer for longer than the busy
        # timeout, killing otherwise healthy watchers in unrelated directories.
        batch_size = 128
        batch: list[Path] = []
        for root in self.scope.roots:
            for path in self.scope.walk(root, self.errors):
                seen += 1
                # Count across all roots; never turn an unvisited tail into
                # missing-file evidence after reaching the scan budget.
                try:
                    self.limits.check_scan(seen)
                except TrackingLimit as exc:
                    self.limit_error = exc
                    raise
                batch.append(path)
                if len(batch) == batch_size:
                    self.check_limits(seen)
                    with self.store.transaction():
                        for candidate in batch:
                            self.sync(candidate, source, token)
                    batch.clear()
        if batch:
            self.check_limits(seen)
            with self.store.transaction():
                for candidate in batch:
                    self.sync(candidate, source, token)
        # Partial commits do not make an incomplete walk evidence of deletion.
        if not self.errors:
            # POSIX keeps indexed path ranges; Windows needs its case-insensitive
            # fallback. Materialize bounded pages so no read cursor/snapshot spans
            # commits. Re-stat candidates under the write lock before deletion.
            for root in ([None] if sys.platform == "win32" else self.scope.roots):
                after = ""
                while True:
                    where = "path>? AND present=1 AND id NOT IN (SELECT id FROM temp.scan_seen)"
                    params: tuple = (after,)
                    if root is not None:
                        prefix = str(root).rstrip(os.sep) + os.sep
                        upper = prefix[:-1] + chr(ord(os.sep) + 1)
                        where += " AND path>=? AND path<?"
                        params += (prefix, upper)
                    rows = self.store.db.execute(
                        "SELECT path FROM files WHERE " + where + " ORDER BY path LIMIT ?",
                        (*params, batch_size),
                    ).fetchall()
                    if rows:
                        self.check_limits(seen + len(rows))
                        seen += len(rows)
                        with self.store.transaction():
                            for row in rows:
                                path = Path(row["path"])
                                if self.scope.includes(path):
                                    self.sync(path, source, token)
                        after = rows[-1]["path"]
                    if len(rows) < batch_size:
                        break
        with self.store.transaction():
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
        count = self.store.db.execute("SELECT gaps FROM sessions WHERE id=?", (self.session,)).fetchone()[0]
        category, _ = classify(detail)
        print(f"- agcc gap: {category}; gap_count={count}; details: "
              f"{inline('agcc history --session ' + self.session)}", file=sys.stderr)


def track(store: Store, scope: Scope, agent: str, backend: str = "auto", interval: float = 1,
          reconcile: float = 60, command: list[str] | None = None, duration: float | None = None,
          tag_files: bool = False, control: WatchControl | None = None,
          limits: TrackingLimits | None = None) -> tuple[int, str]:
    if interval < 0.1 or reconcile < interval or (duration is not None and duration <= 0):
        raise SafetyError("interval must be >= 0.1; reconcile >= interval; duration > 0")
    limits = limits or TrackingLimits()
    limits.check_state(store.home)
    observer, warning = make_backend(scope, backend)
    if warning:
        print(f"- agcc warning: {inline(warning)}", file=sys.stderr)
    sid = store.start_session(agent, scope.roots, observer.name if observer else "poll",
                              Path(command[0]).name if command else None)
    tracker = Tracker(store, scope, sid, tag_files, limits)
    process: subprocess.Popen | None = None
    code, interrupted, pending_scan = 0, False, False
    final_failed = False
    tracking_started = False
    old_handlers: dict = {}
    wake_fds: tuple[int, int] | None = None
    previous_wakeup: int | None = None
    control_wakeup = False

    def attach_session(exc: BaseException) -> None:
        # Reporting an error must not replace it with an attribute failure.
        with contextlib.suppress(BaseException):
            exc.agentgc_session = sid

    def stop(signum, _frame):
        nonlocal interrupted, code
        interrupted, code = True, 128 + signum
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                # Keep the child's process group and inherited terminal intact.
                # On SIGINT terminals normally signal both; forwarding is harmless.
                process.send_signal(signum if os.name != "nt" else signal.SIGTERM)

    try:
        tracker.scan("baseline")
        if tracker.errors:
            raise SafetyError("Baseline scan was incomplete; command was not started")
        if observer and getattr(observer, 'coverage_errors', []):
            tracker.gap("Partial native coverage; affected directories will be retried: " +
                        "; ".join(observer.coverage_errors))
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, stop)
        if observer and sys.platform.startswith("linux"):
            try:
                wake_fds = os.pipe()
                for fd in wake_fds:
                    os.set_blocking(fd, False)
                    os.set_inheritable(fd, False)
                previous_wakeup = signal.set_wakeup_fd(wake_fds[1], warn_on_full_buffer=False)
                observer.wake_fd = wake_fds[0]
            except (OSError, ValueError, AttributeError) as exc:
                if previous_wakeup is not None:
                    signal.set_wakeup_fd(previous_wakeup)
                    previous_wakeup = None
                if wake_fds:
                    for fd in wake_fds:
                        os.close(fd)
                    wake_fds = None
                print(f"- agcc warning: Signal wakeup unavailable ({type(exc).__name__}); using short waits",
                      file=sys.stderr)
        if observer and control:
            try:
                observer.watch_control(control.path)
                control_wakeup = True
            except (OSError, AttributeError, SafetyError) as exc:
                print(f"- agcc warning: Control event wakeup unavailable ({type(exc).__name__}); using short waits",
                      file=sys.stderr)
        if command:
            env = dict(os.environ, AGENTGC_SESSION=sid, AGENTGC_HOME=str(store.home))
            process = subprocess.Popen(command, env=env)  # No shell; preserve argv and exit status.
            store.command_started(sid)
        # Baseline/setup can be slow; readiness must start with fresh health.
        store.db.execute("UPDATE sessions SET heartbeat=? WHERE id=?", (time.time(), sid))
        print(f"agcc: session {sid}; backend {observer.name if observer else 'poll'}", file=sys.stderr)
        if control:
            control.ready(sid, scope, duration)
        tracking_started = True
        start = last_scan = last_beat = time.monotonic()
        while not interrupted:
            if control and control.stopping():
                break
            if process is not None and process.poll() is not None:
                code = process.returncode
                store.command_finished(sid, process.returncode)
                break
            now = time.monotonic()
            if duration is not None and now - start >= duration:
                break
            due = interval if observer is None or pending_scan else reconcile
            deadline = min(last_scan + due, last_beat + 5)
            if duration is not None:
                deadline = min(deadline, start + duration)
            timeout = max(0, deadline - now)
            if process is not None:
                timeout = min(timeout, interval, 0.25)
            elif observer is None or wake_fds is None or (control and not control_wakeup):
                timeout = min(timeout, interval, 1)
            if observer:
                notices, dirty, overflow = observer.read(timeout)
                pending_scan |= dirty
                if notices or tracker.errors or overflow:
                    tracker.check_limits()
                    with store.transaction():
                        for path, kind in notices:
                            tracker.sync(path)
                            # Retain short-lived evidence without inventing metadata.
                            if not path.exists():
                                store.event(str(path), kind + "-unresolved", "filesystem-event", sid)
                        if tracker.errors:
                            tracker.gap("Incomplete event read: " + "; ".join(tracker.errors))
                            tracker.errors = []
                        if overflow:
                            tracker.gap("Native event queue overflow; reconciling surviving files")
            else:
                time.sleep(timeout)
            if interrupted or (control and control.stopping()):
                break
            now = time.monotonic()
            due = interval if observer is None or pending_scan else reconcile
            if now - last_scan >= due:
                tracker.scan()
                if observer and (pending_scan or getattr(observer, 'coverage_errors', [])):
                    try:
                        observer.refresh()
                        if getattr(observer, 'coverage_errors', []):
                            tracker.gap("Partial native coverage; affected directories will be retried: " +
                                        "; ".join(observer.coverage_errors))
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
    except BaseException as exc:
        attach_session(exc)
        code = 1
        # Final reconciliation cannot recover intermediate history lost on error.
        # Never turn a failed tracker into a misleading zero-gap session.
        detail = f"Tracking interrupted by {type(exc).__name__}; intermediate history may be incomplete"
        if isinstance(exc, TrackingLimit):
            detail += ": " + str(exc)
        try:
            with store.transaction():
                tracker.gap(detail)
        except BaseException:
            print(f"- agcc gap (not persisted): {inline(detail)}", file=sys.stderr)
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
            if observer and tracker.limit_error is None:
                # Each read is bounded. Drain queued batches, but do not let an
                # unrelated continuing writer postpone shutdown indefinitely.
                deadline = time.monotonic() + 1
                try:
                    for batch in range(32):
                        notices, _, overflow = observer.read(0)
                        if notices or tracker.errors or overflow:
                            tracker.check_limits()
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
            if tracker.limit_error is None:
                tracker.scan()
            else:
                final_failed = True
            final_failed |= bool(tracker.errors) or bool(getattr(observer, 'coverage_errors', []))
        except BaseException as exc:
            attach_session(exc)
            final_failed = True
            detail = f"Final reconciliation failed by {type(exc).__name__}; tracking may be incomplete"
            if isinstance(exc, TrackingLimit):
                detail += ": " + str(exc)
            try:
                with store.transaction():
                    tracker.gap(detail)
            except BaseException:
                print(f"- agcc gap (not persisted): {inline(detail)}", file=sys.stderr)
            raise
        finally:
            try:
                if observer:
                    observer.close()
                if previous_wakeup is not None:
                    signal.set_wakeup_fd(previous_wakeup)
                if wake_fds:
                    for fd in wake_fds:
                        os.close(fd)
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
                if final_failed and command is None and not interrupted:
                    code = 1
                if process is not None and process.poll() is not None:
                    store.command_finished(sid, process.returncode)
                unresolved = store.db.execute(
                    "SELECT COUNT(*) FROM events WHERE session_id=? AND kind LIKE '%-unresolved'", (sid,)
                ).fetchone()[0]
                gaps = store.db.execute("SELECT gaps FROM sessions WHERE id=?", (sid,)).fetchone()[0]
                tracking_status = ("interrupted" if interrupted else "failed" if not tracking_started else
                                   "partial" if final_failed or gaps or unresolved else "complete")
                with store.transaction():
                    store.tracking_finished(sid, tracking_status)
                    store.end_session(sid, code, "interrupted" if interrupted else
                                      "failed" if final_failed or code != 0 else "finished")
            except BaseException as exc:
                attach_session(exc)
                raise
    return (128 - code if code < 0 else code), sid


def scan_once(store: Store, scope: Scope, limits: TrackingLimits | None = None) -> dict:
    limits = limits or TrackingLimits()
    limits.check_state(store.home)
    sid = store.start_session("scan", scope.roots, "snapshot")
    tracker = Tracker(store, scope, sid, limits=limits)
    failed = False
    try:
        tracker.scan("baseline")
    except BaseException as exc:
        failed = True
        with contextlib.suppress(BaseException):
            exc.agentgc_session = sid
        if isinstance(exc, TrackingLimit):
            with store.transaction():
                tracker.gap(str(exc))
        raise
    finally:
        with store.transaction():
            store.tracking_finished(sid, "failed" if failed or tracker.errors else "complete")
            store.end_session(sid, 1 if failed or tracker.errors else 0,
                              "failed" if failed or tracker.errors else "finished")
    return {"session": sid, "errors": tracker.errors, **store.stats()}
