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
CREATE TABLE IF NOT EXISTS session_results (
 session_id TEXT PRIMARY KEY,
 command_status TEXT NOT NULL, command_exit_code INTEGER,
 tracking_status TEXT NOT NULL
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


class StoreNotReady(SafetyError):
    """Existing state is initializing or temporarily busy for a nonblocking reader."""


def _query_roots(roots: list[str | Path] | None) -> tuple[Path, ...]:
    """Normalize index filters lexically, without touching historical paths."""
    normalized = {Path(os.path.abspath(os.path.expanduser(root))) for root in roots or []}
    selected: list[Path] = []
    for root in sorted(normalized, key=lambda path: (len(path.parts), str(path))):
        if not any(root.is_relative_to(parent) for parent in selected):
            selected.append(root)
    return tuple(selected)


def _page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 10000:
        raise SafetyError("limit must be between 1 and 10000")
    if not 0 <= offset <= 2**63 - 1:
        raise SafetyError("offset must be between 0 and 9223372036854775807")


class Store:
    def __init__(self, home: Path, *, read_only: bool = False):
        self.home = absolute(home)
        if not read_only:
            self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        no_links(self.home)
        self.path = self.home / "index.sqlite3"
        for candidate in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
            if candidate.exists() or candidate.is_symlink():
                no_links(candidate)
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            connection = None
            try:
                connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True,
                                             timeout=0, isolation_level=None)
                self.db = connection
                self.db.row_factory = sqlite3.Row
                self.db.execute("PRAGMA query_only=ON")
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    raise StoreNotReady("State database initialization is incomplete")
                if version != 1:
                    raise SafetyError(f"Unsupported database version: {version}")
            except sqlite3.OperationalError as exc:
                if connection is not None:
                    connection.close()
                code = getattr(exc, 'sqlite_errorcode', None)
                if isinstance(code, int) and (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise StoreNotReady("State database is temporarily busy; readiness is not yet verified") from exc
                raise
            except BaseException:
                if connection is not None:
                    connection.close()
                raise
            return
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA busy_timeout=10000")
            # Reopening an initialized WAL store must not acquire a writer lock.
            if self.db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise SafetyError(f"Unsupported database version: {version}")
            statements = [statement.strip() for statement in SCHEMA.split(";") if statement.strip()]
            # Schema-v1 has additive indexes too. Check catalog metadata without
            # a write lock, but retain upgrades for older initialized stores.
            expected = {statement.split()[5] for statement in statements}
            existing = {row[0] for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )}
            if version == 0 or not expected.issubset(existing):
                # Publish the version only after all initialization is durable.
                with self.transaction():
                    for statement in statements:
                        self.db.execute(statement)
                    self.db.execute("INSERT OR IGNORE INTO meta VALUES ('id', ?)", (str(uuid.uuid4()),))
                    self.db.execute("PRAGMA user_version=1")
            if os.name != "nt":
                self.path.chmod(0o600)
        except BaseException:
            self.db.close()
            raise

    def tag_payload(self, file_id: int, label: str, source: str, session: str | None = None) -> dict:
        db_id = self.db.execute("SELECT value FROM meta WHERE key='id'").fetchone()[0]
        return {"v": 1, "db": db_id, "file": file_id, "label": label, "source": source, "session": session}

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
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
        self.db.execute(
            "INSERT INTO session_results(session_id,command_status,tracking_status) VALUES(?,?,'running')",
            (sid, "not-started" if command else "not-applicable"),
        )
        return sid

    def command_started(self, sid: str) -> None:
        self.db.execute(
            "UPDATE session_results SET command_status='running',command_exit_code=NULL WHERE session_id=?",
            (sid,),
        )

    def command_finished(self, sid: str, code: int) -> None:
        # This is the child's actual returncode, including a negative POSIX
        # signal value. Wrapper/tracking errors never substitute their code.
        self.db.execute(
            "UPDATE session_results SET command_status=?,command_exit_code=? WHERE session_id=?",
            ("succeeded" if code == 0 else "failed", code, sid),
        )

    def tracking_finished(self, sid: str, status: str) -> None:
        if status not in ("complete", "partial", "failed", "interrupted", "unknown"):
            raise SafetyError("Invalid tracking result")
        self.db.execute("UPDATE session_results SET tracking_status=? WHERE session_id=?", (status, sid))

    def end_session(self, sid: str, code: int, status: str = "finished") -> None:
        self.db.execute(
            "UPDATE sessions SET status=?, ended=?, heartbeat=?, exit_code=? WHERE id=?",
            (status, time.time(), time.time(), code, sid),
        )
        # Generic callers can end scans or manually close a confirmed stopped
        # session. They cannot establish a child's result from this legacy
        # wrapper exit_code. Explicit tracker results take precedence.
        self.db.execute(
            "UPDATE session_results SET tracking_status=CASE "
            "WHEN ?='interrupted' THEN 'interrupted' "
            "WHEN ?='finished' THEN CASE WHEN "
            "(SELECT gaps FROM sessions WHERE id=?)>0 OR EXISTS "
            "(SELECT 1 FROM events WHERE session_id=? AND kind LIKE '%-unresolved') "
            "THEN 'partial' ELSE 'complete' END ELSE 'unknown' END "
            "WHERE session_id=? AND tracking_status='running'",
            (status, status, sid, sid, sid),
        )

    def active_for(self, path: Path) -> list[str]:
        # A stale heartbeat is NOT proof that the process or its children stopped.
        return [row["id"] for row in self.db.execute("SELECT id,roots FROM sessions WHERE status='running'")
                if any(path.is_relative_to(Path(root)) for root in json.loads(row["roots"]))]

    def _path_filter(self, column: str, roots: tuple[Path, ...]) -> tuple[str, list[str]]:
        if os.name == "nt":
            # Native Path comparisons preserve Windows case-insensitive semantics.
            self.db.create_function(
                "agentgc_query_path", 1,
                lambda value: int(bool(value) and any(Path(value).is_relative_to(root) for root in roots)),
                deterministic=True,
            )
            return f"agentgc_query_path({column})=1", []
        terms, values = [], []
        for root in roots:
            prefix = str(root).rstrip(os.sep) + os.sep
            upper = prefix[:-1] + chr(ord(os.sep) + 1)
            # Path columns already use BINARY collation. Redundant explicit
            # collations here prevent SQLite from combining the path indexes.
            terms.append(f"({column}=? OR ({column}>=? AND {column}<?))")
            values.extend((str(root), prefix, upper))
        return "(" + " OR ".join(terms) + ")", values

    def _session_filter(self, roots: tuple[Path, ...]) -> str:
        def overlap(value: str) -> int:
            stored = json.loads(value)
            if not isinstance(stored, list) or any(not isinstance(root, str) for root in stored):
                raise ValueError("Invalid session roots")
            return int(any(Path(root).is_relative_to(selected) or selected.is_relative_to(Path(root))
                           for root in stored for selected in roots))

        # JSON1 is optional in SQLite builds; connection-local functions need no
        # schema changes or unbounded lists of matching session IDs.
        self.db.create_function("agentgc_query_overlap", 1, overlap, deterministic=True)
        return "agentgc_query_overlap(s.roots)=1"

    def list_files(self, query: str = "", sort: str = "time", label: str | None = None,
                   include_missing: bool = False, limit: int = 100, offset: int = 0,
                   session: str | None = None, roots: list[str | Path] | None = None) -> list[dict]:
        orders = {"time": "f.changed", "size": "f.size", "activity": "f.activity"}
        if sort not in orders:
            raise SafetyError("Invalid sort")
        _page(limit, offset)
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
        selected = _query_roots(roots)
        if selected:
            clause, values = self._path_filter("f.path", selected)
            terms.append(clause)
            args.extend(values)
        sql = ("SELECT f.*,s.agent FROM files f LEFT JOIN sessions s ON s.id=f.session_id WHERE "
               + " AND ".join(terms) + f" ORDER BY {orders[sort]} DESC,f.path LIMIT ? OFFSET ?")
        return [dict(r) for r in self.db.execute(sql, (*args, limit, offset))]

    def history(self, path: str | Path | None = None, session: str | None = None,
                limit: int = 100, offset: int = 0,
                roots: list[str | Path] | None = None) -> list[dict]:
        _page(limit, offset)
        terms, values = [], []
        if path:
            terms.append("e.path=?")
            values.append(str(absolute(path)))
        if session:
            terms.append("e.session_id=?")
            values.append(session)
        selected = _query_roots(roots)
        if selected:
            clause, parameters = self._path_filter("e.path", selected)
            overlapping = self._session_filter(selected)
            terms.append(f"({clause} OR (e.path='' AND e.kind='gap' AND EXISTS "
                         f"(SELECT 1 FROM sessions s WHERE s.id=e.session_id AND {overlapping})))")
            values.extend(parameters)
        where = " WHERE " + " AND ".join(terms) if terms else ""
        return [dict(row) for row in self.db.execute(
            "SELECT e.* FROM events e" + where + " ORDER BY e.id DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        )]

    def list_sessions(self, roots: list[str | Path] | None = None, status: str | None = None,
                      limit: int = 100, offset: int = 0) -> list[dict]:
        _page(limit, offset)
        terms, values = [], []
        if status is not None:
            if status not in ("running", "finished", "failed", "interrupted", "closed-by-user"):
                raise SafetyError("Invalid session status")
            terms.append("s.status=?")
            values.append(status)
        selected = _query_roots(roots)
        if selected:
            terms.append(self._session_filter(selected))
        where = " WHERE " + " AND ".join(terms) if terms else ""
        return [dict(row) for row in self.db.execute(
            "SELECT s.* FROM sessions s" + where + " ORDER BY s.started DESC,s.id LIMIT ? OFFSET ?",
            (*values, limit, offset),
        )]

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) files,COALESCE(SUM(size),0) bytes FROM files WHERE present=1"
        ).fetchone()
        return {**dict(row), "events": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "active_sessions": self.db.execute("SELECT COUNT(*) FROM sessions WHERE status='running'").fetchone()[0],
                "labels": {r[0]: r[1] for r in self.db.execute(
                    "SELECT label,COUNT(*) FROM files WHERE present=1 GROUP BY label")}}
