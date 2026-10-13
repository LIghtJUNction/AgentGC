"""Bounded directory pressure inventory. Attention is never cleanup permission."""
from __future__ import annotations

import math
import os
import shutil
from pathlib import Path
import stat
import time

from .paths import SafetyError, absolute, no_links

MIB = 1024 * 1024
DEFAULTS = {"dependencies": 1024*MIB, "build": 512*MIB, "cache": 1024*MIB,
            "logs": 256*MIB, "quarantine": MIB, "protected": 5120*MIB}
NAMES = {"target": "build", "build": "build", "dist": "build", ".next": "build",
         "node_modules": "dependencies", ".venv": "dependencies", "venv": "dependencies",
         ".cache": "cache", "cache": "cache", "__pycache__": "cache",
         ".npm": "cache", "registry": "cache", "pkgs": "cache",
         ".rustup": "dependencies", "mise": "dependencies", "uv": "dependencies",
         ".pytest_cache": "cache", ".mypy_cache": "cache", ".ruff_cache": "cache",
         "logs": "logs", "log": "logs", ".agentgc-quarantine": "quarantine",
         "Backups": "protected", "docker": "protected", "containers": "protected",
         "postgresql": "protected", "mysql": "protected", "mariadb": "protected",
         "models": "protected", "Downloads": "protected"}
PATH_CATEGORIES = {Path("/var/cache/pacman/pkg"): "cache", Path("/var/lib"): "protected",
                   Path("/opt"): "dependencies", Path("/usr/local"): "dependencies",
                   Path("/tmp"): "cache"}
SKIP = {".git"}
SYSTEM_SKIP = {Path("/proc"), Path("/sys"), Path("/dev"), Path("/run")}


def inventory(roots: list[Path], thresholds: dict[str, int] | None = None,
              max_entries: int = 200000, max_seconds: float = 30,
              state_home: Path | None = None, file_threshold: int = 100000) -> dict:
    limits = {**DEFAULTS, **(thresholds or {})}
    if set(limits) != set(DEFAULTS) or any(not isinstance(v, int) or v <= 0 for v in limits.values()):
        raise SafetyError("Thresholds must be positive bytes for known categories")
    if file_threshold <= 0:
        raise SafetyError("File-count threshold must be positive")
    if max_entries <= 0 or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise SafetyError("Inventory budgets must be positive")
    selected = sorted({absolute(p) for p in roots}, key=lambda p: (len(p.parts), str(p)))
    if not selected:
        raise SafetyError("pressure requires explicitly selected roots")
    # Nested roots are merged only on the same device. Cross-device roots must be explicit.
    actual = []
    for root in selected:
        no_links(root)
        if any(root == p or root.is_relative_to(p) for p in SYSTEM_SKIP):
            raise SafetyError("Runtime pseudo-filesystems cannot be inventory roots")
        if not root.is_dir():
            raise SafetyError(f"Not a readable directory: {root}")
        device = root.stat().st_dev
        if not any(root.is_relative_to(p) and device == p.stat().st_dev for p in actual):
            actual.append(root)
    records: dict[str, dict] = {}
    gaps: list[dict] = []
    started = time.monotonic()
    entries = 0
    stopped = False
    gap_count = 0

    def gap(path, reason):
        nonlocal gap_count
        gap_count += 1
        # Bound diagnostics too; do not hide that errors were truncated.
        if len(gaps) < 100:
            gaps.append({"path": str(path), "reason": reason})

    def candidate(path, category, parent):
        key = str(path)
        records[key] = {"path": key, "category": category, "bytes": 0, "files": 0,
                        "threshold_bytes": limits[category], "file_threshold": file_threshold,
                        "parent_candidate": parent,
                        "label": "review", "source": "directory-snapshot"}
        return key

    def walk(path, device, ancestors):
        nonlocal entries, stopped
        if stopped:
            return
        if len(path.parts) > 128:
            gap(path, "directory depth budget reached")
            return
        try:
            no_links(path)
            with os.scandir(path) as stream:
                for entry in stream:
                    if entries >= max_entries or time.monotonic()-started >= max_seconds:
                        stopped = True
                        gap(path, "inventory budget reached; byte totals are lower bounds")
                        return
                    entries += 1
                    p = Path(entry.path)
                    if p in SYSTEM_SKIP or entry.name in SKIP or (state_home and p == state_home):
                        continue
                    if entry.name == ".snapshots":
                        gap(p, "snapshot storage skipped; use filesystem-native space accounting")
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if info.st_dev != device:
                                gap(p, "mount boundary skipped; select this root explicitly")
                                continue
                            category = PATH_CATEGORIES.get(p, NAMES.get(entry.name))
                            chain = ancestors
                            if category:
                                key = candidate(p, category, ancestors[-1] if ancestors else None)
                                chain = (*ancestors, key)
                            walk(p, device, chain)
                            if stopped:
                                return
                        elif stat.S_ISREG(info.st_mode):
                            for key in ancestors:
                                records[key]["bytes"] += info.st_size
                                records[key]["files"] += 1
                    except OSError as exc:
                        gap(p, type(exc).__name__)
        except (OSError, SafetyError) as exc:
            gap(path, type(exc).__name__)

    for root in actual:
        if stopped:
            gap(root, "root not fully visited; inventory budget exhausted")
            continue
        category = PATH_CATEGORIES.get(root, NAMES.get(root.name))
        ancestors = (candidate(root, category, None),) if category else ()
        walk(root, root.stat().st_dev, ancestors)
    for record in records.values():
        record["reasons"] = []
        if record["bytes"] >= record["threshold_bytes"]:
            record["reasons"].append("logical-size")
        if record["files"] >= file_threshold:
            record["reasons"].append("file-count")
        record["attention"] = bool(record["reasons"])
        record["complete"] = not gaps
        record["note"] = ("Directory pressure observation; review purpose, growth and references. "
                          "Not cleanup permission; directory name does not establish disposability.")
    disks = []
    seen_devices = set()
    for root in actual:
        device = root.stat().st_dev
        if device not in seen_devices:
            seen_devices.add(device)
            usage = shutil.disk_usage(root)
            percent = 100 * usage.used / usage.total if usage.total else 0
            disks.append({"root": str(root), "used_percent": round(percent, 2), "free_bytes": usage.free,
                          "attention": percent >= 80 or usage.free < 5120*MIB,
                          "urgent": percent >= 90 or usage.free < 2048*MIB})
    return {"roots": [str(p) for p in actual], "items": sorted(records.values(), key=lambda r: -r["bytes"]),
            "disks": disks,
            "entries": entries, "complete": not gaps, "gaps": gaps,
            "gap_count": gap_count, "gaps_truncated": gap_count > len(gaps),
            "thresholds": limits, "measurement": "logical bytes; nested candidates overlap; hard links may repeat",
            "mode": "preview"}


def save_reviews(store, result: dict) -> int:
    """Separate directory notes never affect per-file labels or cleanup authorization."""
    with store.transaction():
        store.db.execute("CREATE TABLE IF NOT EXISTS directory_reviews ("
                         "path TEXT PRIMARY KEY, observed REAL NOT NULL, record TEXT NOT NULL)")
        import json
        for item in result["items"]:
            if item["attention"]:
                store.db.execute("INSERT INTO directory_reviews VALUES(?,?,?) "
                                 "ON CONFLICT(path) DO UPDATE SET observed=excluded.observed,record=excluded.record",
                                 (item["path"], time.time(), json.dumps(item)))
    return sum(item["attention"] for item in result["items"])


def reviews(store) -> list[dict]:
    import json
    exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='directory_reviews'").fetchone()
    if not exists:
        return []
    return [{**json.loads(row["record"]), "observed": row["observed"]}
            for row in store.db.execute("SELECT * FROM directory_reviews ORDER BY observed DESC LIMIT 1000")]
