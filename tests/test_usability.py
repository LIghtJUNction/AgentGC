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
from agentgc.cleanup import mark
from agentgc.diagnostics import classify, summarize
from agentgc.output import markdown
from agentgc.paths import SafetyError, Scope
from agentgc.store import Store
from agentgc.tracker import scan_once


class UsabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'work'
        self.root.mkdir()
        self.store = Store(self.base / 'state')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def file(self, name):
        path = self.root / name
        path.write_text('data', encoding='utf-8')
        return path

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(['--home', str(self.store.home), '--json', *map(str, args)])
        return code, json.loads(out.getvalue())

    def test_new_file_immediately_indexed_without_writer_claim(self):
        path = self.file('receipt.json')
        code, result = self.cli('mark', path, 'keep')
        self.assertEqual(code, 0)
        self.assertEqual(result['label'], 'keep')
        self.assertEqual(result['source'], 'manual-observation')
        self.assertIsNone(result['session_id'])
        self.assertEqual(self.store.db.execute("SELECT kind FROM events ORDER BY id").fetchall()[0][0], 'observed')

    def test_batch_partial_failure_and_duplicates(self):
        a, b = self.file('a'), self.file('b')
        code, result = self.cli('mark', a, b, a, self.root / 'missing', 'temporary')
        self.assertEqual(code, 1)
        self.assertEqual((result['marked'], result['failed']), (2, 1))
        self.assertEqual(self.store.file(a)['label'], 'temporary')
        self.assertIn('failed=1', markdown(result, 'mark'))

    def test_hundreds_of_new_files_in_one_cli_call(self):
        paths = [self.file(f'output-{n}') for n in range(250)]
        code, result = self.cli('mark', *paths, 'temporary', '--note', 'Intermediate outputs')
        self.assertEqual((code, result['marked'], result['failed']), (0, 250, 0))
        self.assertEqual(self.store.stats()['labels'], {'temporary': 250})

    def test_compound_gap_does_not_hide_other_impacts(self):
        category, impact = classify('Incomplete event read: Permission denied; Link or reparse point is not allowed: /a; No such file: /b')
        self.assertIn('permission', category)
        self.assertIn('link-boundary', category)
        self.assertIn('path-disappeared', category)
        self.assertIn('unknown', impact)
        category, _ = classify('Tracking interrupted by FileNotFoundError; intermediate history may be incomplete')
        self.assertEqual(category, 'tracking-failure')  # failed spawn is not a transient file

    def test_manifest_and_stdin_null_paths(self):
        a = self.file('space name')
        b = self.file('other name' if os.name == 'nt' else 'new\nline')
        manifest = self.base / 'files.txt'
        manifest.write_text(str(a) + '\n' + str(a) + '\n', encoding='utf-8')
        code, result = self.cli('mark', 'keep', '--files-from', manifest)
        self.assertEqual((code, result['marked']), (0, 1))
        with patch('sys.stdin', io.StringIO(str(a) + '\0' + str(b) + '\0')):
            code, result = self.cli('mark', 'review', '--files-from', '-', '--null')
        self.assertEqual((code, result['marked']), (0, 2))

    def test_new_disposable_still_requires_note_and_stopped_session(self):
        path = self.file('a')
        with self.assertRaises(SafetyError):
            mark(self.store, path, 'disposable')
        self.assertEqual(self.store.stats()['files'], 0)  # registration rolled back
        self.store.start_session('test', [self.root], 'poll')
        with self.assertRaises(SafetyError):
            mark(self.store, path, 'disposable', 'approved')
        self.assertEqual(self.store.stats()['files'], 0)

    def test_existing_changed_disposable_still_refused(self):
        path = self.file('a')
        scan_once(self.store, Scope([self.root], self.store.home))
        path.write_text('changed contents')
        with self.assertRaises(SafetyError):
            mark(self.store, path, 'disposable', 'approved')

    def test_links_and_internal_state_not_registered(self):
        target = self.file('a')
        link = self.base / 'link'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        with self.assertRaises(SafetyError):
            mark(self.store, link / target.name, 'keep')
        with self.assertRaises(SafetyError):
            mark(self.store, self.store.home / 'index.sqlite3', 'keep')
        with self.assertRaises(SafetyError):
            mark(self.store, self.root, 'keep')
        self.assertEqual(self.store.stats()['files'], 0)

    def test_grouped_retained_diagnostics_not_lost_file_counts(self):
        sid = self.store.start_session('test', [self.root], 'poll')
        for _ in range(2):
            self.store.event('', 'gap', 'tracker', sid, detail='Incomplete scan: Permission denied')
            self.store.event(str(self.root / 'a'), 'deleted-unresolved', 'filesystem-event', sid)
        self.store.event('', 'gap', 'tracker', sid, detail='Link or reparse point is not allowed: /x')
        self.store.event('', 'gap', 'tracker', sid, detail='No such file or directory: /tmp/a')
        self.store.event('', 'gap', 'tracker', sid, detail='Native event queue overflow')
        self.store.db.execute('UPDATE sessions SET gaps=5 WHERE id=?', (sid,))
        result = summarize(self.store, sid)
        self.assertEqual(result['groups'][0]['count'], 2)
        self.assertEqual(len(result['groups']), 4)
        self.assertEqual(result['retained_unresolved_events'], 2)
        self.assertEqual(result['unresolved_paths'], 1)
        code, payload = self.cli('diagnostics', '--session', sid)
        self.assertEqual(code, 0)
        self.assertEqual(payload, result)
        text = markdown(result, 'diagnostics', row_limit=1)
        for category in ('permission', 'link-boundary', 'path-disappeared', 'event-loss-risk'):
            self.assertIn(category, text)
        self.assertIn('not lost files', text)
        self.assertEqual(self.store.stats()['events'], 7)
