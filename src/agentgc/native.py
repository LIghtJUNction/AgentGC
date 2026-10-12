"""Linux inotify without a runtime dependency. Other systems use polling."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import select
import struct
import sys

from .paths import Scope, SafetyError

# Content events only: IN_ATTRIB would include our own file tag writes.
MASK = 0x2 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800
HEADER = struct.Struct("iIII")


class Inotify:
    name = "inotify"

    def __init__(self, scope: Scope, max_watches: int = 8192):
        if not sys.platform.startswith("linux"):
            raise OSError("Native tracking is currently implemented only on Linux")
        self.scope = scope
        self.max_watches = max_watches
        self.watches: dict[int, Path] = {}
        self.lib = ctypes.CDLL(None, use_errno=True)
        self.lib.inotify_init1.argtypes = [ctypes.c_int]
        self.lib.inotify_init1.restype = ctypes.c_int
        self.lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.lib.inotify_add_watch.restype = ctypes.c_int
        self.lib.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.lib.inotify_rm_watch.restype = ctypes.c_int
        self.fd = self.lib.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "Cannot initialize inotify")
        try:
            self.refresh()
        except BaseException:
            self.close()
            raise

    def refresh(self) -> None:
        visited: set[int] = set()
        errors: list[str] = []
        for root in self.scope.roots:
            for path in self.scope.walk(root, errors, directories=True):
                if len(visited) >= self.max_watches:
                    raise OSError(f"Watch limit ({self.max_watches}) reached; use --backend poll")
                # IN_ONLYDIR | IN_DONT_FOLLOW protect watch registration.
                wd = self.lib.inotify_add_watch(self.fd, os.fsencode(path), MASK | 0x01000000 | 0x02000000)
                if wd < 0:
                    raise OSError(ctypes.get_errno(), f"Cannot watch {path}")
                self.watches[wd] = path
                visited.add(wd)
        for wd in self.watches.keys() - visited:
            self.lib.inotify_rm_watch(self.fd, wd)
            del self.watches[wd]
        if errors:
            raise OSError("; ".join(errors))

    def read(self, timeout: float) -> tuple[list[tuple[Path, str]], bool, bool]:
        """Return bounded file notices, rescan-needed, and queue-overflow flags."""
        notices: list[tuple[Path, str]] = []
        rescan = overflow = False
        if not select.select([self.fd], [], [], timeout)[0]:
            return notices, rescan, overflow
        for _ in range(8):
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            pos = 0
            while pos + HEADER.size <= len(data):
                wd, mask, _cookie, length = HEADER.unpack_from(data, pos)
                pos += HEADER.size
                name = os.fsdecode(data[pos:pos + length].split(b"\0", 1)[0])
                pos += length
                if mask & 0x4000:  # IN_Q_OVERFLOW: provenance may have a gap.
                    rescan = overflow = True
                    continue
                folder = self.watches.get(wd)
                if folder is None or mask & 0x8000:  # IN_IGNORED
                    continue
                path = folder / name if name else folder
                if not self.scope.includes(path):
                    continue
                if mask & 0x40000000 or not name:
                    rescan = True
                    continue
                kind = ("deleted" if mask & (0x200 | 0x40) else
                        "created" if mask & (0x100 | 0x80) else "modified")
                if len(notices) < 4096:
                    notices.append((path, kind))
                else:
                    rescan = overflow = True
        return notices, rescan, overflow

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def make_backend(scope: Scope, requested: str) -> tuple[Inotify | None, str | None]:
    if requested == "poll":
        return None, None
    try:
        return Inotify(scope), None
    except (OSError, AttributeError, SafetyError) as exc:
        if requested == "native":
            raise SafetyError(str(exc)) from exc
        return None, f"Using polling instead of native events: {exc}"
