"""Path boundaries and metadata. Never follow links into another workspace."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
import stat
import sys
from typing import Iterator

INTERNAL_NAMES = {".git", ".agentgc-quarantine"}


class SafetyError(ValueError):
    """An operation cannot be shown to be safe."""


def absolute(path: str | Path) -> Path:
    """Resolve parent aliases (for example macOS /tmp), but not the final file."""
    value = Path(os.path.abspath(os.path.expanduser(path)))
    return value.parent.resolve() / value.name


def default_home() -> Path:
    if os.environ.get("AGENTGC_HOME"):
        return absolute(os.environ["AGENTGC_HOME"])
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return absolute(base / "agentgc")


def is_link(info: os.stat_result) -> bool:
    # Windows junctions and other reparse points must also be excluded.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def no_links(path: Path) -> None:
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if is_link(info):
            raise SafetyError(f"Link or reparse point is not allowed: {part}")


def regular(path: Path) -> os.stat_result:
    no_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise SafetyError(f"Not a regular file: {path}")
    return info


def signature(info: os.stat_result) -> str:
    # Exclude ctime: our own xattr writes change it. A digest separately protects
    # reviewed files, including same-size writes with a restored mtime.
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"


def digest(path: Path) -> tuple[str, str]:
    before = regular(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if signature(before) != signature(opened):
            raise SafetyError(f"File changed while opening: {path}")
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
        after = os.fstat(stream.fileno())
    if signature(before) != signature(after) or signature(regular(path)) != signature(after):
        raise SafetyError(f"File changed while reading: {path}")
    return signature(after), value.hexdigest()


class Scope:
    def __init__(self, roots: list[str | Path], home: Path, excludes: list[str] | None = None):
        self.home = absolute(home)
        self.excludes = excludes or []
        self.roots: list[Path] = []
        for root in sorted({absolute(r).resolve(strict=True) for r in roots}, key=lambda p: len(p.parts)):
            no_links(root)
            if not root.is_dir():
                raise SafetyError(f"Watch root is not a directory: {root}")
            if root.is_relative_to(self.home) or any(n in INTERNAL_NAMES for n in root.parts):
                raise SafetyError(f"Cannot watch internal state: {root}")
            if not any(root.is_relative_to(old) for old in self.roots):
                self.roots.append(root)
        if not self.roots:
            raise SafetyError("At least one watch root is required")

    def root_for(self, path: Path) -> Path | None:
        return next((r for r in self.roots if path.is_relative_to(r)), None)

    def includes(self, path: Path) -> bool:
        root = self.root_for(path)
        if root is None or path.is_relative_to(self.home):
            return False
        rel = path.relative_to(root)
        if any(name in INTERNAL_NAMES for name in rel.parts):
            return False
        return not any(
            fnmatch.fnmatchcase(rel.as_posix(), pattern)
            or any(fnmatch.fnmatchcase(part, pattern) for part in rel.parts)
            for pattern in self.excludes
        )

    def walk(self, root: Path, errors: list[str], directories: bool = False) -> Iterator[Path]:
        """Stream a walk. Do not build a second in-memory file index."""
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                no_links(folder)
                if directories:
                    yield folder
                with os.scandir(folder) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        if not self.includes(path):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if is_link(info):
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                stack.append(path)
                            elif not directories and stat.S_ISREG(info.st_mode):
                                yield path
                        except OSError as exc:
                            if len(errors) < 20:
                                errors.append(f"{path}: {exc}")
            except (OSError, SafetyError) as exc:
                if len(errors) < 20:
                    errors.append(f"{folder}: {exc}")
