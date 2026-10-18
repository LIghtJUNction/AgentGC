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
from .pressure import DEFAULTS, MIB, inventory, reviews, save_reviews
from .tracker import scan_once, track
from .watch_control import WatchControl, control_command, request_stop, wait_status, watch_status
from .rules import load as load_rules, matches
from .sync import reconcile, marks as automatic_marks, table_exists
from .output import DISPLAY_LIMIT, markdown
from .diagnostics import summarize
from .session_state import session_result
from .watch_lifecycle import start_watch
from .limits import TrackingLimits, check_state_location


def positive(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def bounded_wait(value: str) -> float:
    result = positive(value)
    if result > 300:
        raise argparse.ArgumentTypeError("must be at most 300 seconds")
    return result


def row_count(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 10000:
        raise argparse.ArgumentTypeError('must be between 1 and 10000')
    return result


def file_budget(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 2**63 - 1:
        raise argparse.ArgumentTypeError("must be a positive 64-bit integer")
    return result


def parser() -> argparse.ArgumentParser:
    examples = {
        'agcc': 'Start here:\n'
                '  agcc run --agent assistant --root . -- your-command\n'
                '  agcc list --root . --sort size --limit 20\n'
                '  agcc sessions --root . --status running\n'
                '  agcc mark result.json keep --note "Final deliverable"\n'
                '\nUse agcc COMMAND --help for command options and examples.',
        'run': 'Example:\n  agcc run --agent assistant --root . -- python build.py\n'
               '\nUse -- before the child command. --root selects tracking directories;\n'
               'it does not change the child working directory. Child streams are\n'
               'preserved. The default exit code follows the child; --strict-tracking\n'
               'also fails on partial tracking without changing command_exit_code.',
        'watch-start': 'Example:\n  agcc --json watch-start --root . --agent assistant\n'
                       '\nReturns once the background watcher has a verified baseline. Retain the\n'
                       'returned control and home; stop only your watcher with watch-stop CONTROL.',
        'watch-stop': 'Examples:\n  agcc watch-stop CONTROL\n'
                      '  agcc watch-stop CONTROL --no-wait\n'
                      '\nWaits up to 30 seconds by default. A partial result is nonzero unless\n'
                      '--no-strict-tracking is supplied; outcomes remain explicit in either mode.',
        'session': 'Example:\n  agcc --json session SESSION_ID\n'
                   '\nSchema version 1 keeps command_exit_code and tracking_status independent.\n'
                   'roots is an array; unresolved_event_count counts retained events.',
        'diagnostics': 'Examples:\n  agcc diagnostics --session SESSION_ID\n'
                       '  agcc --details diagnostics --session SESSION_ID\n'
                       '\nDefault output groups evidence by class and stored path prefix. Details\n'
                       'retain original gap text; grouped counts can overlap.',
        'list': 'Examples:\n  agcc list --root . --sort size --limit 20\n'
                '  agcc list --root . --label temporary --offset 20\n'
                '\nWithout --root, query the shared index across all projects.',
        'search': 'Example:\n  agcc search output --root . --limit 20\n'
                  '\nSearch path, note and agent label as literal text.',
        'history': 'Examples:\n  agcc history result.json\n'
                   '  agcc history --root . --limit 20\n'
                   '\nProject history retains session-level gaps from overlapping sessions;\n'
                   'a gap may affect other roots in the same session too.',
        'sessions': 'Examples:\n  agcc sessions --root . --status running\n'
                    '  agcc sessions --root . --limit 20 --offset 20\n'
                    '\nOverlapping roots include a parent watcher covering this project.\n'
                    'Filters apply before pagination; this query never closes sessions.',
        'mark': 'Examples:\n  agcc mark result.json keep --note "Final deliverable"\n'
                '  agcc mark scratch.txt temporary --note "Intermediate output"\n'
                '\nExplicit new regular files are indexed immediately. temporary and\n'
                'review labels do not authorize cleanup.',
        'gc': 'Preview first:\n  agcc gc scratch.txt\n'
              '\nOnly explicitly reviewed disposable files are eligible. Quarantine is\n'
              'reversible and does not free disk space. Applying a preview requires\n'
              '--apply --confirm-unused after stopping writers and reviewing references.',
    }
    app = argparse.ArgumentParser(prog="agcc", description="Track and manage files left by AI agents.",
                                  epilog=examples['agcc'], formatter_class=argparse.RawDescriptionHelpFormatter)
    app.add_argument("--version", action="version", version=f"agcc {__version__}")
    app.add_argument("--home", type=Path, default=default_home(), help="private state directory (or AGENTGC_HOME)")
    app.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    app.add_argument('--rows', type=row_count, default=DISPLAY_LIMIT, help='Markdown display limit, not query limit (default: 20)')
    app.add_argument('--details', action='store_true', help='show all returned Markdown rows; overrides --rows, not query limits')
    app.add_argument("--config", type=Path, help="explicit .agcc configuration")
    app.add_argument("--no-project-rules", action="store_true", help="ignore project configuration, keep core protections")
    sub = app.add_subparsers(dest="action", required=True)
    for name in ("scan", "run", "watch", "watch-start"):
        cmd = sub.add_parser(name, help={"scan": "index a workspace once", "run": "track a command",
                                        "watch": "track already-running tools",
                                        "watch-start": "start a background watcher and wait for verified readiness"}[name])
        cmd.add_argument("--root", action="append", help="watch root; repeat for more roots; default: current directory")
        cmd.add_argument("--exclude", action="append", default=[], help="exclude a relative glob or a matching path component")
        cmd.add_argument('--max-files', type=file_budget, default=10000,
                         help='maximum file candidates per scan across all roots (default: 10000); excess fails visibly')
        cmd.add_argument('--max-state-mib', type=positive, default=256,
                         help='state database and sidecar byte budget in MiB (default: 256); never deletes records')
        cmd.add_argument('--allow-memory-state', action='store_true',
                         help='explicitly allow bounded tracking state on Linux tmpfs/ramfs; prefer stable disk-backed state')
        if name != "scan":
            cmd.add_argument("--agent", default="unknown", help="session label, not verified process attribution")
            cmd.add_argument("--backend", choices=("auto", "native", "poll"), default="auto")
            cmd.add_argument("--interval", type=positive, default=1, help="polling interval in seconds (minimum 0.1)")
            cmd.add_argument("--reconcile", type=positive, default=60, help="native full-scan interval in seconds")
            cmd.add_argument("--tag-files", action="store_true", help="also write optional xattr/ADS hints")
            if name != 'watch-start':
                cmd.add_argument('--strict-tracking', action=argparse.BooleanOptionalAction, default=name == 'watch',
                                 help='return nonzero for partial tracking; run defaults off, watch defaults on')
            if name == "run":
                cmd.add_argument("command", nargs=argparse.REMAINDER, help="-- executable [arguments]")
            else:
                cmd.add_argument("--duration", type=positive, help="stop after this many seconds")
                cmd.add_argument("--control", help="unique cooperative control name; default duration: 3600 seconds")
                if name == 'watch-start':
                    cmd.add_argument('--wait-ready', type=bounded_wait, default=30,
                                     help='wait for baseline and readiness (default: 30 seconds)')
    cmd = sub.add_parser("watch-stop", help="request a controlled watcher to stop and perform its final scan")
    cmd.add_argument("control", help="control name used with watch --control")
    waiting = cmd.add_mutually_exclusive_group()
    waiting.add_argument("--wait", type=bounded_wait, default=30,
                         help="wait for finalization (default: 30 seconds, maximum: 300)")
    waiting.add_argument('--no-wait', action='store_const', const=None, dest='wait',
                         help='request stop only; inspect completion separately')
    cmd.add_argument('--strict-tracking', action=argparse.BooleanOptionalAction, default=True,
                     help='return nonzero for partial tracking (default: on)')
    cmd = sub.add_parser("watch-status", help="inspect one control without listing unrelated sessions")
    cmd.add_argument("control", help="control name used with watch --control")
    cmd.add_argument("--wait-ready", type=bounded_wait, help="wait up to 300 seconds for baseline/readiness; emit one result")
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
        cmd.add_argument("--root", action="append", type=Path,
                         help="filter indexed paths under a directory; repeat for a union; no scan")
    cmd = sub.add_parser("history", help="read metadata change history (not file contents)")
    cmd.add_argument("path", nargs="?")
    cmd.add_argument("--session")
    cmd.add_argument("--limit", type=int, default=100)
    cmd.add_argument('--offset', type=int, default=0)
    cmd.add_argument('--root', action='append', type=Path,
                     help='filter indexed paths and overlapping session-level gaps; no scan')
    cmd = sub.add_parser('session', help='compact command result and tracking quality for one session')
    cmd.add_argument('id')
    cmd = sub.add_parser("diagnostics", help="group retained gaps and explain unresolved event impact")
    cmd.add_argument('--session', required=True)
    cmd = sub.add_parser("mark", help="classify explicit files; index new regular files immediately")
    cmd.add_argument("paths", nargs='*', type=Path)
    cmd.add_argument("label", choices=LABELS)
    cmd.add_argument('--files-from', type=Path, help='UTF-8 path list; - reads stdin; relative paths use current directory')
    cmd.add_argument('--null', action='store_true', help='NUL-delimited path list instead of lines')
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
    cmd = sub.add_parser("sessions", help="list and filter tracking sessions")
    cmd.add_argument('--root', action='append', type=Path, help='sessions with overlapping tracking roots; repeat for a union')
    cmd.add_argument('--status', choices=('running', 'finished', 'failed', 'interrupted', 'closed-by-user'))
    cmd.add_argument('--limit', type=int, default=100, help='query limit, 1..10000 (default: 100)')
    cmd.add_argument('--offset', type=int, default=0)
    cmd = sub.add_parser("end-session", help="close a stale session after stopping its processes")
    cmd.add_argument("id")
    cmd.add_argument("--confirm-stopped", action="store_true")
    sub.add_parser("stats", help="show indexed file counts and logical bytes")
    sub.add_parser("doctor", help="check index health and recovery state")
    cmd = sub.add_parser("pressure", help="bounded directory size checks; thresholds flag review, never cleanup")
    cmd.add_argument("--root", action="append", required=True, type=Path)
    cmd.add_argument('--disk-role', choices=('auto', 'general', 'boot'), default='auto',
                     help='filesystem headroom policy for selected roots; auto detects conventional boot mounts')
    cmd.add_argument("--threshold", action="append", default=[], metavar="CATEGORY=MIB",
                     help="override review threshold in MiB (build, dependencies, cache, logs, quarantine, protected)")
    cmd.add_argument("--max-entries", type=int)
    cmd.add_argument("--file-threshold", type=int, help="review a candidate directory with this many files")
    cmd.add_argument("--max-seconds", type=positive)
    cmd.add_argument("--apply", action="store_true", help="save attention notes only, not file cleanup permission")
    sub.add_parser("reviews", help="list historical directory attention notes; remeasure before acting")
    for name in ('sync', 'auto-mark'):
        cmd = sub.add_parser(name, help='reconcile local project rule observations; never clean files')
        cmd.add_argument('--root', action='append', type=Path)
        cmd.add_argument('--max-entries', type=int)
        cmd.add_argument('--max-seconds', type=positive)
        cmd.add_argument('--disk-role', choices=('auto', 'general', 'boot'), default='auto')
        if name == 'sync':
            cmd.add_argument('--dry-run', action='store_true')
        else:
            cmd.add_argument('--apply', action='store_true')
    cmd = sub.add_parser('rules', help='inspect declarative project rules')
    cmd.add_argument('operation', choices=('show', 'list', 'validate', 'explain'))
    cmd.add_argument('path', nargs='?', type=Path)
    sub.add_parser('auto-marks', help='query separate automatic observations, not file labels')
    cmd = sub.add_parser("prune", help="preview or prune old events from completed sessions")
    cmd.add_argument("--days", type=positive, default=30)
    cmd.add_argument("--apply", action="store_true")
    cmd.add_argument("--rule-history", action="store_true",
                     help="also prune old rule-transition events; skip these while any session is running")
    cmd = sub.add_parser("skill", help="print or install the bundled Agent Skill")
    cmd.add_argument("--install", type=Path, help="parent skills directory; never overwrite an existing skill")
    for name, cmd in sub.choices.items():
        if name in examples:
            cmd.epilog = examples[name]
            cmd.formatter_class = argparse.RawDescriptionHelpFormatter
        cmd.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        cmd.add_argument("--home", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        cmd.add_argument('--rows', type=row_count, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        cmd.add_argument('--details', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        cmd.add_argument('--config', type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        cmd.add_argument('--no-project-rules', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return app


def tracking_summary(store: Store, session: str) -> dict | None:
    """Read retained evidence for presentation, never mutate the command payload."""
    try:
        row = store.db.execute('SELECT status,gaps,backend FROM sessions WHERE id=?', (session,)).fetchone()
        if row is None:
            return None
        return summarize(store, session)
    except sqlite3.Error:
        return None  # unavailable is explicit; child exit codes must not change


def emit(data, machine: bool, stream=None, command: str = 'agcc', offset: int = 0,
         rows: int = DISPLAY_LIMIT, details: bool = False, tracking: dict | None = None) -> None:
    stream = stream or sys.stdout
    if machine:
        print(json.dumps(data, ensure_ascii=True, sort_keys=True), file=stream)
    else:
        print(markdown(data, command, offset, row_limit=rows, details=details, tracking=tracking), end='', file=stream)


def execute(args, store: Store) -> tuple[object, int]:
    action = args.action
    roots_hint = getattr(args,'root',None)
    anchor = roots_hint[0] if roots_hint else None
    config = load_rules(cwd=anchor,config=args.config, disabled=args.no_project_rules) if action in ('scan','run','watch','pressure','sync','auto-mark','rules') else None
    if action == 'auto-marks':
        return automatic_marks(store), 0
    if action == 'rules':
        if args.operation == 'explain':
            if args.path is None:
                raise SafetyError('rules explain requires a path')
            path = absolute(args.path)
            no_links(path)
            kind = 'directory' if path.is_dir() else 'file'
            return {'path':str(path),'config':config['config'],'fingerprint':config['fingerprint'],
                    'matching_rules':[r for r in config['rules'] if matches(r,path,kind,Path(config['project']))],
                    'note':'Path matching only; sync evaluates conditions and records unknown measurements'}, 0
        return config, 0
    if action in ('sync','auto-mark'):
        result = reconcile(store, config, args.root or [Path(config['project'])],
                           dry_run=args.dry_run if action == 'sync' else not args.apply,
                           max_entries=args.max_entries, max_seconds=args.max_seconds, disk_role=args.disk_role)
        return result, 0 if result['complete'] else 1
    if action in ("scan", "run", "watch"):
        scope = Scope(args.root or [Path.cwd()], store.home, args.exclude)
        limits = TrackingLimits(args.max_files, args.max_state_mib)
        if action == "scan":
            result = scan_once(store, scope, limits)
            result.update(session_result(store, result['session']))
            if config['config'] and config['auto']['enabled'] and action in config['auto']['after']:
                try:
                    result['automatic'] = reconcile(store,config,scope.roots,include=scope.includes,excludes=scope.excludes)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    result['automatic'] = {'complete':False,'error':str(exc)}
            return result, 1 if result["errors"] else 0
        command = None
        if action == "run":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise SafetyError("run requires -- executable [arguments]")
        control = WatchControl(store, args.control) if action == "watch" and args.control else None
        duration = getattr(args, "duration", None)
        if control and duration is None:
            duration = 3600
        code, sid = track(store, scope, args.agent, args.backend, args.interval, args.reconcile,
                          command, duration, args.tag_files, control, limits)
        outcome = session_result(store, sid)
        if args.strict_tracking and code == 0 and outcome['tracking_status'] != 'complete':
            code = 1
        elif not args.strict_tracking and action == 'watch' and outcome['tracking_status'] in ('complete', 'partial'):
            code = 0
        result = {**outcome, "exit_code": code, 'cli_exit_code': code}
        if config['config'] and config['auto']['enabled'] and action in config['auto']['after']:
            try:
                result['automatic'] = reconcile(store,config,scope.roots,include=scope.includes,excludes=scope.excludes)
            except (OSError, ValueError, sqlite3.Error) as exc:
                result['automatic'] = {'complete':False,'error':str(exc)}
        return result, code
    if action == "watch-status":
        result = (wait_status(store, args.control, "ready", args.wait_ready) if args.wait_ready is not None
                  else watch_status(store, args.control))
        success = result["ready"] or (args.wait_ready is None and result.get("state") == "finished")
        return result, 0 if success else 1
    if action == "watch-stop":
        result = request_stop(store, args.control)
        if args.wait is not None:
            result = {**result, **wait_status(store, args.control, "finished", args.wait)}
            return result, 0 if result["state"] == "finished" else 1
        return result, 0
    if action in ("list", "search"):
        return store.list_files(getattr(args, "query", ""), args.sort, args.label, args.all,
                                 args.limit, args.offset, args.session, args.root), 0
    if action == "history":
        return store.history(args.path, args.session, args.limit, args.offset, args.root), 0
    if action == 'diagnostics':
        return summarize(store, args.session), 0
    if action == 'session':
        return session_result(store, args.id), 0
    if action == "mark":
        paths = list(args.paths)
        if args.null and args.files_from is None:
            raise SafetyError('--null requires --files-from')
        if args.files_from is not None:
            if str(args.files_from) == '-':
                text = sys.stdin.read()
            else:
                no_links(Path(args.files_from).absolute())
                text = args.files_from.read_text(encoding='utf-8')
            paths.extend(Path(p) for p in (text.split('\0') if args.null else text.splitlines()) if p)
        paths = list(dict.fromkeys(paths))
        if not paths:
            raise SafetyError('mark requires paths or --files-from')
        if len(paths) == 1 and args.files_from is None:
            return mark(store, paths[0], args.label, args.note, args.tag_files), 0
        items, errors = [], []
        for path in paths:
            try:
                items.append(mark(store, path, args.label, args.note, args.tag_files))
            except (OSError, SafetyError) as exc:
                errors.append({'path': str(path), 'reason': str(exc)})
        return {'items': items, 'errors': errors, 'marked': len(items), 'failed': len(errors)}, 1 if errors else 0
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
        return [session_result(store, row) for row in store.list_sessions(args.root, args.status, args.limit, args.offset)], 0
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
    if action == "reviews":
        return reviews(store), 0
    if action == "pressure":
        limits = {}
        for value in args.threshold:
            category, sep, amount = value.partition("=")
            if not sep or category not in DEFAULTS:
                raise SafetyError("threshold must be a known CATEGORY=MIB")
            try:
                mib = positive(amount)
            except argparse.ArgumentTypeError as exc:
                raise SafetyError(str(exc)) from exc
            if mib > (2**63 - 1) / MIB:
                raise SafetyError("threshold exceeds supported byte range")
            limits[category] = int(mib * MIB)
        if config['config']:
            from copy import deepcopy
            import hashlib
            override = deepcopy(config)
            for rule in override['rules']:
                category = rule.get('match',{}).get('category')
                for clause in rule.get('when',{}).get('any',[]):
                    if category in limits and 'size_gte' in clause:
                        clause['size_gte'] = f'{limits[category]} B'
                    if args.file_threshold is not None and 'files_gte' in clause:
                        if args.file_threshold <= 0:
                            raise SafetyError('File-count threshold must be positive')
                        clause['files_gte'] = args.file_threshold
            if limits or args.file_threshold is not None:
                override['fingerprint'] = hashlib.sha256(json.dumps({k:v for k,v in override.items() if k != 'fingerprint'},sort_keys=True).encode()).hexdigest()
            result = reconcile(store,config,args.root,dry_run=not args.apply,
                               max_entries=args.max_entries,max_seconds=args.max_seconds,overrides=override,
                               disk_role=args.disk_role)
            return result, 0 if result['complete'] else 1
        result = inventory(args.root, limits, 200000 if args.max_entries is None else args.max_entries,
                           30 if args.max_seconds is None else args.max_seconds, store.home,
                           100000 if args.file_threshold is None else args.file_threshold, disk_role=args.disk_role)
        if args.apply:
            result["saved_reviews"] = save_reviews(store, result)
            result["mode"] = "mark-review"
        return result, 0 if result["complete"] else 1
    if action == "prune":
        cutoff = time.time() - args.days * 86400
        condition = "at<? AND (session_id IS NULL OR session_id IN (SELECT id FROM sessions WHERE status<>'running'))"
        result = {"mode": "apply" if args.apply else "preview",
                  "note": "Index, labels, and quarantine journal are retained. SQLite reuses freed pages."}
        with store.transaction():
            result["events"] = store.db.execute("SELECT COUNT(*) FROM events WHERE " + condition, (cutoff,)).fetchone()[0]
            if args.apply:
                store.db.execute("DELETE FROM events WHERE " + condition, (cutoff,))
            if args.rule_history:
                # Rule events have no session identity. Protect all of them while
                # any session is running, including stale sessions, under this lock.
                running = store.db.execute("SELECT 1 FROM sessions WHERE status='running' LIMIT 1").fetchone()
                history = {"events": 0, "skipped": bool(running),
                           "note": "Current rule observations and growth baselines are retained."}
                if running:
                    history["reason"] = "A running session exists; rule history cannot be attributed to a completed session."
                elif table_exists(store, "auto_events"):
                    history["events"] = store.db.execute("SELECT COUNT(*) FROM auto_events WHERE at<?", (cutoff,)).fetchone()[0]
                    if args.apply:
                        store.db.execute("DELETE FROM auto_events WHERE at<?", (cutoff,))
                result["rule_history"] = history
        return result, 0
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
                emit({"installed": str(path)}, args.json, command='skill')
            elif args.json:
                emit({"content": content}, True)
            else:
                print(content, end="")
            return 0
        if args.action in ('scan', 'run', 'watch', 'watch-start'):
            check_state_location(args.home, args.allow_memory_state)
            TrackingLimits(args.max_files, args.max_state_mib).check_state(absolute(args.home))
        if args.action == 'watch-start':
            result, code = start_watch(args.home, args.root, args.exclude, agent=args.agent, backend=args.backend,
                                      interval=args.interval, reconcile=args.reconcile,
                                      duration=args.duration if args.duration is not None else 3600,
                                      tag_files=args.tag_files, control=args.control, seconds=args.wait_ready,
                                      config=args.config, no_project_rules=args.no_project_rules,
                                      limits=TrackingLimits(args.max_files, args.max_state_mib),
                                      allow_memory_state=args.allow_memory_state)
        elif args.action in ("watch-status", "watch-stop"):
            seconds = args.wait_ready if args.action == "watch-status" else args.wait
            result, code = control_command(args.home, args.control, stop=args.action == "watch-stop", seconds=seconds)
            if (args.action == 'watch-stop' and args.strict_tracking and seconds is not None
                    and code == 0 and result.get('tracking_status') != 'complete'):
                code = 1
            if (args.action == 'watch-stop' and not args.strict_tracking and seconds is not None
                    and not result.get('timed_out') and result.get('status') in ('finished', 'failed')
                    and result.get('tracking_status') in ('complete', 'partial')):
                code = 0
        else:
            store = Store(args.home)
            result, code = execute(args, store)
        # Child stdout stays untouched, including when --json is used.
        tracking = tracking_summary(store, result['session']) if not args.json and args.action in ('run','watch') else None
        emit(result, args.json, sys.stderr if args.action == "run" else sys.stdout,
             args.action, getattr(args, 'offset', 0), args.rows, args.details, tracking)
        return code
    except (OSError, ValueError, sqlite3.Error) as exc:
        failure = {"error": str(exc)}
        failure_code = 2
        sid = getattr(exc, 'agentgc_session', None)
        if sid and store:
            try:
                failure.update(session_result(store, sid))
                child_code = failure.get('command_exit_code')
                if args.action == 'run' and child_code is not None:
                    failure_code = 128 - child_code if child_code < 0 else child_code
                    if args.strict_tracking and failure_code == 0:
                        failure_code = 1
            except (OSError, ValueError, sqlite3.Error):
                failure.update({'session': sid, 'command_exit_code': None, 'command_status': 'unknown',
                                'tracking_status': 'unknown', 'gap_count': None, 'unresolved_event_count': None,
                                'roots': [], 'schema_version': 1})
        elif args.action in ('run', 'watch', 'watch-start'):
            failure.update({'session': None, 'command_exit_code': None, 'command_status': 'unknown',
                            'tracking_status': 'unknown', 'gap_count': None, 'unresolved_event_count': None,
                            'roots': [], 'schema_version': 1})
        if args.action in ('run', 'watch', 'watch-start'):
            failure['cli_exit_code'] = failure_code
        if args.json:
            emit(failure, True, sys.stderr)
        else:
            emit(failure, False, sys.stderr, args.action)
        return failure_code
    except KeyboardInterrupt:
        return 130
    finally:
        if store:
            store.close()
