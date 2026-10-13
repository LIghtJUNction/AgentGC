"""Cooperative watcher control; never signal a PID or close a session record."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from .paths import SafetyError, no_links, regular
from .store import Store


def control_path(store: Store, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise SafetyError("control name must be 1-64 ASCII letters, digits, underscores or hyphens")
    path = store.home / "watches" / name
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
        self.path = control_path(store, name)
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        no_links(self.path.parent)
        # Names cannot be reused, so an old stop request cannot stop a new watcher.
        self.path.mkdir(mode=0o700)

    def ready(self, session: str) -> None:
        no_links(self.path)
        with (self.path / "ready.json").open("x", encoding="utf-8") as stream:
            json.dump({"session": session, "pid": os.getpid()}, stream)

    def stopping(self) -> bool:
        no_links(self.path)
        try:
            regular(self.path / "stop")
        except FileNotFoundError:
            return False
        return True


def request_stop(store: Store, name: str) -> dict:
    path = control_path(store, name)
    ready = path / "ready.json"
    if regular(ready).st_size > 4096:
        raise SafetyError("Invalid watcher readiness record")
    record = json.loads(ready.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or not isinstance(record.get("session"), str):
        raise SafetyError("Invalid watcher readiness record")
    row = store.db.execute("SELECT status FROM sessions WHERE id=?", (record["session"],)).fetchone()
    if row is None or row[0] != "running":
        raise SafetyError("No running watcher for this control name")
    stop = path / "stop"
    try:
        with stop.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        regular(stop)
    return {"session": record["session"], "control": name, "stop_requested": True,
            "note": "Wait for the watcher to exit and finish its final scan; this is not cleanup permission."}
