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
