"""Independent command and tracking outcomes for compact local clients."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .paths import SafetyError


def session_result(store, session: str | Mapping) -> dict:
    """Read one coherent snapshot, including legacy stores without outcomes.

    Gap counts are cumulative session counters. Unresolved counts include only
    retained event rows, not files or all events that ever occurred. Explicit
    partial outcomes remain partial even after retained history is pruned.
    """
    values = {} if isinstance(session, str) else dict(session)
    sid = session if isinstance(session, str) else values.get("id", values.get("session"))
    store.db.execute("SAVEPOINT session_result_snapshot")
    try:
        row = store.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise SafetyError("No session with that ID")
        row = dict(row)
        roots = json.loads(row["roots"])
        if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
            raise SafetyError("Invalid session roots")
        tables = {item[0] for item in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('events','session_results')"
        )}
        saved = None
        if "session_results" in tables:
            saved = store.db.execute("SELECT * FROM session_results WHERE session_id=?", (sid,)).fetchone()
        unresolved = None
        if "events" in tables:
            unresolved = store.db.execute(
                "SELECT COUNT(*) FROM events WHERE session_id=? AND kind LIKE '%-unresolved'", (sid,)
            ).fetchone()[0]
        gaps = row.get("gaps")
        command_status = saved["command_status"] if saved else (
            "unknown" if row.get("command") else "not-applicable")
        command_exit_code = saved["command_exit_code"] if saved else None
        if command_status == "running" and row["status"] != "running":
            # Manual closure establishes no observation of the child's exit.
            command_status = "unknown"
        tracking_status = saved["tracking_status"] if saved else "unknown"
        if tracking_status == "complete" and ((gaps or 0) > 0 or (unresolved or 0) > 0):
            tracking_status = "partial"
        if tracking_status == "complete" and (gaps is None or unresolved is None):
            tracking_status = "unknown"
        return {
            "schema_version": 1,
            "session": row["id"],
            "agent": row.get("agent"),
            "backend": row.get("backend"),
            "started": row.get("started"),
            "ended": row.get("ended"),
            "roots": roots,
            "session_status": row["status"],
            "command_status": command_status,
            "command_exit_code": command_exit_code,
            "tracking_status": tracking_status,
            "gap_count": gaps,
            "unresolved_event_count": unresolved,
            "count_scope": {"gap_count": "session-cumulative", "unresolved_event_count": "retained-events"},
        }
    finally:
        store.db.execute("RELEASE session_result_snapshot")
