"""Explicit SQLite space reclamation; file labels and payloads are untouched."""

from __future__ import annotations

import sqlite3

from .store import Store


def state_bytes(store: Store) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            total += store.path.with_name(store.path.name + suffix).stat().st_size
        except FileNotFoundError:
            pass
    return total


def compact(store: Store, *, apply: bool) -> dict:
    result = {
        "status": "preview",
        "before_bytes": state_bytes(store),
        "free_page_bytes": store.db.execute("PRAGMA freelist_count").fetchone()[0]
        * store.db.execute("PRAGMA page_size").fetchone()[0],
        "note": "Logical database and sidecar bytes, not physical allocation. "
                "Compaction needs temporary disk space and may fail while other connections are busy.",
    }
    if not apply:
        return result
    try:
        # A busy reader can retain the old WAL. Do not start another full rewrite
        # until the existing one can be checkpointed and truncated.
        if store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise sqlite3.OperationalError("Checkpoint is busy; retry compaction after other connections finish")
        store.db.execute("VACUUM")
        if store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise sqlite3.OperationalError("Post-compaction checkpoint is busy; space reclamation is not confirmed")
    except (OSError, sqlite3.Error) as exc:
        result.update(status="failed", error=str(exc), after_bytes=state_bytes(store))
    else:
        after = state_bytes(store)
        result.update(status="complete", after_bytes=after,
                      reclaimed_bytes=max(0, result["before_bytes"] - after))
    return result


def prune_file_index(store: Store, cutoff: float, *, apply: bool) -> dict:
    """Forget unreviewed metadata only, under the caller's retention transaction."""
    import json
    import os
    from pathlib import Path

    from .paths import SafetyError
    from .store import _query_roots

    if not store.db.in_transaction:
        raise SafetyError("File-index retention requires a transaction")
    result = {"files": 0, "skipped": False,
              "note": "Only index metadata is forgotten; no filesystem files are removed. "
                      "Later scans may observe forgotten files as new baselines. "
                      "Manual labels/notes, review evidence, recovery records, retained events "
                      "and running-session roots are protected. History loss is irreversible."}
    roots = []
    for row in store.db.execute("SELECT roots FROM sessions WHERE status='running'"):
        try:
            values = json.loads(row[0])
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) or not Path(value).is_absolute() for value in values
            ):
                raise ValueError("Unknown active scope")
            roots.extend(values)
        except (ValueError, TypeError):
            result.update(skipped=True, reason="A running session has unknown roots; index retention refused")
            return result
    # Case-insensitive Windows path semantics cannot use POSIX BINARY ranges.
    if os.name == 'nt' and roots:
        result.update(skipped=True, reason="Running sessions protect file-index retention on Windows")
        return result
    condition = """changed<? AND label='review' AND note='' AND review_hash IS NULL
        AND review_signature IS NULL AND tag_status='disabled' AND source<>'manual-observation'
        AND (session_id IS NULL OR session_id IN (SELECT id FROM sessions WHERE status<>'running'))
        AND NOT EXISTS (SELECT 1 FROM quarantine q WHERE q.file_id=files.id)
        AND NOT EXISTS (SELECT 1 FROM events e WHERE (e.file_id=files.id OR e.path=files.path)
            AND NOT (e.at<? AND e.kind NOT IN ('marked','quarantined','restored')
                AND (e.session_id IS NULL OR e.session_id IN
                    (SELECT id FROM sessions WHERE status<>'running'))))"""
    params = [cutoff, cutoff]
    for root in _query_roots(roots):
        prefix = str(root).rstrip(os.sep) + os.sep
        upper = prefix[:-1] + chr(ord(os.sep) + 1)
        condition += " AND NOT (path>=? AND path<?)"
        params.extend((prefix, upper))
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_marks'").fetchone():
        condition += " AND NOT EXISTS (SELECT 1 FROM auto_marks a WHERE a.path=files.path)"
    if apply:
        result['files'] = store.db.execute("DELETE FROM files WHERE " + condition, params).rowcount
    else:
        result['files'] = store.db.execute("SELECT COUNT(*) FROM files WHERE " + condition, params).fetchone()[0]
    return result
