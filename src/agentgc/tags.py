"""Small, best-effort file labels; never used to authorize cleanup."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys

from .paths import SafetyError, regular


def write_tag(path: Path, payload: dict) -> str:
    try:
        regular(path)
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
        if len(data) > 1024:
            return "unsupported: tag too large"
        if sys.platform.startswith("linux"):
            os.setxattr(path, "user.agentgc", data, follow_symlinks=False)
            return "xattr"
        if sys.platform == "darwin":
            lib = ctypes.CDLL(None, use_errno=True)
            lib.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                    ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
            lib.setxattr.restype = ctypes.c_int
            if lib.setxattr(os.fsencode(path), b"io.agentgc.meta", data, len(data), 0, 1) != 0:
                raise OSError(ctypes.get_errno(), "setxattr failed")
            return "xattr"
        if os.name == "nt":
            # Unsupported volumes reject ADS. Never replace the primary stream.
            with open(str(path) + ":agentgc", "wb") as stream:
                stream.write(data)
            return "ads"
        return "unsupported"
    except (OSError, AttributeError, SafetyError) as exc:
        return f"unavailable: {type(exc).__name__}"
