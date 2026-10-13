"""SQLite is the source of truth; file tags are only optional mirrors."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator
import uuid

from .paths import SafetyError, absolute, no_links

LABELS = ("keep", "temporary", "review", "disposable")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, agent TEXT NOT NULL, roots TEXT NOT NULL,
 command TEXT, backend TEXT NOT NULL, status TEXT NOT NULL,
 started REAL NOT NULL, ended REAL, heartbeat REAL NOT NULL,
 pid INTEGER, exit_code INTEGER, gaps INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
 id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, root TEXT NOT NULL,
 size INTEGER NOT NULL, signature TEXT NOT NULL,
 present INTEGER NOT NULL DEFAULT 1, label TEXT NOT NULL DEFAULT 'review',
 first_seen REAL NOT NULL, changed REAL NOT NULL, activity INTEGER NOT NULL DEFAULT 0,
 session_id TEXT, source TEXT NOT NULL, scan_token TEXT,
 review_hash TEXT, review_signature TEXT, note TEXT NOT NULL DEFAULT '',
 tag_status TEXT NOT NULL DEFAULT 'disabled'
);
CREATE INDEX IF NOT EXISTS files_size ON files(size DESC);
CREATE INDEX IF NOT EXISTS files_changed ON files(changed DESC);
CREATE INDEX IF NOT EXISTS files_label ON files(label, present);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, file_id INTEGER, path TEXT NOT NULL,
 session_id TEXT, kind TEXT NOT NULL, source TEXT NOT NULL,
 at REAL NOT NULL, size INTEGER, detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_file ON events(file_id, id DESC);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id DESC);
CREATE INDEX IF NOT EXISTS events_file_session ON events(file_id, session_id);
CREATE INDEX IF NOT EXISTS events_path ON events(path, id DESC);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
CREATE TABLE IF NOT EXISTS quarantine (
 id TEXT PRIMARY KEY, file_id INTEGER NOT NULL, original TEXT NOT NULL,
 destination TEXT NOT NULL, signature TEXT NOT NULL, digest TEXT NOT NULL,
 status TEXT NOT NULL, created REAL NOT NULL, restored REAL
);
"""


class Store:
    def __init__(self, home: Path):
        self.home = absolute(home)
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        no_links(self.home)
        self.path = self.home / "index.sqlite3"
        for candidate in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
            if candidate.exists() or candidate.is_symlink():
                no_links(candidate)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise SafetyError(f"Unsupported database version: {version}")
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA user_version=1")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('id', ?)", (str(uuid.uuid4()),))
        if os.name != "nt":
            self.path.chmod(0o600)

    def tag_payload(self, file_id: int, label: str, source: str, session: str | None = None) -> dict:
        db_id = self.db.execute("SELECT value FROM meta WHERE key='id'").fetchone()[0]
        return {"v": 1, "db": db_id, "file": file_id, "label": label, "source": source, "session": session}

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def event(self, path: str, kind: str, source: str, session: str | None = None,
              file_id: int | None = None, size: int | None = None, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events(file_id,path,session_id,kind,source,at,size,detail) VALUES(?,?,?,?,?,?,?,?)",
            (file_id, path, session, kind, source, time.time(), size, detail),
        )

    def file(self, path: str | Path) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM files WHERE path=?", (str(absolute(path)),)).fetchone()
        if row is None:
            raise SafetyError(f"Not indexed: {path}. Run agcc scan first.")
        return row

    def start_session(self, agent: str, roots: list[Path], backend: str,
                      command: str | None = None) -> str:
        sid = str(uuid.uuid4())
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions(id,agent,roots,command,backend,status,started,heartbeat,pid) "
            "VALUES(?,?,?,?,?,'running',?,?,?)",
            (sid, agent, json.dumps([str(r) for r in roots]), command, backend, now, now, os.getpid()),
        )
        return sid

    def end_session(self, sid: str, code: int, status: str = "finished") -> None:
        self.db.execute(
            "UPDATE sessions SET status=?, ended=?, heartbeat=?, exit_code=? WHERE id=?",
            (status, time.time(), time.time(), code, sid),
        )

    def active_for(self, path: Path) -> list[str]:
        # A stale heartbeat is NOT proof that the process or its children stopped.
        return [row["id"] for row in self.db.execute("SELECT id,roots FROM sessions WHERE status='running'")
                if any(path.is_relative_to(Path(root)) for root in json.loads(row["roots"]))]

    def list_files(self, query: str = "", sort: str = "time", label: str | None = None,
                   include_missing: bool = False, limit: int = 100, offset: int = 0,
                   session: str | None = None) -> list[dict]:
        orders = {"time": "f.changed", "size": "f.size", "activity": "f.activity"}
        if sort not in orders or not 1 <= limit <= 10000 or offset < 0:
            raise SafetyError("Invalid sort, limit (1..10000), or offset")
        text = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        terms = ["(f.path LIKE ? ESCAPE '\\' OR f.note LIKE ? ESCAPE '\\' OR s.agent LIKE ? ESCAPE '\\')"]
        args: list = [f"%{text}%"] * 3
        if not include_missing:
            terms.append("f.present=1")
        if label:
            terms.append("f.label=?")
            args.append(label)
        if session:
            terms.append("EXISTS (SELECT 1 FROM events e WHERE e.file_id=f.id AND e.session_id=?)")
            args.append(session)
        sql = ("SELECT f.*,s.agent FROM files f LEFT JOIN sessions s ON s.id=f.session_id WHERE "
               + " AND ".join(terms) + f" ORDER BY {orders[sort]} DESC,f.path LIMIT ? OFFSET ?")
        return [dict(r) for r in self.db.execute(sql, (*args, limit, offset))]

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) files,COALESCE(SUM(size),0) bytes FROM files WHERE present=1"
        ).fetchone()
        return {**dict(row), "events": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "active_sessions": self.db.execute("SELECT COUNT(*) FROM sessions WHERE status='running'").fetchone()[0],
                "labels": {r[0]: r[1] for r in self.db.execute(
                    "SELECT label,COUNT(*) FROM files WHERE present=1 GROUP BY label")}}
