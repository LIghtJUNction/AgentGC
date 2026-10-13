"""Bounded cleanup for direct subprocesses owned by the test suite."""
from __future__ import annotations

import subprocess


def stop_process(process):
    """Terminate, escalate if needed, reap, and close captured pipes.

    Wait for the direct child rather than draining output during cleanup:
    descendants may still hold a pipe open. This does not terminate descendants.
    A failure to reap remains visible to the caller.
    """
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    finally:
        try:
            if process.stdout is not None:
                process.stdout.close()
        finally:
            if process.stderr is not None:
                process.stderr.close()
