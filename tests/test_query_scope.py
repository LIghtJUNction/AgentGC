from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentgc.cli import main
from agentgc.paths import SafetyError
from agentgc.store import Store


class QueryScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / 'project'
        self.other = self.base / 'project-other'
        self.store = Store(self.base / 'state')
        self.clock = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, path, *, root=None, session=None, size=1, label='review',
               note='', present=1):
        self.clock += 1
        cursor = self.store.db.execute(
            'INSERT INTO files(path,root,size,signature,first_seen,changed,session_id,source,label,note,present) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (str(path), str(root or path.parent), size, 'test', self.clock, self.clock,
             session, 'test', label, note, present),
        )
        self.store.event(str(path), 'observed', 'test', session, cursor.lastrowid, size)
        return cursor.lastrowid

    def session(self, roots, status='running', agent='test'):
        self.clock += 1
        sid = self.store.start_session(agent, roots, 'poll')
        self.store.db.execute('UPDATE sessions SET started=? WHERE id=?', (self.clock, sid))
        if status != 'running':
            self.store.end_session(sid, 0, status)
        return sid

    def paths(self, rows):
        return {row['path'] for row in rows}

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--home', str(self.store.home), '--json', '--no-project-rules',
                         *map(str, arguments)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(err.getvalue(), '')
        return json.loads(out.getvalue())

    def test_files_match_paths_not_remembered_roots_or_similar_prefixes(self):
        target = self.project / 'output'
        self.record(target, root=self.other)
        self.record(self.other / 'output', root=self.project)
        self.record(self.project / 'inner' / 'receipt')
        expected = {str(target), str(self.project / 'inner' / 'receipt')}
        self.assertEqual(self.paths(self.store.list_files(roots=[self.project])), expected)
        self.assertEqual(len(self.store.list_files()), 3)
        self.assertEqual(self.paths(self.store.history(roots=[self.project])), expected)

    def test_root_union_duplicates_and_nested_roots_do_not_duplicate_rows(self):
        a, b = self.project / 'inner' / 'a', self.other / 'b'
        self.record(a)
        self.record(b)
        roots = [self.project, self.project / 'inner', self.other, self.project]
        self.assertEqual(self.paths(self.store.list_files(roots=roots)), {str(a), str(b)})
        self.assertEqual(len(self.store.history(roots=roots)), 2)
        self.assertEqual(self.store.list_files(roots=[]), self.store.list_files())

    def test_root_is_literal_case_sensitive_and_supports_filesystem_root(self):
        literal = self.base / "literal%_'quoted"
        target = literal / 'data'
        self.record(target)
        self.record(self.base / 'literalXXquoted' / 'data')
        self.assertEqual(self.paths(self.store.list_files(roots=[literal])), {str(target)})
        self.assertEqual(len(self.store.list_files(roots=[Path(self.base.anchor)])), 2)
        self.assertEqual(self.paths(self.store.list_files(roots=[target])), {str(target)})
        case = self.base / 'PROJECT' / 'data'
        self.record(case)
        expected = {str(case)} if os.name == 'nt' else set()
        self.assertEqual(self.paths(self.store.list_files(roots=[self.base / 'project'])), expected)

    @unittest.skipIf(os.name == 'nt', 'backslash is a path separator on Windows')
    def test_posix_backslash_is_a_literal_path_character(self):
        root = self.base / 'name\\segment'
        self.record(root / 'data')
        self.record(self.base / 'name' / 'segment' / 'data')
        self.assertEqual(self.paths(self.store.list_files(roots=[root])), {str(root / 'data')})

    @unittest.skipIf(os.name == 'nt', 'Windows uses case-insensitive path functions')
    def test_posix_scoped_queries_search_path_indexes_instead_of_scanning(self):
        self.record(self.project / 'data')
        self.record(self.other / 'data')
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            self.store.list_files(roots=[self.project])
            self.store.history(roots=[self.project])
        finally:
            self.store.db.set_trace_callback(None)
        queries = [sql for sql in statements if sql.startswith(('SELECT f.*', 'SELECT e.*'))]
        self.assertEqual(len(queries), 2)
        for query, alias, index in zip(queries, ('f', 'e'), ('sqlite_autoindex_files_1', 'events_path')):
            details = [row['detail'] for row in self.store.db.execute('EXPLAIN QUERY PLAN ' + query)]
            searches = [detail for detail in details if f'SEARCH {alias} ' in detail and index in detail]
            self.assertTrue(any('path=?' in detail for detail in searches), details)
            self.assertTrue(any('path>? AND path<?' in detail for detail in searches), details)
            self.assertFalse(any(f'SCAN {alias}' in detail for detail in details), details)

    def test_missing_relative_and_expanded_roots_are_index_only(self):
        root = self.project / 'removed'
        self.record(root / 'result', present=0)
        self.project.mkdir()
        with contextlib.chdir(self.project):
            relative = self.store.list_files(roots=['./removed/../removed'], include_missing=True)
        self.assertEqual(self.paths(relative), {str(root / 'result')})
        with patch.dict(os.environ, {'HOME': str(self.project), 'USERPROFILE': str(self.project)}):
            expanded = self.store.list_files(roots=['~/removed'], include_missing=True)
        self.assertEqual(relative, expanded)
        self.assertEqual(self.store.list_files(roots=[root]), [])
        with patch('pathlib.Path.resolve', side_effect=AssertionError('query resolved a path')), \
             patch('pathlib.Path.stat', side_effect=AssertionError('query inspected a path')), \
             patch('pathlib.Path.lstat', side_effect=AssertionError('query inspected a link')), \
             patch('os.scandir', side_effect=AssertionError('query scanned a directory')):
            self.assertEqual(self.paths(self.store.history(roots=[root])), {str(root / 'result')})
            self.assertEqual(self.store.list_sessions(roots=[root]), [])

    def test_query_root_does_not_follow_link_or_linked_parent(self):
        target, alias = self.base / 'target', self.base / 'alias'
        target.mkdir()
        try:
            alias.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        actual = target / 'inner' / 'data'
        historical = alias / 'inner' / 'data'
        self.record(actual)
        self.record(historical)
        self.assertEqual(self.paths(self.store.list_files(roots=[alias])), {str(historical)})
        self.assertEqual(self.paths(self.store.history(roots=[alias / 'inner'])), {str(historical)})

    def test_file_filters_and_pagination_intersect_after_root_selection(self):
        sid = self.session([self.project], status='finished', agent='needle')
        old = self.project / 'old'
        fid = self.record(old, session=sid, size=10, label='keep')
        current = self.session([self.project], status='finished')
        self.store.db.execute('UPDATE files SET session_id=? WHERE id=?', (current, fid))
        self.record(self.project / 'medium', session=sid, size=20, label='keep', note='needle')
        self.record(self.project / 'large', session=sid, size=30, label='keep')
        self.record(self.project / 'excluded-label', session=sid, size=40, label='review')
        self.record(self.other / 'outside', session=sid, size=100, label='keep')
        rows = self.store.list_files('needle', 'size', 'keep', False, 1, 1, sid, [self.project])
        self.assertEqual(self.paths(rows), {str(self.project / 'medium')})
        self.assertIn(str(old), self.paths(self.store.list_files(session=sid, roots=[self.project])))

    def test_history_preserves_overlapping_session_gaps_and_intersects_filters(self):
        ancestor = self.session([self.base], status='finished')
        inside = self.session([self.project], status='finished')
        outside = self.session([self.other], status='finished')
        path = self.project / 'file'
        self.record(path, session=inside)
        self.record(self.other / 'file', session=outside)
        for sid in (ancestor, inside, outside):
            self.store.event('', 'gap', 'test', sid, detail=sid)
        self.store.event('', 'gap', 'test', detail='unknown scope')
        self.store.event(str(self.other / 'failure'), 'gap', 'test', inside, detail='outside path')
        self.store.event('', 'unresolved', 'test', inside)
        rows = self.store.history(roots=[self.project])
        self.assertEqual({row['detail'] for row in rows if row['kind'] == 'gap'}, {ancestor, inside})
        self.assertEqual(len(rows), 3)
        selected = self.store.history(session=inside, roots=[self.project])
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(row['session_id'] == inside for row in selected))
        self.assertEqual(self.paths(self.store.history(path=path, session=inside, roots=[self.project])), {str(path)})
        self.assertEqual(self.store.history(path=path, roots=[self.other]), [])
        self.assertEqual(self.store.history(session=outside, roots=[self.project]), [])
        self.assertEqual(len(self.store.history()), 8)

    def test_history_root_filter_applies_before_pagination(self):
        expected = []
        for index in range(3):
            path = self.project / str(index)
            self.record(path)
            expected.append(str(path))
            self.record(self.other / str(index))
        rows = self.store.history(limit=1, offset=1, roots=[self.project])
        self.assertEqual([row['path'] for row in rows], [expected[1]])

    def test_sessions_overlap_in_both_directions_and_filter_before_paging(self):
        ancestor = self.session([self.base], status='finished')
        exact = self.session([self.project], status='finished')
        descendant = self.session([self.project / 'child'], status='failed')
        multiple = self.session([self.other, self.project / 'nested'], status='finished')
        self.session([self.other], status='finished')
        expected = {ancestor, exact, descendant, multiple}
        self.assertEqual({row['id'] for row in self.store.list_sessions([self.project])}, expected)
        self.assertEqual(self.store.list_sessions([self.project], 'finished', 1, 1)[0]['id'], exact)
        union = self.store.list_sessions([self.project, self.project / 'child', self.project])
        self.assertEqual(len(union), 4)
        running = self.session([self.project])
        for _ in range(105):
            self.session([self.other], status='finished')
        self.assertNotIn(running, {row['id'] for row in self.store.list_sessions()})
        self.assertEqual([row['id'] for row in self.store.list_sessions([self.project], 'running')], [running])
        self.assertEqual(self.store.active_for(self.project / 'output'), [running])

    def test_all_queries_are_read_only_and_work_on_read_only_connections(self):
        sid = self.session([self.project])
        self.record(self.project / 'data', session=sid)
        self.store.event('', 'gap', 'test', sid)
        before = list(self.store.db.iterdump())
        changes = self.store.db.total_changes
        self.store.db.execute('PRAGMA query_only=ON')
        self.store.list_files(roots=[self.project])
        self.store.history(roots=[self.project])
        self.store.list_sessions([self.project], 'running')
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(list(self.store.db.iterdump()), before)
        read_only = Store(self.store.home, read_only=True)
        try:
            self.assertEqual(len(read_only.list_files(roots=[self.project])), 1)
            self.assertEqual(len(read_only.history(roots=[self.project])), 2)
            self.assertEqual(len(read_only.list_sessions([self.project])), 1)
        finally:
            read_only.close()

    def test_query_page_bounds_and_status_validation(self):
        for query in (self.store.list_files, self.store.history, self.store.list_sessions):
            for invalid in (0, 10001):
                with self.assertRaises(SafetyError):
                    query(limit=invalid)
            for invalid in (-1, 2**63):
                with self.assertRaises(SafetyError):
                    query(offset=invalid)
        with self.assertRaises(SafetyError):
            self.store.list_sessions(status='not-a-status')

    def test_cli_roots_combine_with_search_history_and_session_filters(self):
        sid = self.session([self.project])
        target = self.project / 'needle'
        self.record(target, session=sid, label='keep')
        self.record(self.other / 'needle', label='keep')
        self.assertEqual(self.paths(self.cli('list', '--root', self.project, '--label', 'keep')), {str(target)})
        self.assertEqual(self.paths(self.cli('search', 'needle', '--root', self.project, '--session', sid)), {str(target)})
        self.assertEqual(self.paths(self.cli('history', '--root', self.project, '--session', sid)), {str(target)})
        rows = self.cli('sessions', '--root', self.project, '--status', 'running')
        self.assertEqual([row['session'] for row in rows], [sid])
        self.assertEqual(rows[0]['roots'], [str(self.project)])
        self.assertEqual(len(self.cli('list', '--root', self.project, '--root', self.other)), 2)


if __name__ == '__main__':
    unittest.main()
