"""Small reproducible resource check; never uses an existing workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentgc.native import Inotify  # noqa: E402
from agentgc.paths import Scope  # noqa: E402
from agentgc.store import Store  # noqa: E402
from agentgc.tracker import scan_once  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--idle-seconds", type=float, default=1)
    args = parser.parse_args()
    if not 1 <= args.files <= 100000 or not 0 < args.idle_seconds <= 60:
        parser.error("files must be 1..100000; idle-seconds must be >0 and <=60")
    with tempfile.TemporaryDirectory(prefix="agentgc-bench-") as temp:
        base = Path(temp).resolve()
        root = base / "work"
        root.mkdir()
        for index in range(args.files):
            (root / f"item-{index:06}.txt").write_text("sample\n", encoding="utf-8")
        store = Store(base / "state")
        try:
            scope = Scope([root], store.home)
            started = time.perf_counter()
            scan_once(store, scope)
            initial = time.perf_counter() - started
            before = store.stats()["events"]
            started = time.perf_counter()
            scan_once(store, scope)
            unchanged = time.perf_counter() - started
            added = store.stats()["events"] - before
            tracemalloc.start()
            scan_once(store, scope)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            result = {"files": args.files, "python": sys.version.split()[0], "platform": sys.platform,
                      "initial_scan_seconds": round(initial, 6), "unchanged_scan_seconds": round(unchanged, 6),
                      "unchanged_scan_new_events": added, "scan_python_allocation_peak_bytes": peak}
            if sys.platform.startswith("linux"):
                observer = Inotify(scope)
                try:
                    started = time.process_time()
                    observer.read(args.idle_seconds)
                    result["native_idle_wait_cpu_seconds"] = round(time.process_time() - started, 6)
                    result["native_idle_wait_wall_seconds_requested"] = args.idle_seconds
                finally:
                    observer.close()
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            result["index_and_sidecar_bytes"] = sum(p.stat().st_size for p in store.home.glob("index.sqlite3*"))
            print(json.dumps(result, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
