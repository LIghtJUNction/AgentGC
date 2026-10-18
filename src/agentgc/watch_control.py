"""Cooperative watcher control; never signal a PID or close a session record."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from .paths import Scope, SafetyError, absolute, no_links, regular
from .store import Store, StoreNotReady


def control_path(store: Store, name: str) -> Path:
    return _control_path(store.home, name)


def _control_path(home: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise SafetyError("control name must be 1-64 ASCII letters, digits, underscores or hyphens")
    path = home / "watches" / name
    for candidate in (path, *path.parents):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        no_links(candidate)
        break
    return path


class WatchControl:
    def __init__(self, store: Store, name: str):
        self.store = store
        self.path = control_path(store, name)
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        no_links(self.path.parent)
        # Names cannot be reused, so an old stop request cannot stop a new watcher.
        self.path.mkdir(mode=0o700)

    def ready(self, session: str, scope: Scope | None = None, duration: float | None = None) -> None:
        no_links(self.path)
        row = self.store.db.execute("SELECT roots FROM sessions WHERE id=?", (session,)).fetchone()
        if row is None:
            raise SafetyError("No watcher session for readiness")
        record = {"version": 1, "session": session, "pid": os.getpid(),
                  "roots": json.loads(row["roots"]), "excludes": scope.excludes if scope else None,
                  "expires_at": time.time() + duration if duration is not None else None}
        payload = json.dumps(record, ensure_ascii=True)
        if len(payload.encode("utf-8")) > 4096:
            raise SafetyError("Watcher readiness exceeds the 4096-byte metadata budget")
        temporary = self.path / "ready.tmp"
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(payload)
        try:
            # Publish complete JSON atomically, without replacing an existing path.
            os.link(temporary, self.path / "ready.json")
        finally:
            temporary.unlink()

    def stopping(self) -> bool:
        no_links(self.path)
        try:
            regular(self.path / "stop")
        except FileNotFoundError:
            return False
        return True


def readiness(store: Store, name: str) -> tuple[dict, sqlite3.Row]:
    ready = control_path(store, name) / "ready.json"
    if regular(ready).st_size > 4096:
        raise SafetyError("Invalid watcher readiness record")
    try:
        record = json.loads(ready.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise SafetyError("Invalid watcher readiness record") from exc
    if (not isinstance(record, dict) or not isinstance(record.get("session"), str)
            or type(record.get("pid")) is not int or record["pid"] <= 0
            or record.get("version", 1) != 1):
        raise SafetyError("Invalid watcher readiness record")
    row = store.db.execute("SELECT * FROM sessions WHERE id=?", (record["session"],)).fetchone()
    if row is None or record["pid"] != row["pid"] or row["command"] is not None:
        raise SafetyError("Watcher readiness does not match session identity")
    if "roots" in record and record["roots"] != json.loads(row["roots"]):
        raise SafetyError("Watcher readiness does not match session roots")
    excludes = record.get("excludes")
    if excludes is not None and (not isinstance(excludes, list) or any(not isinstance(p, str) for p in excludes)):
        raise SafetyError("Invalid watcher exclusion metadata")
    expiry = record.get("expires_at")
    if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry)):
        raise SafetyError("Invalid watcher expiry metadata")
    return record, row


def starting_status(home: Path, name: str, note: str | None = None) -> dict:
    """Keep unavailable startup evidence typed, rather than assuming zero gaps."""
    return {"schema_version": 1, "control": name, "home": str(home), "session": None,
            "state": "starting", "ready": False, "status": None, "session_status": None,
            "pid": None, "backend": None, "roots": [], "excludes": None,
            "coverage_known": False, "expires_at": None, "heartbeat_age_seconds": None,
            "exit_code": None, "command_status": "not-applicable", "command_exit_code": None,
            "tracking_status": "unknown", "gaps": None, "gap_count": None,
            "unresolved_event_count": None,
            "count_scope": {"gap_count": "session-cumulative", "unresolved_event_count": "retained-events"},
            "note": note or "No successful baseline published; do not start writes."}


def watch_status(store: Store, name: str) -> dict:
    # Readiness identity, legacy status and independent outcomes must describe
    # the same database snapshot while a watcher finalizes concurrently.
    store.db.execute("SAVEPOINT watch_status_snapshot")
    try:
        return _watch_status(store, name)
    finally:
        store.db.execute("RELEASE watch_status_snapshot")


def _watch_status(store: Store, name: str) -> dict:
    try:
        record, row = readiness(store, name)
    except FileNotFoundError:
        return starting_status(store.home, name)
    now = time.time()
    age = now - row["heartbeat"]
    state = row["status"]
    if state == "running":
        state = "ready" if 0 <= age <= 15 else "stale"
        if record.get("expires_at") is not None and now >= record["expires_at"]:
            state = "expiring"
        try:
            regular(control_path(store, name) / "stop")
        except FileNotFoundError:
            pass
        else:
            state = "stopping"
    # Read-only status clients work with legacy stores too; the shared outcome
    # reader retains unknown command results instead of inferring from exit_code.
    from .session_state import session_result
    return {**session_result(store, record["session"]),
            "control": name, "home": str(store.home), "session": record["session"], "pid": record["pid"],
            "state": state, "ready": state == "ready", "status": row["status"],
            "backend": row["backend"], "heartbeat_age_seconds": round(age, 2),
            "roots": json.loads(row["roots"]), "excludes": record.get("excludes"),
            "coverage_known": "roots" in record and record.get("excludes") is not None,
            "expires_at": record.get("expires_at"), "exit_code": row["exit_code"], "gaps": row["gaps"],
            "note": "Recent heartbeat evidence is not proof of live writers or cleanup permission."}


def wait_status(store: Store, name: str, goal: str, seconds: float) -> dict:
    """One bounded client wait, not recurring agent tool calls or session mutation."""
    if goal not in ("ready", "finished") or not math.isfinite(seconds) or not 0 <= seconds <= 300:
        raise SafetyError("Watcher wait must be between 0 and 300 seconds")
    deadline = time.monotonic() + seconds
    delay = 0.1
    while True:
        result = watch_status(store, name)
        ended = result.get("status") in ("finished", "failed", "interrupted", "closed-by-user")
        reached = result["ready"] if goal == "ready" else ended
        if reached or (goal == "ready" and (ended or result["state"] in ("stopping", "expiring"))):
            return {**result, "timed_out": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, "timed_out": True}
        time.sleep(min(delay, remaining))
        delay = min(1.0, delay * 2)


def control_command(home: Path, name: str, *, stop: bool = False, seconds: float | None = None) -> tuple[dict, int]:
    """Open existing state without writer contention; include opening in the wait budget."""
    if seconds is not None and (not math.isfinite(seconds) or not 0 <= seconds <= 300):
        raise SafetyError("Watcher wait must be between 0 and 300 seconds")
    deadline = time.monotonic() + (seconds or 0)
    home = absolute(home)
    _control_path(home, name)
    delay = 0.1
    while True:
        try:
            store = Store(home, read_only=True)
        except (FileNotFoundError, StoreNotReady):
            if stop:
                raise SafetyError("No successful watcher baseline for this control name")
            result = starting_status(home, name, "No initialized state or successful baseline; do not start writes.")
            remaining = deadline - time.monotonic()
            if seconds is None:
                return result, 1
            if remaining <= 0:
                return {**result, "timed_out": True}, 1
            time.sleep(min(delay, remaining))
            delay = min(1.0, delay * 2)
            continue
        try:
            if stop:
                if seconds is None:
                    # A request-only client still returns the same typed
                    # coverage and quality fields as a waiting client. Keep
                    # the request identity and subsequent status coherent if
                    # the watcher finalizes as soon as it sees the stop file.
                    store.db.execute("SAVEPOINT watch_stop_snapshot")
                    try:
                        result = request_stop(store, name)
                        return {**watch_status(store, name), **result}, 0
                    finally:
                        store.db.execute("RELEASE watch_stop_snapshot")
                result = request_stop(store, name)
            if seconds is not None:
                status = wait_status(store, name, "finished" if stop else "ready",
                                     max(0, deadline - time.monotonic()))
            else:
                status = watch_status(store, name)
            result = {**result, **status} if stop else status
            success = result["state"] == "finished" if stop else (
                result["ready"] or (seconds is None and result["state"] == "finished"))
            return result, 0 if success else 1
        finally:
            store.close()


def request_stop(store: Store, name: str) -> dict:
    record, row = readiness(store, name)
    if row["status"] != "running":
        raise SafetyError("No running watcher for this control name")
    stop = control_path(store, name) / "stop"
    try:
        with stop.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        regular(stop)
    return {"session": record["session"], "control": name, "stop_requested": True,
            "note": "Wait for the watcher to exit and finish its final scan; this is not cleanup permission."}
