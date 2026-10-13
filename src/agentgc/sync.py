"""Local rule reconciliation, independent of manual file labels and cleanup."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from .paths import SafetyError, Scope, absolute, no_links
from .pressure import inventory
from .rules import matches, quantity


def table_exists(store, name):
    return store.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is not None


def marks(store, project=None):
    if not table_exists(store, 'auto_marks'):
        return []
    sql = 'SELECT record FROM auto_marks'
    args = ()
    if project:
        sql += ' WHERE project=?'
        args = (str(project),)
    return [json.loads(r['record']) for r in store.db.execute(sql+' ORDER BY path,rule_id', args)]


def conditions(rule, item, previous, ratio=1):
    clauses = rule.get('when', {}).get('any')
    if clauses is None:
        return True
    outcomes = []
    for clause in clauses:
        values = []
        for key, limit in clause.items():
            threshold = quantity(limit) if key in ('size_gte', 'growth_gte') else limit
            if key == 'size_gte':
                value = item['bytes']
            elif key == 'files_gte':
                value = item['files']
            elif not item['complete'] or previous is None:
                values.append(None)
                continue
            elif key == 'growth_gte':
                value = item['bytes']-previous['bytes']
            elif previous['bytes'] == 0:
                values.append(None)
                continue
            else:
                value = 100*(item['bytes']-previous['bytes'])/previous['bytes']
            values.append(True if value >= threshold*ratio else (False if item['complete'] else None))
        outcomes.append(False if False in values else (None if None in values else True))
    return True if True in outcomes else (None if None in outcomes else False)


def reconcile(store, config, roots, dry_run=False, max_entries=None, max_seconds=None,
              include=None, excludes=None, overrides=None):
    project = Path(config['project'])
    roots = [absolute(r) for r in roots]
    if any(not r.is_relative_to(project) for r in roots):
        raise SafetyError('Rule sync roots must stay inside the selected project; use pressure for external stores')
    if include is None:
        include = Scope(roots, store.home, excludes).includes
    effective = config if overrides is None else overrides
    rules = effective['rules']
    result = inventory(roots, max_entries=config['limits']['max_entries'] if max_entries is None else max_entries,
                       max_seconds=config['limits']['max_seconds'] if max_seconds is None else max_seconds, state_home=store.home,
                       select=lambda p,k: any(matches(r,p,k,project) for r in rules), include=include)
    identity = {'project': str(project), 'roots': sorted(result['roots']), 'excludes': sorted(excludes or []),
                'measurement': 'logical-bytes-v1'}
    scope = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    baseline = {}
    if table_exists(store, 'sync_baselines'):
        row = store.db.execute('SELECT record FROM sync_baselines WHERE scope=?', (scope,)).fetchone()
        if row:
            baseline = json.loads(row['record'])
    old = {(r['path'],r['rule_id']): r for r in marks(store, project)}
    now = time.time()
    updates = {}
    notifications = []
    for item in result['items']:
        path = Path(item['path'])
        for rule in rules:
            if not matches(rule, path, item.get('kind','directory'), project):
                continue
            key = (str(path),rule['id'])
            before = old.get(key, {})
            prior = baseline.get(str(path))
            # Never compare incompatible file/directory observations.
            if prior and prior.get('kind','directory') != item.get('kind','directory'):
                prior = None
            outcome = conditions(rule, item, prior)
            state = 'active' if outcome is True else ('inactive' if outcome is False else 'unknown')
            if before.get('state') == 'active':
                if outcome is None or (outcome is False and conditions(rule,item,prior,rule.get('notify',{}).get('clear_below',0.8)) is not False):
                    state = 'active'
            record = {'project': str(project), 'path': str(path), 'rule_id': rule['id'], 'state': state,
                      'evaluation': 'unknown' if outcome is None else ('matched' if outcome else 'not-matched'),
                      'action': rule['then'], 'measurement': item, 'config_fingerprint': effective['fingerprint'],
                      'scope': scope, 'first_seen': before.get('first_seen',now), 'observed': now,
                      'last_notified': before.get('last_notified',0), 'source': 'rule-snapshot'}
            worsened = (item['bytes']-before.get('measurement',{}).get('bytes',item['bytes']) >= 256*1024**2
                        and item['bytes'] >= before.get('measurement',{}).get('bytes',item['bytes'])*1.25)
            cooldown = quantity(rule.get('notify',{}).get('cooldown','24h'), True)
            if state == 'active' and (before.get('state') != 'active' or worsened) and now-record['last_notified'] >= cooldown:
                notifications.append({'path':str(path),'rule_id':rule['id'],'action':rule['then']})
                record['last_notified'] = now
            updates[key] = record
    enabled = {r['id']:r for r in rules if r.get('enabled',True)}
    for key,before in old.items():
        if key in updates or before.get('scope') != scope:
            continue
        if key[1] not in enabled:
            state = 'disabled'
        elif result['complete']:
            # lstat-like lexists: do not classify a replacement link as a missing path.
            path = Path(key[0])
            try:
                no_links(path)
                path.lstat()
                state = 'unmatched'
            except FileNotFoundError:
                state = 'missing'
            except (OSError, SafetyError):
                continue
        else:
            continue
        updates[key] = {**before,'state':state,'observed':now,'config_fingerprint':effective['fingerprint']}
    changed = [r for key,r in updates.items() if old.get(key,{}).get('state') != r['state']
               or old.get(key,{}).get('config_fingerprint') != r['config_fingerprint']]
    if not dry_run:
        with store.transaction():
            store.db.execute('CREATE TABLE IF NOT EXISTS auto_marks (project TEXT,path TEXT,rule_id TEXT,record TEXT NOT NULL,PRIMARY KEY(project,path,rule_id))')
            store.db.execute('CREATE TABLE IF NOT EXISTS sync_baselines (scope TEXT PRIMARY KEY,record TEXT NOT NULL)')
            store.db.execute('CREATE TABLE IF NOT EXISTS auto_events (id INTEGER PRIMARY KEY,at REAL,project TEXT,path TEXT,rule_id TEXT,state TEXT,config_fingerprint TEXT)')
            for key,r in updates.items():
                store.db.execute('INSERT INTO auto_marks VALUES(?,?,?,?) ON CONFLICT(project,path,rule_id) DO UPDATE SET record=excluded.record',
                                 (str(project),*key,json.dumps(r)))
            for r in changed:
                store.db.execute('INSERT INTO auto_events(at,project,path,rule_id,state,config_fingerprint) VALUES(?,?,?,?,?,?)',
                                 (now,str(project),r['path'],r['rule_id'],r['state'],effective['fingerprint']))
            if result['complete']:
                snapshot = {i['path']:i for i in result['items']}
                store.db.execute('INSERT INTO sync_baselines VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET record=excluded.record',
                                 (scope,json.dumps(snapshot)))
    return {'mode':'preview' if dry_run else 'sync','project':str(project),'config':config['config'],
            'config_fingerprint':effective['fingerprint'], 'complete':result['complete'],'gaps':result['gaps'],
            'gap_count':result['gap_count'],'entries':result['entries'],'disks':result['disks'],
            'changes':changed,'notifications':notifications,'observations':list(updates.values()),
            'note':'Local rule observations only; manual labels, file history and cleanup permissions are unchanged'}
