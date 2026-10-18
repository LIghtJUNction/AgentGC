"""Summarize retained evidence; event counts are never lost-file counts."""
from __future__ import annotations

import json
from pathlib import PurePath
import re

from .paths import SafetyError
from .session_state import session_result


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


_QUALITY = {
    'expected-boundary': ('info', 'Link targets were intentionally excluded; this does not prove coverage elsewhere.'),
    'metadata-unavailable': ('warning', 'Observed path metadata was unavailable; the cause and writer are unknown.'),
    'coverage-loss': ('error', 'Scope coverage or intermediate history may be missing; final scans cannot restore every event.'),
    'unknown': ('error', 'The retained evidence does not establish the failure or its coverage impact.'),
    'removal-notice': ('info', 'A deletion notice can reflect normal removal; it is not a lost-file count.'),
}


def _gap_classes(detail: str) -> list[str]:
    """Return text-derived hints without extracting paths or asserting causes."""
    value = detail.lower()
    result = []
    listener_failure = any(marker in value for marker in (
        'native event read failed', 'event listener interrupted', 'listener read failed',
        'native watch refresh failed', 'tracking interrupted', 'watch limit',
        'no readable native watch directories', 'partial native coverage',
    ))
    if (listener_failure or 'overflow' in value or 'drain limit' in value
            or 'permission' in value or 'operation not permitted' in value
            or re.search(r'\berrno (?:1|13)\b', value)):
        result.append('coverage-loss')
    if 'link or reparse' in value:
        result.append('expected-boundary')
    # ENOENT from a failed spawn/listener is not evidence of a short-lived file.
    if not listener_failure and ('no such file' in value or re.search(r'\berrno 2\b', value)):
        result.append('metadata-unavailable')
    return result or ['unknown']


def _event_prefix(path: str, roots: list[PurePath]) -> tuple[str | None, str | None]:
    """Use stored lexical paths only; never resolve aliases or inspect links."""
    if not path:
        return None, None
    candidate = PurePath(path)
    if not candidate.is_absolute() or '..' in candidate.parts:
        return None, None
    for root in roots:
        if candidate.is_relative_to(root):
            relative = candidate.relative_to(root)
            # One directory level keeps deep temporary names out of the summary.
            prefix = relative.parts[0] if len(relative.parts) > 1 else '.'
            return str(root), prefix
    return None, None


def _compact_groups(store, session: str, stored_roots: str) -> list[dict]:
    try:
        values = json.loads(stored_roots)
    except (TypeError, ValueError):
        values = None
    roots = []
    if isinstance(values, list) and all(isinstance(value, str) for value in values):
        roots = [PurePath(value) for value in values
                 if PurePath(value).is_absolute() and '..' not in PurePath(value).parts]
        roots.sort(key=lambda root: (-len(root.parts), str(root)))
    grouped = {}
    # ORDER BY path makes duplicate paths adjacent, allowing distinct-path counts
    # without retaining a second in-memory set of all observed paths.
    for item in store.db.execute(
        "SELECT kind,path,detail,COUNT(*) count,MIN(at) first_seen,MAX(at) last_seen "
        "FROM events WHERE session_id=? AND (kind IN "
        "('gap','link-boundary-notice','coverage-error-notice') OR kind LIKE '%-unresolved') "
        "GROUP BY path,kind,detail ORDER BY path,kind,detail", (session,)
    ):
        gap = item['kind'] == 'gap'
        if gap:
            classes = _gap_classes(item['detail'])
            categories = classify(item['detail'])[0].split(', ')
        else:
            classes = [{
                'created-unresolved': 'metadata-unavailable',
                'modified-unresolved': 'metadata-unavailable',
                'deleted-unresolved': 'removal-notice',
                'link-boundary-notice': 'expected-boundary',
                'coverage-error-notice': 'coverage-loss',
            }.get(item['kind'], 'unknown')]
            categories = []
        source = 'text-derived' if gap else 'event-kind'
        path_source = 'event-path' if item['path'] else 'unattributed'
        root, prefix = _event_prefix(item['path'], roots)
        for classification in classes:
            key = classification, source, path_source, root, prefix
            if key not in grouped:
                severity, impact = _QUALITY[classification]
                grouped[key] = {
                    'classification': classification, 'severity': severity, 'impact': impact,
                    'evidence_source': source, 'path_source': path_source,
                    'root': root, 'prefix': prefix,
                    'gap_events': 0, 'unresolved_events': 0, 'notice_events': 0,
                    'paths': 0 if item['path'] else None,
                    'kinds': set(), 'categories': set(),
                    'first_seen': item['first_seen'], 'last_seen': item['last_seen'],
                    '_last_path': None,
                }
            group = grouped[key]
            count_field = ('gap_events' if gap else 'unresolved_events'
                           if item['kind'].endswith('-unresolved') else 'notice_events')
            group[count_field] += item['count']
            if item['path'] and item['path'] != group['_last_path']:
                group['paths'] += 1
                group['_last_path'] = item['path']
            group['kinds'].add(item['kind'])
            group['categories'].update(categories)
            group['first_seen'] = min(group['first_seen'], item['first_seen'])
            group['last_seen'] = max(group['last_seen'], item['last_seen'])
    result = []
    for group in grouped.values():
        group.pop('_last_path')
        group['kinds'] = sorted(group['kinds'])
        group['categories'] = sorted(group['categories'])
        result.append(group)
    return sorted(result, key=lambda group: (
        {'error': 0, 'warning': 1, 'info': 2}[group['severity']],
        group['classification'], group['root'] or '', group['prefix'] or '',
        group['evidence_source'], group['path_source'],
    ))


def summarize(store, session: str) -> dict:
    # One read snapshot: concurrent tracking/pruning must not mix counters from
    # different moments. A savepoint also preserves a caller's outer transaction.
    store.db.execute('SAVEPOINT diagnostic_summary')
    try:
        return _summarize(store, session)
    finally:
        store.db.execute('RELEASE diagnostic_summary')


def _summarize(store, session: str) -> dict:
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
    category_counts = {}
    retained_gaps = sum(g['count'] for g in groups)
    for group in groups:
        for category in group['category'].split(', '):
            category_counts[category] = category_counts.get(category, 0) + group['count']
    unresolved = [dict(r) for r in store.db.execute(
        "SELECT kind,COUNT(*) events,COUNT(DISTINCT path) paths FROM events "
        "WHERE session_id=? AND kind LIKE '%-unresolved' GROUP BY kind ORDER BY kind", (session,)
    )]
    paths = store.db.execute(
        "SELECT COUNT(DISTINCT path) FROM events WHERE session_id=? AND kind LIKE '%-unresolved'", (session,)
    ).fetchone()[0]
    roots = store.db.execute('SELECT roots FROM sessions WHERE id=?', (session,)).fetchone()[0]
    return {**dict(row), **session_result(store, session), 'groups': groups, 'unresolved': unresolved,
            'compact_groups': _compact_groups(store, session, roots),
            'compact_note': 'Classification and severity are evidence hints, not verified causes. Text-derived gap classifications may overlap, and typed notices may share a cause with gaps; do not sum grouped counts as distinct gaps or lost files. Prefixes use only stored event paths and selected session roots, grouped by the first relative parent directory; no paths are extracted from free text. Null roots/prefixes are unattributed or outside valid stored roots, not session-wide path attribution. Original gap details remain in groups; unresolved and typed notices remain available in session history. Missing or inconsistent history remains unknown and is not represented as a benign group.',
            'categories': [{'category': key, 'events': count}
                           for key, count in sorted(category_counts.items())],
            'retained_gap_events': retained_gaps,
            'missing_gap_details': row['gaps'] - retained_gaps if row['gaps'] >= retained_gaps else None,
            'gap_count_consistent': row['gaps'] >= retained_gaps,
            'retained_unresolved_events': sum(r['events'] for r in unresolved),
            'unresolved_paths': paths,
            'note': 'Gap categories are hints derived from retained text, not verified causes. Retained events, not lost files. Repeated notices may share paths. Deleted notices can reflect normal removal; creation/modification notices lack metadata when paths disappear. Causes and writer identity are unknown. Category counts overlap for compound records; do not sum them as distinct gaps. Missing gap details compare the cumulative counter with retained records; their causes are unknown, not proof of pruning. A retained count above the counter is inconsistent, not negative missing history. Pruned history is not included; zero counts do not prove complete tracking.'}
