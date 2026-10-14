"""Summarize retained evidence; event counts are never lost-file counts."""
from __future__ import annotations

from .paths import SafetyError


def classify(detail: str) -> tuple[str, str]:
    # Legacy gaps contain free text. Keep ambiguous failures unknown, not benign.
    value = detail.lower()
    matches = []
    if 'overflow' in value or 'drain limit' in value:
        matches.append(('event-loss-risk', 'Intermediate history may be missing; final scans only recover surviving state.'))
    if 'permission' in value or 'errno 13' in value:
        matches.append(('permission', 'Unreadable scope has unknown metadata; incomplete scans cannot confirm deletion.'))
    if 'link or reparse' in value:
        matches.append(('link-boundary', 'Link targets are intentionally not read; other errors in this record may still affect coverage.'))
    if 'no such file' in value:
        matches.append(('path-disappeared', 'Path vanished during observation; metadata may be unavailable, cause unknown (not proof of uninstall).'))
    if not matches:
        return 'tracking-failure', 'Coverage or intermediate history may be incomplete; inspect retained detail.'
    return ', '.join(m[0] for m in matches), ' '.join(m[1] for m in matches)


def summarize(store, session: str) -> dict:
    row = store.db.execute('SELECT id,status,gaps,backend FROM sessions WHERE id=?', (session,)).fetchone()
    if row is None:
        raise SafetyError('No session with that ID')
    groups = []
    for item in store.db.execute(
        "SELECT detail,COUNT(*) count,MIN(at) first_seen,MAX(at) last_seen FROM events "
        "WHERE session_id=? AND kind='gap' GROUP BY detail ORDER BY MIN(id)", (session,)
    ):
        category, impact = classify(item['detail'])
        groups.append({**dict(item), 'category': category, 'impact': impact})
    unresolved = [dict(r) for r in store.db.execute(
        "SELECT kind,COUNT(*) events,COUNT(DISTINCT path) paths FROM events "
        "WHERE session_id=? AND kind LIKE '%-unresolved' GROUP BY kind ORDER BY kind", (session,)
    )]
    paths = store.db.execute(
        "SELECT COUNT(DISTINCT path) FROM events WHERE session_id=? AND kind LIKE '%-unresolved'", (session,)
    ).fetchone()[0]
    return {**dict(row), 'groups': groups, 'unresolved': unresolved,
            'retained_unresolved_events': sum(r['events'] for r in unresolved),
            'unresolved_paths': paths,
            'note': 'Gap categories are hints derived from retained text, not verified causes. Retained events, not lost files. Repeated notices may share paths. Deleted notices can reflect normal removal; creation/modification notices lack metadata when paths disappear. Causes and writer identity are unknown. Pruned history is not included; zero counts do not prove complete tracking.'}
