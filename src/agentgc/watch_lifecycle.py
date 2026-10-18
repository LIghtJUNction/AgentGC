"""Launch a finite, controlled local watcher and verify its startup in one call."""

from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from .paths import Scope, SafetyError, absolute, no_links
from .store import Store, StoreNotReady
from .watch_control import _control_path, starting_status, watch_status
from .limits import TrackingLimits, check_state_location

# Keep references while this interpreter is alive so Popen does not discard an
# intentional background child. Polling reaps completed children without signals.
_children: dict[tuple[str, str], subprocess.Popen] = {}


def _reap_completed() -> None:
    for key, child in list(_children.items()):
        if child.poll() is not None:
            del _children[key]


def _finish_owned(child: subprocess.Popen) -> str | None:
    """Bounded cleanup only for the direct child created by this invocation."""
    try:
        if child.poll() is not None:
            return None
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # TimeoutExpired embeds child.args; never log or return that exception.
        return ("Direct child startup cleanup failed (" + type(exc).__name__ +
                "); it may still be running. Inspect its control and private log.")
    return None


def _status(home: Path, control: str) -> dict:
    try:
        store = Store(home, read_only=True)
    except (FileNotFoundError, StoreNotReady):
        return starting_status(home, control)
    try:
        return watch_status(store, control)
    finally:
        store.close()


def start_watch(home: Path, roots: list[str | Path] | None = None,
                excludes: list[str] | None = None, *, agent: str = "unknown",
                backend: str = "auto", interval: float = 1, reconcile: float = 60,
                duration: float = 3600, tag_files: bool = False,
                control: str | None = None, seconds: float = 30,
                config: str | Path | None = None, no_project_rules: bool = False,
                limits: TrackingLimits | None = None,
                allow_memory_state: bool = False) -> tuple[dict, int]:
    """Start without a shell; success requires this child's verified readiness.

    Private logs contain only the existing watcher output, never a saved command
    line. Failed or timed-out startup terminates and reaps only our direct child;
    recorded PIDs belonging to another watcher are never signalled.
    """
    if not math.isfinite(seconds) or not 0 <= seconds <= 300:
        raise SafetyError("Watcher wait must be between 0 and 300 seconds")
    if (not all(math.isfinite(value) for value in (interval, reconcile, duration))
            or interval < 0.1 or reconcile < interval or duration <= 0):
        raise SafetyError("interval must be >= 0.1; reconcile >= interval; duration > 0 and finite")
    if backend not in ("auto", "native", "poll"):
        raise SafetyError("Watcher backend must be auto, native or poll")
    deadline = time.monotonic() + seconds
    home = absolute(home)
    limits = limits or TrackingLimits()
    check_state_location(home, allow_memory_state)
    limits.check_state(home)
    scope = Scope(roots or [Path.cwd()], home, excludes)
    control = control if control is not None else "watch-" + uuid.uuid4().hex
    target = _control_path(home, control)
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        raise SafetyError("Watcher control names cannot be reused")
    _reap_completed()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    no_links(home)
    launches = home / "watch-launches"
    launches.mkdir(mode=0o700, exist_ok=True)
    no_links(launches)
    launch = launches / control
    # A failed launch reserves its name too. This directory is distinct from
    # watches/control, whose creation remains exclusively the watcher's job.
    launch.mkdir(mode=0o700)
    log_path = launch / "watch.log"
    command = [sys.executable, "-B", "-m", "agentgc", "--home", str(home), "--json"]
    if config is not None:
        command.extend(("--config", str(absolute(config))))
    if no_project_rules:
        command.append("--no-project-rules")
    command.extend(("watch", "--agent=" + agent, "--backend", backend, "--interval", str(interval),
                    "--reconcile", str(reconcile), "--duration", str(duration), "--control", control,
                    "--max-files", str(limits.max_files), "--max-state-mib", str(limits.max_state_mib)))
    if allow_memory_state:
        command.append('--allow-memory-state')
    for root in scope.roots:
        command.extend(("--root", str(root)))
    for exclude in scope.excludes:
        command.append("--exclude=" + exclude)
    if tag_files:
        command.append("--tag-files")
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "close_fds": True}
    if os.name == "posix":
        options["start_new_session"] = True
    elif os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(log_path, flags, 0o600), "wb") as log:
        # Final stdout is queried from SQLite by the client; only existing
        # watcher warnings/errors go to the private log.
        child = subprocess.Popen(command, stderr=log, **options)

    result = starting_status(home, control)
    delay = 0.1
    timed_out = False
    failure = "Watcher exited before verified readiness; inspect log."
    succeeded = False
    cleanup_error = None
    try:
        while True:
            result = _status(home, control)
            if result.get("pid") not in (None, child.pid):
                # A competing process may have claimed the name after preflight.
                # Do not request its stop or accept its session as our watcher.
                result = starting_status(home, control)
                failure = "Watcher readiness belongs to another process; own startup stopped."
                break
            if child.poll() is not None:
                break
            if result["ready"]:
                if (not result["coverage_known"] or set(result["roots"]) != {str(root) for root in scope.roots}
                        or result["excludes"] != scope.excludes):
                    failure = "Watcher readiness does not match requested coverage."
                    break
                succeeded = True
                _children[(str(home), control)] = child
                return {**result, "log": str(log_path), "timed_out": False, "start_failed": False}, 0
            if result["state"] not in ("starting", "ready"):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                failure = "Watcher readiness timed out; own startup stopped."
                break
            time.sleep(min(delay, remaining))
            delay = min(1.0, delay * 2)
    except (OSError, ValueError, sqlite3.Error) as exc:
        failure = str(exc)
    finally:
        if not succeeded:
            cleanup_error = _finish_owned(child)
            if cleanup_error is not None:
                _children[(str(home), control)] = child

    try:
        final = _status(home, control)
        if final.get("pid") in (None, child.pid):
            result = final
    except (OSError, ValueError, sqlite3.Error):
        pass
    if cleanup_error is not None:
        failure = "Watcher startup did not complete. " + cleanup_error
    return {**result, "ready": False, "log": str(log_path), "timed_out": timed_out,
            "start_failed": True, "startup_error": failure, "cleanup_failed": cleanup_error is not None,
            "launcher_pid": child.pid, "launcher_exit_code": child.returncode}, 1
