"""Linux inotify without a runtime dependency. Other systems use polling."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
from pathlib import Path
import select
import struct
import sys

from .paths import Scope, SafetyError, no_links

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
        self.coverage_errors: list[str] = []
        self.control_wd: int | None = None
        self.wake_fd: int | None = None
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
        failed_paths: list[Path] = []
        existing_paths = {path: wd for wd, path in self.watches.items()}
        for root in self.scope.roots:
            for path in self.scope.walk(root, errors, directories=True, failed_paths=failed_paths):
                if (len(visited) + (self.control_wd is not None) >= self.max_watches or
                        (path not in existing_paths and
                         len(self.watches) + (self.control_wd is not None) >= self.max_watches)):
                    raise OSError(f"Watch limit ({self.max_watches}) reached; use --backend poll")
                # IN_ONLYDIR | IN_DONT_FOLLOW protect watch registration.
                wd = self.lib.inotify_add_watch(self.fd, os.fsencode(path), MASK | 0x01000000 | 0x02000000)
                if wd < 0:
                    code = ctypes.get_errno()
                    # Path-local failures must not discard healthy native watches.
                    # Exhausted kernel resources and descriptor failures still fail.
                    if code not in (errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                        raise OSError(code, f"Cannot watch {path}")
                    if code in (errno.EACCES, errno.EPERM):
                        failed_paths.append(path)
                    if len(errors) < 20:
                        errors.append(f"{path}: {OSError(code, os.strerror(code))}")
                    continue
                previous = existing_paths.get(path)
                if previous is not None and previous != wd and previous in self.watches:
                    self.lib.inotify_rm_watch(self.fd, previous)
                    del self.watches[previous]
                self.watches[wd] = path
                visited.add(wd)
        for wd in self.watches.keys() - visited:
            # Retain already registered watches beneath temporarily unreadable
            # subtrees. Their coverage remains unknown, not proven complete.
            if any(self.watches[wd].is_relative_to(p) for p in failed_paths):
                continue
            self.lib.inotify_rm_watch(self.fd, wd)
            del self.watches[wd]
        self.coverage_errors = errors
        if not self.watches:
            raise OSError("No readable native watch directories; " + "; ".join(errors))

    def watch_control(self, path: Path) -> None:
        """Wake for private control events without indexing state as user files."""
        no_links(path)
        if self.control_wd is not None:
            raise SafetyError("A control watch is already registered")
        if len(self.watches) >= self.max_watches:
            raise OSError(f"Watch limit ({self.max_watches}) reached; use --backend poll")
        wd = self.lib.inotify_add_watch(self.fd, os.fsencode(path), MASK | 0x01000000 | 0x02000000)
        if wd < 0:
            raise OSError(ctypes.get_errno(), "Cannot watch private control directory")
        self.control_wd = wd

    def read(self, timeout: float) -> tuple[list[tuple[Path, str]], bool, bool]:
        """Return bounded file notices, rescan-needed, and queue-overflow flags."""
        notices: list[tuple[Path, str]] = []
        rescan = overflow = False
        descriptors = [self.fd]
        if self.wake_fd is not None:
            descriptors.append(self.wake_fd)
        ready = select.select(descriptors, [], [], timeout)[0]
        if self.wake_fd is not None and self.wake_fd in ready:
            with contextlib.suppress(BlockingIOError):
                os.read(self.wake_fd, 4096)
        if self.fd not in ready:
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
                if wd == self.control_wd:
                    continue  # Durable stop requests are checked by the tracker.
                if mask & 0x8000:  # IN_IGNORED: free budget and retry surviving paths.
                    self.watches.pop(wd, None)
                    rescan = True
                    continue
                folder = self.watches.get(wd)
                if folder is None:
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

    def pending(self) -> bool:
        """Check for another batch, including batches containing only directory events."""
        return bool(select.select([self.fd], [], [], 0)[0])

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
