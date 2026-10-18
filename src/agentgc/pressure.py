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


def logical_measurement_basis() -> dict:
    """Describe observed file sizes without implying unique physical storage."""
    return {"kind": "logical-file-size", "source": "os.stat_result.st_size",
            "scope": "observed regular files", "physical_allocation": "not-measured",
            "shared_extents": "not-measured", "hard_links": "may-repeat",
            "nested_candidates": "overlap", "snapshot": "non-atomic",
            "incomplete_totals": "observed-lower-bound"}


def disk_measurement_basis() -> dict:
    """State the platform counter source; no filesystem-native allocation query."""
    if os.name == "posix":
        backend = "os.statvfs"
        total_basis = "f_blocks * f_frsize"
        used_basis = "(f_blocks - f_bfree) * f_frsize"
        free_basis = "f_bavail * f_frsize; space available to the current user"
    elif os.name == "nt":
        backend = "nt._getdiskusage (Windows API)"
        total_basis = "total bytes reported by the Windows API through shutil.disk_usage"
        used_basis = "total_bytes - free_bytes"
        free_basis = "free bytes reported by the Windows API through shutil.disk_usage"
    else:
        backend = "platform-dependent"
        total_basis = "total bytes reported by shutil.disk_usage"
        used_basis = "used bytes reported by shutil.disk_usage"
        free_basis = "free bytes reported by shutil.disk_usage"
    return {"kind": "filesystem-capacity", "source": "shutil.disk_usage",
            "backend": backend, "total_bytes_basis": total_basis,
            "used_bytes_basis": used_basis, "free_bytes_basis": free_basis,
            "used_percent_formula": "100 * used_bytes / total_bytes",
            "df_style_used_percent_formula": "100 * used_bytes / (used_bytes + free_bytes)",
            "physical_allocation": "not-measured", "shared_extents": "not-measured",
            "filesystem_type": "unknown",
            "caveat": "Reserved space can change percentage denominators. Filesystem-native "
                      "allocation, compression, shared extents and multi-device accounting "
                      "are not measured; this is not a measurement of reclaimable space."}


def inode_usage(root: Path) -> dict:
    """Sample usable inode capacity once; unknown is not zero or safe."""
    result = {"inode_status": "unknown", "total_inodes": None, "free_inodes": None,
              "inode_used_percent": None, "inode_attention": None, "inode_urgent": None,
              "inode_reason": None}
    sample = getattr(os, "statvfs", None)
    if sample is None:
        return {**result, "inode_status": "unsupported", "inode_reason": "statvfs unavailable"}
    try:
        info = sample(root)
        total, available = info.f_files, info.f_favail
    except (OSError, AttributeError, NotImplementedError) as exc:
        return {**result, "inode_reason": "inode metadata unavailable: " + type(exc).__name__}
    if type(total) is not int or total < 0:
        return {**result, "inode_reason": "invalid inode capacity"}
    if total == 0:
        return {**result, "inode_status": "unsupported",
                "inode_reason": "filesystem reports no fixed inode capacity"}
    if type(available) is not int or not 0 <= available <= total:
        return {**result, "inode_reason": "invalid usable free inode count"}
    percent = 100 * (total - available) / total
    return {**result, "inode_status": "known", "total_inodes": total, "free_inodes": available,
            "inode_used_percent": round(percent, 2), "inode_attention": percent >= 80,
            "inode_urgent": percent >= 90}


def byte_pressure(root: Path, role: str = 'auto') -> dict:
    """Capacity-scaled headroom; conventional boot mount detection is a hint."""
    if role not in ('auto', 'general', 'boot'):
        raise SafetyError('disk role must be auto, general or boot')
    effective, source = ('general', 'default') if role == 'auto' else (role, 'explicit')
    if role == 'auto':
        # Never infer boot policy from a directory name alone: it must be a
        # conventional mount point on the same selected filesystem/device.
        try:
            device = root.stat().st_dev
            for ancestor in (root, *root.parents):
                if (str(ancestor) in ('/boot', '/boot/efi', '/efi') and
                        os.path.ismount(ancestor) and ancestor.stat().st_dev == device):
                    effective, source = 'boot', 'conventional-boot-mount'
                    break
        except OSError:
            source = 'default-mount-detection-unavailable'
    result = {'disk_role': effective, 'disk_role_source': source,
              'capacity_status': 'unknown', 'capacity_reason': None,
              'total_bytes': None, 'used_bytes': None, 'used_percent': None, 'free_bytes': None,
              'df_style_used_percent': None, 'measurement_basis': disk_measurement_basis(),
              'attention_free_bytes': None, 'urgent_free_bytes': None,
              'byte_attention': None, 'byte_urgent': None}
    try:
        usage = shutil.disk_usage(root)
        total, used, free = usage.total, usage.used, usage.free
        if (any(type(n) is not int for n in (total, used, free)) or total <= 0 or
                not 0 <= used <= total or not 0 <= free <= total):
            return {**result, 'capacity_reason': 'invalid byte capacity counters'}
    except (OSError, NotImplementedError) as exc:
        return {**result, 'capacity_reason': 'byte metadata unavailable: ' + type(exc).__name__}
    percent = 100 * used / total
    available_total = used + free
    df_style_percent = 100 * used / available_total if available_total else None
    attention_floor, urgent_floor = (64*MIB, 32*MIB) if effective == 'boot' else (5120*MIB, 2048*MIB)
    attention_floor = min(attention_floor, total // 10)
    urgent_floor = min(urgent_floor, total // 20)
    return {**result, 'capacity_status': 'known', 'total_bytes': total,
            'used_bytes': used, 'used_percent': round(percent, 2), 'free_bytes': free,
            'df_style_used_percent': round(df_style_percent, 2) if df_style_percent is not None else None,
            'attention_free_bytes': attention_floor, 'urgent_free_bytes': urgent_floor,
            'byte_attention': percent >= 80 or free < attention_floor,
            'byte_urgent': percent >= 90 or free < urgent_floor}


def inventory(roots: list[Path], thresholds: dict[str, int] | None = None,
              max_entries: int = 200000, max_seconds: float = 30,
              state_home: Path | None = None, file_threshold: int = 100000,
              select=None, include=None, disk_role: str = 'auto') -> dict:
    if disk_role not in ('auto', 'general', 'boot'):
        raise SafetyError('disk role must be auto, general or boot')
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
                        "label": "review", "source": "directory-snapshot",
                        "measurement_basis": logical_measurement_basis()}
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
                    if (p in SYSTEM_SKIP or entry.name in SKIP or (state_home and p == state_home)
                            or (include and not include(p))):
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
                            if category or (select and select(p, 'directory')):
                                key = candidate(p, category or 'protected', ancestors[-1] if ancestors else None)
                                chain = (*ancestors, key)
                            walk(p, device, chain)
                            if stopped:
                                return
                        elif stat.S_ISREG(info.st_mode):
                            if select and select(p, 'file'):
                                key = candidate(p, 'protected', ancestors[-1] if ancestors else None)
                                records[key]['kind'] = 'file'
                                records[key]['bytes'] = info.st_size
                                records[key]['files'] = 1
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
        ancestors = (candidate(root, category or 'protected', None),) if category or (select and select(root, 'directory')) else ()
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
            capacity = byte_pressure(root, disk_role)
            inodes = inode_usage(root)
            disks.append({"root": str(root), **capacity, **inodes,
                          "attention": True if inodes["inode_attention"] is True else capacity['byte_attention'],
                          "urgent": True if inodes["inode_urgent"] is True else capacity['byte_urgent']})
    return {"roots": [str(p) for p in actual], "items": sorted(records.values(), key=lambda r: -r["bytes"]),
            "disks": disks,
            "entries": entries, "complete": not gaps, "gaps": gaps,
            "gap_count": gap_count, "gaps_truncated": gap_count > len(gaps),
            "thresholds": limits, "measurement": "logical bytes; nested candidates overlap; hard links may repeat",
            "measurement_basis": logical_measurement_basis(),
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
