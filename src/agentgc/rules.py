"""Declarative project rules. No executable hooks or cleanup authority."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tomllib

from .paths import SafetyError, absolute, no_links, regular, signature
from .pressure import DEFAULTS, NAMES, PATH_CATEGORIES

CONDITIONS = {"size_gte", "files_gte", "growth_gte", "growth_percent_gte"}
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
         "KB": 1000, "MB": 1000**2, "GB": 1000**3}


def quantity(value, duration=False):
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400} if duration else UNITS
    if not isinstance(value, str):
        raise SafetyError("Quantity needs a number and explicit unit")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)", value)
    if not match or match[2] not in units:
        raise SafetyError("Invalid quantity or unit")
    try:
        result = Decimal(match[1]) * units[match[2]]
        if not 0 < result <= 2**63-1 or result != int(result):
            raise SafetyError("Quantity must be positive whole bytes/seconds in supported range")
        return int(result)
    except InvalidOperation as exc:
        raise SafetyError("Invalid quantity") from exc


def keys(value, allowed, where):
    if not isinstance(value, dict) or set(value)-set(allowed):
        raise SafetyError(f"Unknown fields or invalid table in {where}")


def number(value, where, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
            or value > 2**63-1 or not math.isfinite(value)):
        raise SafetyError(f"{where} must be finite and positive")
    if integer and not isinstance(value, int):
        raise SafetyError(f"{where} must be an integer")


def patterns(values):
    if not isinstance(values, list) or not values or len(values) > 100:
        raise SafetyError("path must be a nonempty array of at most 100 relative patterns")
    for value in values:
        if (not isinstance(value, str) or not value or len(value) > 512 or value.startswith('/')
                or '\\' in value or ':' in value or '..' in value.split('/')
                or (value != '.' and any(p in ('', '.') for p in value.split('/')))):
            raise SafetyError("Patterns must be relative POSIX paths without ..")


def builtin():
    return [{"id": f"{category}-pressure", "enabled": True,
             "match": {"category": category, "kind": "directory"},
             "when": {"any": [{"size_gte": f"{size} B"}, {"files_gte": 100000}]},
             "then": {"attention": f"{category}-pressure", "message": "Directory pressure requires review, not deletion"},
             "notify": {"cooldown": "24h", "clear_below": 0.8}}
            for category, size in DEFAULTS.items()]


def merge(base, update):
    result = deepcopy(base)
    for key, value in update.items():
        result[key] = merge(result.get(key, {}), value) if isinstance(value, dict) else deepcopy(value)
    return result


def validate(rule):
    keys(rule, {"id", "enabled", "match", "when", "then", "notify"}, "rule")
    if not isinstance(rule.get('id'), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rule['id']):
        raise SafetyError("Rule id must be 1-64 ASCII letters, digits, underscores or hyphens")
    if type(rule.get('enabled', True)) is not bool:
        raise SafetyError("enabled must be boolean")
    if rule.get('enabled') is False and set(rule) <= {'id', 'enabled'}:
        return
    match = rule.get('match', {})
    keys(match, {'path', 'under', 'kind', 'category'}, 'match')
    if not ('path' in match or 'category' in match):
        raise SafetyError("Rule match needs path or category")
    if 'path' in match:
        patterns(match['path'])
    if 'under' in match:
        patterns([match['under']])
        if any(c in match['under'] for c in '*?['):
            raise SafetyError("under is a literal relative directory")
    if match.get('kind', 'any') not in ('any', 'file', 'directory'):
        raise SafetyError("Invalid match kind")
    if 'category' in match and (not isinstance(match['category'], str) or match['category'] not in DEFAULTS):
        raise SafetyError("Unknown category")
    if 'when' in rule:
        keys(rule['when'], {'any'}, 'when')
        clauses = rule['when'].get('any')
        if not isinstance(clauses, list) or not 1 <= len(clauses) <= 32:
            raise SafetyError("when.any needs 1-32 condition tables")
        for clause in clauses:
            keys(clause, CONDITIONS, 'condition')
            if not clause:
                raise SafetyError("Empty condition")
            for key, value in clause.items():
                if key in ('size_gte', 'growth_gte'):
                    quantity(value)
                else:
                    number(value, key, key == 'files_gte')
    action = rule.get('then', {})
    keys(action, {'attention', 'suggest', 'message'}, 'then')
    if not ('attention' in action or 'suggest' in action):
        raise SafetyError("Rule needs an attention or suggestion action")
    if action.get('suggest', 'review') not in ('keep', 'temporary', 'review'):
        raise SafetyError("Automatic rules cannot authorize disposal")
    for key in ('attention', 'message'):
        if key in action and (not isinstance(action[key], str) or not action[key] or len(action[key]) > 1024):
            raise SafetyError("Action text must be a nonempty bounded string")
    notify = rule.get('notify', {})
    keys(notify, {'cooldown', 'clear_below'}, 'notify')
    if 'cooldown' in notify:
        quantity(notify['cooldown'], True)
    ratio = notify.get('clear_below', 0.8)
    number(ratio, 'clear_below')
    if ratio > 1:
        raise SafetyError("clear_below must be <= 1")


def load(cwd=None, config=None, disabled=False):
    root = absolute(cwd or Path.cwd())
    no_links(root)
    path = None
    if not disabled:
        if config is not None:
            path = absolute(config)
            root = path.parent
        else:
            for parent in (root, *root.parents):
                candidate = parent/'.agcc'
                if candidate.exists() or candidate.is_symlink():
                    path, root = candidate, parent
                    break
                if (parent/'.git').exists():
                    root = parent
                    break
    data = {}
    if path:
        no_links(path)
        before = regular(path)
        if before.st_size > 256*1024:
            raise SafetyError(".agcc exceeds 256 KiB")
        fd = os.open(path, os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_BINARY',0))
        with os.fdopen(fd,'rb') as stream:
            if signature(os.fstat(stream.fileno())) != signature(before):
                raise SafetyError('Configuration changed while opening')
            content = stream.read(256*1024+1)
            if len(content) > 256*1024 or signature(os.fstat(stream.fileno())) != signature(before):
                raise SafetyError('Configuration changed while reading')
        if signature(regular(path)) != signature(before):
            raise SafetyError('Configuration changed while reading')
        data = tomllib.loads(content.decode('utf-8'))
        keys(data, {'version', 'extends', 'auto', 'limits', 'rules'}, '.agcc')
        if type(data.get('version')) is not int or data['version'] != 1:
            raise SafetyError("Unsupported .agcc version; expected 1")
    extensions = data.get('extends', ['builtin:standard'])
    if extensions not in ([], ['builtin:standard']):
        raise SafetyError("Only builtin:standard inheritance is supported")
    auto = data.get('auto', {})
    keys(auto, {'enabled', 'after'}, 'auto')
    if type(auto.get('enabled', True)) is not bool:
        raise SafetyError("auto.enabled must be boolean")
    after = auto.get('after', ['run', 'watch', 'scan'])
    if not isinstance(after, list) or any(x not in ('run', 'watch', 'scan') for x in after):
        raise SafetyError("auto.after supports run, watch and scan only")
    limits = data.get('limits', {})
    keys(limits, {'max_entries', 'max_seconds'}, 'limits')
    number(limits.get('max_entries', 200000), 'max_entries', True)
    number(limits.get('max_seconds', 30), 'max_seconds')
    if limits.get('max_entries', 200000) > 10000000 or limits.get('max_seconds', 30) > 600:
        raise SafetyError("Project scan budgets exceed 10000000 entries or 600 seconds")
    effective = {r['id']: r for r in builtin()} if extensions else {}
    custom = data.get('rules', [])
    if not isinstance(custom, list) or len(custom) > 256:
        raise SafetyError("rules must be an array of at most 256 tables")
    seen = set()
    for rule in custom:
        keys(rule, {'id', 'enabled', 'match', 'when', 'then', 'notify'}, 'rule')
        rid = rule.get('id')
        if not isinstance(rid, str) or rid in seen:
            raise SafetyError("Missing or duplicate rule id")
        seen.add(rid)
        effective[rid] = merge(effective.get(rid, {}), rule)
    for rule in effective.values():
        validate(rule)
    result = {'project': str(root), 'config': str(path) if path else None,
              'auto': {'enabled': auto.get('enabled', True), 'after': after},
              'limits': {'max_entries': limits.get('max_entries', 200000), 'max_seconds': limits.get('max_seconds', 30)},
              'rules': list(effective.values()),
              'sources': {rid: {'builtin': rid in {r['id'] for r in builtin()} and bool(extensions),
                                'project': str(path) if rid in seen else None} for rid in effective}}
    result['fingerprint'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def glob_match(path, pattern):
    """Full relative path match: * one segment, ** zero or more segments."""
    parts, tokens = path.split('/'), pattern.split('/')
    from functools import lru_cache
    import fnmatch

    @lru_cache(None)
    def match(i, j):
        if j == len(tokens):
            return i == len(parts)
        if tokens[j] == '**':
            return match(i, j+1) or (i < len(parts) and match(i+1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], tokens[j]) and match(i+1, j+1)
    return match(0, 0)


def matches(rule, path, kind, project):
    if not rule.get('enabled', True) or not path.is_relative_to(project):
        return False
    match = rule['match']
    if match.get('kind', 'any') not in ('any', kind):
        return False
    relative = path.relative_to(project).as_posix()
    under = match.get('under', '.')
    if under != '.' and not (relative == under or relative.startswith(under.rstrip('/')+'/')):
        return False
    if 'category' in match and PATH_CATEGORIES.get(path, NAMES.get(path.name)) != match['category']:
        return False
    return 'path' not in match or any(glob_match(relative, pattern) for pattern in match['path'])
