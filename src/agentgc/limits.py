"""Bound tracking work and state growth without deleting historical evidence."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import stat
import sys

from .paths import SafetyError, absolute, no_links

MIB = 1024 * 1024


class TrackingLimit(SafetyError):
    """Tracking stopped at a resource limit; surviving files are not missing."""


def memory_filesystem(home: Path) -> bool:
    """Recognize Linux tmpfs/ramfs, including nested mounts and missing homes."""
    if not sys.platform.startswith("linux"):
        return False
    selected = absolute(home)
    try:
        with Path("/proc/self/mountinfo").open(encoding="utf-8") as stream:
            text = stream.read(1024 * 1024 + 1)
    except OSError as exc:
        raise SafetyError("Cannot inspect state filesystem; choose a verified disk-backed state home") from exc
    if len(text) > 1024 * 1024:
        raise SafetyError("Mount metadata exceeds the state-filesystem inspection budget")
    best, kind = -1, None
    for line in text.splitlines():
        fields, separator, filesystem = line.partition(" - ")
        parts, tail = fields.split(), filesystem.split()
        if not separator or len(parts) < 6 or not tail:
            continue
        mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), parts[4]))
        if selected.is_relative_to(mount) and len(mount.parts) >= best:
            best, kind = len(mount.parts), tail[0]
    if kind is None:
        raise SafetyError("Cannot identify the state filesystem")
    return kind in ("tmpfs", "ramfs")


def check_state_location(home: Path, allow_memory_state: bool = False) -> None:
    if not allow_memory_state and memory_filesystem(home):
        raise SafetyError(
            "Tracking state on tmpfs/ramfs consumes memory. Reuse a stable disk-backed --home "
            "(default: ~/.local/state/agentgc on Linux); do not create a state database per command. "
            "Use --allow-memory-state only for an intentional, bounded temporary experiment."
        )


@dataclass(frozen=True)
class TrackingLimits:
    max_files: int = 10000
    max_state_mib: float = 256

    def __post_init__(self) -> None:
        if not isinstance(self.max_files, int) or isinstance(self.max_files, bool) or self.max_files <= 0:
            raise SafetyError("max-files must be a positive integer")
        if not math.isfinite(self.max_state_mib) or self.max_state_mib <= 0:
            raise SafetyError("Tracking state budget must be finite and positive")

    def check_state(self, home: Path) -> None:
        existing = home
        while True:
            try:
                existing.lstat()
                break
            except FileNotFoundError:
                existing = existing.parent
        no_links(existing)
        size = 0
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = home / ("index.sqlite3" + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SafetyError("State database and sidecars must be regular, non-linked files")
            size += info.st_size
        if size >= self.max_state_mib * MIB:
            raise TrackingLimit(
                f"Tracking state budget reached ({size} bytes; --max-state-mib {self.max_state_mib:g}). "
                "Tracking stopped; existing records are retained. Preview authorized history retention with "
                "'agcc prune --days 30 --compact'; pruning and compaction require explicit --apply. "
                "Review this store or explicitly choose "
                "a larger budget. Do not rotate to fresh databases to bypass the limit."
            )

    def check_scan(self, files: int) -> None:
        if files > self.max_files:
            raise TrackingLimit(f"Scan file budget reached (--max-files {self.max_files}); "
                                "select narrower roots or explicitly raise the budget")
