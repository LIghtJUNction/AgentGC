"""The agcc command line. Machine output is JSON; errors go to stderr."""

from __future__ import annotations

import argparse
from importlib.resources import files as package_files
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

from . import __version__
from .cleanup import mark, plan, quarantine_one, recover, restore
from .paths import Scope, SafetyError, absolute, default_home, no_links
from .store import LABELS, Store
from .tracker import scan_once, track


def positive(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(prog="agcc", description="Track and manage files left by AI agents.")
    app.add_argument("--version", action="version", version=f"agcc {__version__}")
    app.add_argument("--home", type=Path, default=default_home(), help="private state directory (or AGENTGC_HOME)")
    app.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub = app.add_subparsers(dest="action", required=True)
    for name in ("scan", "run", "watch"):
        cmd = sub.add_parser(name, help={"scan": "index a workspace once", "run": "track a command",
                                        "watch": "track already-running tools"}[name])
        cmd.add_argument("--root", action="append", help="watch root; repeat for more roots; default: current directory")
        cmd.add_argument("--exclude", action="append", default=[], help="exclude a relative glob or a matching path component")
        if name != "scan":
            cmd.add_argument("--agent", default="unknown", help="session label, not verified process attribution")
            cmd.add_argument("--backend", choices=("auto", "native", "poll"), default="auto")
            cmd.add_argument("--interval", type=positive, default=1, help="polling interval in seconds (minimum 0.1)")
            cmd.add_argument("--reconcile", type=positive, default=60, help="native full-scan interval in seconds")
            cmd.add_argument("--tag-files", action="store_true", help="also write optional xattr/ADS hints")
            if name == "run":
                cmd.add_argument("command", nargs=argparse.REMAINDER, help="-- executable [arguments]")
            else:
                cmd.add_argument("--duration", type=positive, help="stop after this many seconds")
    for name in ("list", "search"):
        cmd = sub.add_parser(name, help="query the file index")
        if name == "search":
            cmd.add_argument("query")
        cmd.add_argument("--sort", choices=("time", "size", "activity"), default="time")
        cmd.add_argument("--label", choices=LABELS)
        cmd.add_argument("--all", action="store_true", help="include missing files")
        cmd.add_argument("--limit", type=int, default=100)
        cmd.add_argument("--offset", type=int, default=0)
        cmd.add_argument("--session", help="files with retained events in this session")
    cmd = sub.add_parser("history", help="read metadata change history (not file contents)")
    cmd.add_argument("path", nargs="?")
    cmd.add_argument("--session")
    cmd.add_argument("--limit", type=int, default=100)
    cmd = sub.add_parser("mark", help="classify an indexed file")
    cmd.add_argument("path", type=Path)
    cmd.add_argument("label", choices=LABELS)
    cmd.add_argument("--note", default="")
    cmd.add_argument("--tag-files", action="store_true")
    cmd = sub.add_parser("gc", help="preview or quarantine explicitly reviewed disposable files")
    cmd.add_argument("paths", nargs="*")
    cmd.add_argument("--limit", type=int, default=1000)
    cmd.add_argument("--apply", action="store_true")
    cmd.add_argument("--confirm-unused", action="store_true", help="confirm all writers stopped and files have no needed references")
    cmd = sub.add_parser("restore", help="restore without overwriting any existing file")
    cmd.add_argument("id")
    sub.add_parser("quarantine", help="list reversible file moves")
    cmd = sub.add_parser("recover", help="inspect interrupted moves; never delete content")
    cmd.add_argument("--apply", action="store_true")
    sub.add_parser("sessions", help="list recent tracking sessions")
    cmd = sub.add_parser("end-session", help="close a stale session after stopping its processes")
    cmd.add_argument("id")
    cmd.add_argument("--confirm-stopped", action="store_true")
    sub.add_parser("stats", help="show indexed file counts and logical bytes")
    sub.add_parser("doctor", help="check index health and recovery state")
    cmd = sub.add_parser("prune", help="preview or prune old events from completed sessions")
    cmd.add_argument("--days", type=positive, default=30)
    cmd.add_argument("--apply", action="store_true")
    cmd = sub.add_parser("skill", help="print or install the bundled Agent Skill")
    cmd.add_argument("--install", type=Path, help="parent skills directory; never overwrite an existing skill")
    for cmd in sub.choices.values():
        cmd.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        cmd.add_argument("--home", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return app


def emit(data, machine: bool, stream=None) -> None:
    stream = stream or sys.stdout
    if machine:
        print(json.dumps(data, ensure_ascii=True, sort_keys=True), file=stream)
    elif isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        for item in data:
            if "path" in item and "size" in item and "label" in item:
                print(f"{item['size']:>12}  {item['label']:<10}  {item['source']:<20}  "
                      f"{json.dumps(item['path'], ensure_ascii=False)}", file=stream)
            else:
                print(json.dumps(item, ensure_ascii=True, sort_keys=True), file=stream)
    else:
        print(json.dumps(data, ensure_ascii=True, indent=2), file=stream)


def execute(args, store: Store) -> tuple[object, int]:
    action = args.action
    if action in ("scan", "run", "watch"):
        scope = Scope(args.root or [Path.cwd()], store.home, args.exclude)
        if action == "scan":
            result = scan_once(store, scope)
            return result, 1 if result["errors"] else 0
        command = None
        if action == "run":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise SafetyError("run requires -- executable [arguments]")
        code, sid = track(store, scope, args.agent, args.backend, args.interval, args.reconcile,
                          command, getattr(args, "duration", None), args.tag_files)
        return {"session": sid, "exit_code": code}, code
    if action in ("list", "search"):
        return store.list_files(getattr(args, "query", ""), args.sort, args.label, args.all,
                                 args.limit, args.offset, args.session), 0
    if action == "history":
        if not 1 <= args.limit <= 10000:
            raise SafetyError("limit must be between 1 and 10000")
        terms, values = [], []
        if args.path:
            terms.append("path=?")
            values.append(str(absolute(args.path)))
        if args.session:
            terms.append("session_id=?")
            values.append(args.session)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(terms) if terms else "")
        return [dict(r) for r in store.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*values, args.limit))], 0
    if action == "mark":
        return mark(store, args.path, args.label, args.note, args.tag_files), 0
    if action == "gc":
        if args.apply and not args.confirm_unused:
            raise SafetyError("--apply requires --confirm-unused; stop writers and review references first")
        result = plan(store, args.paths, args.limit)
        if args.apply:
            result["mode"], result["moved"] = "quarantine", []
            for item in result["items"]:
                if item["eligible"]:
                    try:
                        result["moved"].append(quarantine_one(store, Path(item["path"])))
                    except (OSError, SafetyError) as exc:
                        item["eligible"], item["reason"] = False, str(exc)
            return result, 1 if any(not i["eligible"] for i in result["items"]) else 0
        return result, 0
    if action == "restore":
        return restore(store, args.id), 0
    if action == "quarantine":
        return [dict(r) for r in store.db.execute("SELECT * FROM quarantine ORDER BY created DESC LIMIT 1000")], 0
    if action == "recover":
        return {"mode": "apply" if args.apply else "preview", "items": recover(store, args.apply)}, 0
    if action == "sessions":
        return [dict(r) for r in store.db.execute("SELECT * FROM sessions ORDER BY started DESC LIMIT 100")], 0
    if action == "end-session":
        if not args.confirm_stopped:
            raise SafetyError("Requires --confirm-stopped after stopping the wrapper AND its descendants")
        row = store.db.execute("SELECT status FROM sessions WHERE id=?", (args.id,)).fetchone()
        if row is None or row[0] != "running":
            raise SafetyError("No running session with that ID")
        store.end_session(args.id, 1, "closed-by-user")
        return {"id": args.id, "status": "closed-by-user"}, 0
    if action == "stats":
        return store.stats(), 0
    if action == "doctor":
        check = store.db.execute("PRAGMA quick_check").fetchone()[0]
        state_bytes = sum(p.stat().st_size for p in store.home.glob("index.sqlite3*") if p.is_file())
        return {"integrity": check, "home": str(store.home), "state_bytes": state_bytes,
                "native_backend": "inotify" if sys.platform.startswith("linux") else "not implemented; polling available",
                "pending_recovery": recover(store), **store.stats()}, 0 if check == "ok" else 1
    if action == "prune":
        cutoff = time.time() - args.days * 86400
        condition = "at<? AND (session_id IS NULL OR session_id IN (SELECT id FROM sessions WHERE status<>'running'))"
        with store.transaction():
            count = store.db.execute("SELECT COUNT(*) FROM events WHERE " + condition, (cutoff,)).fetchone()[0]
            if args.apply:
                store.db.execute("DELETE FROM events WHERE " + condition, (cutoff,))
        return {"mode": "apply" if args.apply else "preview", "events": count,
                "note": "Index, labels, and quarantine journal are retained. SQLite reuses freed pages."}, 0
    raise SafetyError(f"Unknown command: {action}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = None
    try:
        if args.action == "skill":
            content = package_files("agentgc").joinpath("skills/agentgc/SKILL.md").read_text(encoding="utf-8")
            if args.install:
                folder = absolute(args.install) / "agentgc"
                folder.mkdir(parents=True, exist_ok=True)
                no_links(folder)
                path = folder / "SKILL.md"
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(content)
                emit({"installed": str(path)}, args.json)
            elif args.json:
                emit({"content": content}, True)
            else:
                print(content, end="")
            return 0
        store = Store(args.home)
        result, code = execute(args, store)
        # Child stdout stays untouched, including when --json is used.
        emit(result, args.json, sys.stderr if args.action == "run" else sys.stdout)
        return code
    except (OSError, ValueError, sqlite3.Error) as exc:
        if args.json:
            emit({"error": str(exc)}, True, sys.stderr)
        else:
            print(f"agcc: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        if store:
            store.close()
