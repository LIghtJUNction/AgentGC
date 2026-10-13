"""Compact Markdown presentation only; never changes decisions or JSON payloads."""
from __future__ import annotations

from collections import Counter
import html
import re
import unicodedata

DISPLAY_LIMIT = 20


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
        elif name in ('size', 'bytes', 'state_bytes', 'free_bytes', 'bytes_to_quarantine', 'bytes_freed'):
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
    'sessions': ('id','status','agent','backend','roots','gaps','exit_code'),
    'quarantine': ('id','status','original','destination'),
    'recover': ('id','status','original','destination','reason','action'),
    'reviews': ('path','category','bytes','files','attention','complete','observed','note'),
    'auto-marks': ('path','rule_id','state','evaluation','action','complete','observed'),
}


def markdown(data, command: str = 'agcc', offset: int = 0) -> str:
    lines = [f"## {command}"]

    def summary(row, names):
        text = fields(row, names)
        if text:
            lines.append('- ' + text)

    def rows(items, names, limit=DISPLAY_LIMIT, pagination=False):
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
            lines.append(f'- Showing {len(shown)}/{len(items)} returned rows; {omitted} omitted. Use `--json` for all returned fields/rows.')
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
        rows(row.get('disks', []), ('root','used_percent','free_bytes','attention','urgent'))
        states = ', '.join(f'{k}={v}' for k,v in sorted(Counter(r.get('state','unknown') for r in observations).items()))
        lines.append(f'- Observations={len(observations)}; changes={len(changes)}; unknown={len(unknown)}; notifications={len(row.get("notifications", []))}. {states}')
        warnings(row)
        focus = list(changes)
        existing = {(r.get('path'),r.get('rule_id')) for r in focus}
        focus.extend(r for r in unknown if (r.get('path'),r.get('rule_id')) not in existing)
        rows(focus, ('path','rule_id','state','evaluation','action'))
        if row.get('notifications'):
            lines.append('### Notifications')
            rows(row['notifications'], ('path','rule_id','action','message','state','evaluation'))
        lines.append('- Automatic suggestions are separate from manual labels; not cleanup permission.')

    if isinstance(data, list):
        if command == 'auto-marks':
            lines.append(f'- Returned={len(data)}; unknown={sum(r.get("evaluation") == "unknown" for r in data)}.')
        names = ROW_FIELDS.get(command)
        if names is None:
            names = tuple(dict.fromkeys(k for item in data if isinstance(item,dict) for k in item))
        rows(data, names, limit=None if command in ('quarantine','recover') else DISPLAY_LIMIT,
             pagination=command in ('list','search'))
        if command in ('list','search','history'):
            lines.append('- Sources describe observations, not proven writers. Sizes are logical bytes.')
        if command in ('reviews','auto-marks'):
            lines.append('- Historical observations; remeasure before acting. Not cleanup permission.')
    elif isinstance(data, dict):
        if data.get('error'):
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
            rows(data.get('disks', []), ('root','used_percent','free_bytes','attention','urgent'))
            items = data.get('items', [])
            lines.append(f'- Candidates={len(items)}; attention={sum(bool(r.get("attention")) for r in items)}.')
            rows(items, ('path','category','bytes','files','attention','reasons','complete'))
            lines.append('- Logical bytes; nested candidates/hard links may overlap. Review is not cleanup permission.')
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
                'doctor': ('integrity','home','state_bytes','native_backend','files','bytes','events','active_sessions','labels'),
                'run': ('session','exit_code'), 'watch': ('session','exit_code'),
                'restore': ('id','path','status','label'),
            }
            names = selectors.get(command, tuple(k for k in data if k not in ('automatic','errors','error','gaps','pending_recovery')))
            summary(data, names)
            warnings(data)
            if command in ('stats', 'doctor', 'scan'):
                lines.append('- Sizes are logical bytes, not unique physical disk usage.')
            if command in ('run', 'watch'):
                lines.append('- Tracking gaps: unavailable in this result; query `history --session`/`sessions`.')
            if command == 'watch-stop':
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
