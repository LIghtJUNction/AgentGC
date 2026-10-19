"""Compact Markdown presentation only; never changes decisions or JSON payloads."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import html
import re
import unicodedata

DISPLAY_LIMIT = 20
TIME_FIELDS = frozenset(('at', 'observed', 'first_seen', 'last_seen', 'started', 'ended',
                         'heartbeat', 'changed', 'created', 'restored', 'expires_at',
                         'last_notified'))
DISK_FIELDS = ('root','disk_role','disk_role_source','capacity_status','capacity_reason',
               'total_bytes','used_bytes','used_percent','df_style_used_percent','free_bytes','attention_free_bytes','urgent_free_bytes',
               'byte_attention','byte_urgent','inode_status','total_inodes','free_inodes',
               'inode_used_percent','inode_attention','inode_urgent','inode_reason','attention','urgent')


def inline(value) -> str:
    """Keep untrusted data on one line and outside Markdown/terminal syntax."""
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = []
    for char in text:
        if char in "\n\r\t":
            escaped.append({"\n": r"\n", "\r": r"\r", "\t": r"\t"}[char])
        elif unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    text = html.escape("".join(escaped), quote=False)
    fence = "`" * (1 + max((len(s) for s in re.findall(r"`+", text)), default=0))
    return f"{fence} {text} {fence}"


def size(value) -> str:
    if value is None:
        return "unknown"
    if not isinstance(value, (int, float)):
        return inline(value)
    for divisor, unit in ((2**30, "GiB"), (2**20, "MiB"), (2**10, "KiB")):
        if abs(value) >= divisor:
            return f"{value/divisor:.2f} {unit}"
    return f"{value} B"


def timestamp(value) -> str:
    """Render epoch seconds in UTC; malformed metadata stays visibly unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 'unknown'
    try:
        moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=value)
        text = moment.isoformat(timespec='seconds')
    except (OverflowError, ValueError):
        return 'unknown'
    return inline(text.replace('+00:00', 'Z'))


def fields(row, names) -> str:
    parts = []
    for name in names:
        if name not in row:
            continue
        value = row[name]
        if value == '' and name in ('note','message'):
            continue
        if name == 'path':
            parts.append(inline(value))
        elif name in TIME_FIELDS:
            parts.append(f"{name}={timestamp(value)}")
        elif name in ('size', 'bytes', 'state_bytes', 'total_bytes', 'used_bytes', 'free_bytes',
                      'attention_free_bytes', 'urgent_free_bytes', 'bytes_to_quarantine', 'bytes_freed'):
            parts.append(f"{name}={size(value)}")
        elif isinstance(value, dict):
            parts.append(f"{name}: " + ", ".join(f"{inline(k)}={inline(v)}" for k,v in value.items()))
        elif isinstance(value, list):
            parts.append(f"{name}: " + ", ".join(inline(v) for v in value))
        else:
            parts.append(f"{name}={inline(value)}")
    return "; ".join(parts)


ROW_FIELDS = {
    'list': ('path','size','label','source','note'),
    'search': ('path','size','label','source','note'),
    'history': ('id','at','kind','path','source','session_id','size','detail'),
    'sessions': ('session','id','command_status','command_exit_code','tracking_status','gap_count',
                 'unresolved_event_count','session_status','status','agent','backend','started','ended','roots'),
    'quarantine': ('id','status','original','destination'),
    'recover': ('id','status','original','destination','reason','action'),
    'reviews': ('path','category','bytes','files','attention','complete','observed','note'),
    'auto-marks': ('path','rule_id','state','evaluation','action','complete','observed'),
}


def markdown(data, command: str = 'agcc', offset: int = 0, row_limit: int = DISPLAY_LIMIT,
             details: bool = False, tracking: dict | None = None) -> str:
    lines = [f"## {command}"]
    display_limit = None if details else row_limit

    def summary(row, names):
        text = fields(row, names)
        if text:
            lines.append('- ' + text)

    def outcome(row):
        if 'command_status' in row:
            lines.append(f'- command: {inline(row["command_status"])}; command_exit_code={inline(row.get("command_exit_code"))}')
        if 'tracking_status' in row:
            lines.append(f'- tracking: {inline(row["tracking_status"])}; gap_count={inline(row.get("gap_count"))}; unresolved_event_count={inline(row.get("unresolved_event_count"))}')

    def disk_rows(items):
        rows(items, DISK_FIELDS, limit=None)
        for item in items:
            basis = item.get('measurement_basis')
            if basis:
                summary(basis, ('source','backend','used_percent_formula','df_style_used_percent_formula',
                                'physical_allocation','shared_extents','filesystem_type'))
        if any(item.get('measurement_basis') for item in items):
            lines.append('- Capacity counters and df-style percentages use the displayed formulas. Physical allocation, compression and shared extents are not measured; these are not reclaimable-space estimates.')

    def rows(items, names, limit=display_limit, pagination=False):
        shown = items if limit is None else items[:limit]
        for item in shown:
            if isinstance(item, dict):
                summary(item, names)
                if item.get('present') == 0:
                    lines.append('  - **Missing** (not a current file).')
            else:
                lines.append('- ' + inline(item))
        omitted = len(items)-len(shown)
        if not items:
            lines.append('- None returned.')
        if omitted:
            lines.append(f'- Showing {len(shown)}/{len(items)} returned rows; {omitted} omitted. Use `--details` for all returned rows or `--json` for raw fields.')
        if pagination and items:
            lines.append(f'- Next page: same query/filters with `--offset {offset+len(shown)}`. A full page does not prove exhaustion.')

    def warnings(row):
        if row.get('complete') is False:
            lines.append('- **Incomplete**: measurements are lower bounds; unknown is not zero or resolved.')
        for key in ('error','errors','gaps','pending_recovery'):
            value = row.get(key)
            if not value:
                continue
            lines.append(f'### {key}')
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        summary(item, tuple(item))
                    else:
                        lines.append('- ' + inline(item))
            else:
                lines.append('- ' + inline(value))
        summary(row, ('gap_count','gaps_truncated','truncated'))

    def sync(row):
        observations = row.get('observations', [])
        changes = row.get('changes', [])
        unknown = [r for r in observations if r.get('evaluation') == 'unknown']
        summary(row, ('mode','project','config','complete','entries'))
        disk_rows(row.get('disks', []))
        states = ', '.join(f'{k}={v}' for k,v in sorted(Counter(r.get('state','unknown') for r in observations).items()))
        lines.append(f'- Observations={len(observations)}; changes={len(changes)}; unknown={len(unknown)}; notifications={len(row.get("notifications", []))}. {states}')
        warnings(row)
        focus = list(observations if details else changes)
        existing = {(r.get('path'),r.get('rule_id')) for r in focus}
        focus.extend(r for r in (changes if details else unknown) if (r.get('path'),r.get('rule_id')) not in existing)
        rows(focus, ('path','rule_id','state','evaluation','action'))
        if row.get('notifications'):
            lines.append('### Notifications')
            rows(row['notifications'], ('path','rule_id','action','message','state','evaluation'))
        lines.append('- Automatic suggestions are separate from manual labels; not cleanup permission.')

    def diagnostic(row, show_outcome=True):
        if show_outcome:
            outcome(row)
        summary(row, ('id','backend','retained_gap_events','missing_gap_details',
                      'gap_count_consistent','unresolved_paths'))
        if row.get('missing_gap_details'):
            lines.append('- **Missing history**: some counted gaps have no retained detail; their categories and impacts are unknown.')
        if row.get('gap_count_consistent') is False:
            lines.append('- **Inconsistent evidence**: retained gap records exceed the cumulative session counter.')
        if row.get('categories'):
            lines.append('### Gap categories (overlapping counts)')
            rows(row['categories'], ('category','events'), limit=None)
        if 'compact_groups' in row:
            lines.append('### Tracking evidence by class and path prefix')
            compact = row['compact_groups']
            names = ('classification','severity','root','prefix','gap_events','unresolved_events',
                     'notice_events','paths','categories')
            if details:
                rows(compact, names, limit=None)
            else:
                rows([g for g in compact if g['severity'] == 'error'], names, limit=None)
                rows([g for g in compact if g['severity'] != 'error'], names)
            lines.append('- ' + inline(row.get('compact_note', 'Counts may overlap; not lost files.')))
            sid = row.get('session', row.get('id'))
            lines.append('- Raw evidence: ' + inline(f'agcc --details diagnostics --session {sid}') +
                         ' or ' + inline(f'agcc history --session {sid}') + '. Counts are retained events, not lost files.')
        if details or 'compact_groups' not in row:
            rows(row.get('groups', []), ('category','count','first_seen','last_seen','impact','detail'), limit=None)
            summary(row, ('status','gaps','retained_unresolved_events','note'))
        rows(row.get('unresolved', []), ('kind','events','paths'), limit=None)

    if isinstance(data, list):
        if command == 'auto-marks':
            lines.append(f'- Returned={len(data)}; active={sum(r.get("state") == "active" for r in data)}; unknown={sum(r.get("evaluation") == "unknown" for r in data)}.')
            data = sorted(data, key=lambda r: (r.get('evaluation') != 'unknown', r.get('state') != 'active'))
        if command == 'sessions':
            lines.append(f'- Returned={len(data)}; running={sum(r.get("session_status", r.get("status")) == "running" for r in data)}; query order preserved. Use `--status running` to select running sessions.')
        names = ROW_FIELDS.get(command)
        if names is None:
            names = tuple(dict.fromkeys(k for item in data if isinstance(item,dict) for k in item))
        rows(data, names, limit=None if command in ('quarantine','recover') else display_limit,
             pagination=command in ('list','search','history','sessions'))
        if command in ('list','search','history'):
            lines.append('- Sources describe observations, not proven writers. Sizes are logical bytes.')
        if command in ('reviews','auto-marks'):
            lines.append('- Historical observations; remeasure before acting. Not cleanup permission.')
    elif isinstance(data, dict):
        if command == 'diagnostics':
            diagnostic(data)
        elif command == 'mark' and 'items' in data:
            summary(data, ('marked','failed'))
            rows(data['items'], ('path','label','note','tag_status'))
            warnings(data)
        elif data.get('error'):
            outcome(data)
            warnings(data)
        elif command == 'gc':
            items = data.get('items', [])
            eligible = sum(bool(i.get('eligible')) for i in items)
            summary(data, ('mode','bytes_to_quarantine','bytes_freed','truncated'))
            lines.append(f'- Eligible={eligible}; blocked={len(items)-eligible}. All returned candidates follow:')
            rows(items, ('path','size','eligible','reason'), limit=None)
            if 'moved' in data:
                lines.append('### Moves')
                rows(data['moved'], ('id','path','destination','status'), limit=None)
            lines.append('- Quarantine does not free space. Stop writers and review references before `--apply --confirm-unused`.')
        elif command in ('sync','auto-mark') or ('observations' in data):
            sync(data)
        elif command == 'pressure':
            summary(data, ('mode','roots','complete','entries','saved_reviews'))
            warnings(data)
            disk_rows(data.get('disks', []))
            items = data.get('items', [])
            lines.append(f'- Candidates={len(items)}; attention={sum(bool(r.get("attention")) for r in items)}.')
            rows(sorted(items, key=lambda r: not r.get('attention')), ('path','category','bytes','files','attention','reasons','complete'))
            lines.append('- Directory bytes sum st_size; nested candidates/hard links may overlap. Physical allocation and shared extents are not measured. Review is not cleanup permission.')
        elif command == 'rules':
            summary(data, ('project','config','fingerprint','path','note'))
            rules = data.get('rules', data.get('matching_rules', []))
            rows(rules, ('id','enabled','match','when','then','notify'))
            summary(data, ('auto','limits'))
        elif command == 'recover':
            summary(data, ('mode',))
            rows(data.get('items', []), ('id','status','original','destination','reason','action'), limit=None)
        else:
            selectors = {
                'mark': ('path','label','note','tag_status'),
                'scan': ('session','files','bytes','events','active_sessions','labels'),
                'stats': ('files','bytes','events','active_sessions','labels'),
                'doctor': ('integrity','home','state_bytes','state_budget_mib','state_over_budget','state_warning',
                           'native_backend','files','bytes','events','active_sessions','labels'),
                'run': ('session','exit_code'), 'watch': ('session','exit_code'),
                'session': ('session','roots','agent','backend','started','ended'),
                'watch-start': ('session','control','ready','roots','excludes','coverage_known','log'),
                'watch-status': ('session','control','state','ready','roots','excludes','coverage_known','heartbeat_age_seconds','expires_at'),
                'watch-stop': ('session','control','state','stop_requested','timed_out','roots'),
                'restore': ('id','path','status','label'),
            }
            names = selectors.get(command, tuple(k for k in data if k not in ('automatic','errors','error','gaps','pending_recovery')))
            summary(data, names)
            if command in ('run','watch','session','watch-start','watch-status','watch-stop'):
                outcome(data)
            warnings(data)
            if command in ('stats', 'doctor', 'scan'):
                lines.append('- Sizes are logical bytes, not unique physical disk usage.')
            if command in ('run', 'watch'):
                if tracking is None:
                    lines.append('- Tracking gaps: unavailable; query `history --session`/`sessions`.')
                else:
                    diagnostic(tracking, show_outcome='command_status' not in data)
                    lines.append('- Recorded counts only; zero does not prove complete history or writer identity.')
            if command == 'watch-stop':
                if data.get('status') in ('finished', 'failed', 'interrupted', 'closed-by-user'):
                    lines.append('- Watcher finalization ended; see tracking quality above. This is not proof that application writers stopped or cleanup permission.')
                else:
                    lines.append('- Stop requested only; wait for the session final scan to finish.')
            if 'automatic' in data:
                lines.append('### Automatic')
                automatic = data['automatic']
                if isinstance(automatic, dict) and 'observations' in automatic:
                    sync(automatic)
                elif isinstance(automatic, dict):
                    warnings(automatic)
                    summary(automatic, ('complete',))
    else:
        lines.append('- ' + inline(data))
    return '\n'.join(lines) + '\n'
