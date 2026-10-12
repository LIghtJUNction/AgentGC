"""Explicit review, recoverable moves, and a durable operation journal."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from .paths import INTERNAL_NAMES, SafetyError, absolute, digest, no_links, regular
from .store import LABELS, Store
from .tags import write_tag


def git_protected(path: Path) -> bool:
    for parent in path.parents:
        if (parent / ".git").exists():
            executable = shutil.which("git")
            if not executable:
                return True  # Cannot establish that the file is untracked.
            try:
                result = subprocess.run(
                    [executable, "-C", str(parent), "ls-files", "--error-unmatch", "--",
                     path.relative_to(parent).as_posix()],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                return True
            return result.returncode != 1
    return False


def allowed_path(store: Store, path: Path) -> None:
    if path.is_relative_to(store.home) or any(part in INTERNAL_NAMES for part in path.parts):
        raise SafetyError("Internal state and Git metadata are protected")
    info = regular(path)
    if info.st_nlink != 1:
        raise SafetyError("Hard-linked files are protected")
    if git_protected(path):
        raise SafetyError("Git-tracked files (or unverifiable Git state) are protected")
    if store.active_for(path):
        raise SafetyError("A tracking session is still running for this path")


def mark(store: Store, path: Path, label: str, note: str = "", tag_files: bool = False) -> dict:
    if label not in LABELS:
        raise SafetyError(f"Unknown label: {label}")
    path = absolute(path)
    with store.transaction():
        row = store.file(path)
        if not row["present"]:
            raise SafetyError("Cannot mark a missing or quarantined file")
        review_sig = review_hash = None
        if label == "disposable":
            if not note.strip():
                raise SafetyError("A disposable label requires --note explaining why it is no longer needed")
            allowed_path(store, path)
            review_sig, review_hash = digest(path)
            if review_sig != row["signature"]:
                raise SafetyError("File changed since indexing; scan and review it again")
        store.db.execute(
            "UPDATE files SET label=?,note=?,review_signature=?,review_hash=? WHERE id=?",
            (label, note, review_sig, review_hash, row["id"]),
        )
        store.event(str(path), "marked", "user-review", file_id=row["id"], detail=f"{label}: {note}")
        if tag_files:
            status = write_tag(path, store.tag_payload(row["id"], label, "user-review"))
            store.db.execute("UPDATE files SET tag_status=? WHERE id=?", (status, row["id"]))
    return dict(store.file(path))


def validate_candidate(store: Store, row) -> tuple[str, str]:
    path = Path(row["path"])
    if row["label"] != "disposable" or not row["review_hash"]:
        raise SafetyError("File has not been explicitly reviewed as disposable")
    if not row["present"]:
        raise SafetyError("File is missing or already quarantined")
    allowed_path(store, path)
    stamp, value = digest(path)
    if stamp != row["review_signature"] or value != row["review_hash"]:
        raise SafetyError("File changed after review; scan and review it again")
    return stamp, value


def plan(store: Store, paths: list[str] | None = None, limit: int = 1000) -> dict:
    if not 1 <= limit <= 10000:
        raise SafetyError("limit must be between 1 and 10000")
    if paths:
        rows = [store.file(path) for path in dict.fromkeys(paths)]
        truncated = len(rows) > limit
        rows = rows[:limit]
    else:
        rows = list(store.db.execute(
            "SELECT * FROM files WHERE label='disposable' AND present=1 ORDER BY size DESC,id LIMIT ?",
            (limit + 1,),
        ))
        truncated, rows = len(rows) > limit, rows[:limit]
    items = []
    for row in rows:
        reason = None
        try:
            validate_candidate(store, row)
        except (OSError, SafetyError) as exc:
            reason = str(exc)
        items.append({"path": row["path"], "size": row["size"], "eligible": reason is None, "reason": reason})
    return {"mode": "preview", "items": items, "truncated": truncated,
            "bytes_to_quarantine": sum(i["size"] for i in items if i["eligible"]),
            "bytes_freed": 0}


def quarantine_one(store: Store, path: Path) -> dict:
    path = absolute(path)
    # Hold the write lock across revalidation and rename so another agcc process
    # cannot change review or start a session between them. Unmanaged writers
    # must be stopped by the operator; this is not an OS sandbox.
    with store.transaction():
        row = store.file(path)
        stamp, value = validate_candidate(store, row)
        qid = str(uuid.uuid4())
        folder = path.parent / ".agentgc-quarantine"
        folder.mkdir(mode=0o700, exist_ok=True)
        no_links(folder)
        operation = folder / qid
        operation.mkdir(mode=0o700)
        destination = operation / "payload"
        store.db.execute(
            "INSERT INTO quarantine(id,file_id,original,destination,signature,digest,status,created) "
            "VALUES(?,?,?,?,?,?,'prepared',?)",
            (qid, row["id"], str(path), str(destination), stamp, value, time.time()),
        )
    # The prepared journal must be durable before the filesystem changes.
    with store.transaction():
        row = store.file(path)
        validate_candidate(store, row)
        no_links(destination.parent)
        if destination.exists() or destination.is_symlink():
            raise SafetyError("Quarantine destination is occupied")
        os.rename(path, destination)  # Same-directory filesystem; no silent copy/delete fallback.
        # If a race changed the source, retain the moved object for recovery.
        status = "quarantined"
        try:
            after_sig, after_hash = digest(destination)
            if (after_sig, after_hash) != (stamp, value):
                status = "needs-review"
        except (OSError, SafetyError):
            status = "needs-review"
        store.db.execute("UPDATE quarantine SET status=? WHERE id=?", (status, qid))
        store.db.execute(
            "UPDATE files SET present=0,review_hash=NULL,review_signature=NULL,label='review' WHERE id=?", (row["id"],)
        )
        store.event(str(path), "quarantined", "user-review", file_id=row["id"], size=row["size"], detail=qid)
    return {"id": qid, "path": str(path), "destination": str(destination), "status": status}


def restore(store: Store, qid: str) -> dict:
    with store.transaction():
        row = store.db.execute("SELECT * FROM quarantine WHERE id=?", (qid,)).fetchone()
        if row is None or row["status"] == "restored":
            raise SafetyError("Unknown or already restored quarantine entry")
        source, target = Path(row["destination"]), Path(row["original"])
        no_links(source)
        no_links(target.parent)
        if target.exists() or target.is_symlink():
            raise SafetyError("Restore destination exists; it will not be overwritten")
        if store.active_for(target):
            raise SafetyError("Stop tracking sessions before restoring files")
        stamp, value = digest(source)
        if (stamp, value) != (row["signature"], row["digest"]):
            raise SafetyError("Quarantined file changed; inspect it manually before recovery")
        # A hard link creates the target exclusively. Unlike rename, it cannot
        # replace a file created concurrently. Unsupported filesystems fail safe.
        os.link(source, target, follow_symlinks=False)
        # Keep the quarantine link until the restored journal is committed. A
        # crash can leave a duplicate link, but never lose the only copy.
        store.db.execute("UPDATE quarantine SET status='restored',restored=? WHERE id=?", (time.time(), qid))
        store.db.execute(
            "UPDATE files SET present=1,label='keep',signature=?,size=?,review_hash=NULL,review_signature=NULL WHERE id=?",
            (stamp, regular(target).st_size, row["file_id"]),
        )
        store.event(str(target), "restored", "user-review", file_id=row["file_id"], detail=qid)
    source.unlink()
    try:
        source.parent.rmdir()
    except OSError:
        pass
    return {"id": qid, "path": str(target), "status": "restored", "label": "keep"}


def recover(store: Store, apply: bool = False) -> list[dict]:
    """Reconcile interrupted moves without moving or deleting user content."""
    items = []
    with store.transaction():
        for row in store.db.execute("SELECT * FROM quarantine WHERE status='prepared'").fetchall():
            src, dst = Path(row["original"]), Path(row["destination"])
            src_exists = src.exists() or src.is_symlink()
            dst_exists = dst.exists() or dst.is_symlink()
            status = "needs-review"
            if dst_exists and not src_exists:
                try:
                    if digest(dst) == (row["signature"], row["digest"]):
                        status = "quarantined"
                except (OSError, SafetyError):
                    pass
            elif src_exists and not dst_exists:
                status = "cancelled"
            items.append({"id": row["id"], "status": status, "original": str(src), "destination": str(dst)})
            if apply:
                store.db.execute("UPDATE quarantine SET status=? WHERE id=?", (status, row["id"]))
                if status == "quarantined":
                    store.db.execute(
                        "UPDATE files SET present=0,label='review',review_hash=NULL,review_signature=NULL WHERE id=?",
                        (row["file_id"],),
                    )
    return items
