import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from agentgc.cli import main
from agentgc.paths import SafetyError, Scope
from agentgc.rules import load, glob_match, quantity
from agentgc.store import Store
from agentgc.sync import reconcile, marks
from agentgc.tracker import scan_once


RULE = '''version = 1
extends = []
[[rules]]
id = "test"
[rules.match]
path = ["target", "**/target"]
kind = "directory"
[rules.when]
any = [{ size_gte = "100 B" }]
[rules.then]
attention = "large"
[rules.notify]
clear_below = 0.8
cooldown = "24h"
'''


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()/'project'
        self.root.mkdir()
        self.store = Store(Path(self.tmp.name)/'state')
        self.addCleanup(self.store.close)
        self.config = self.root/'.agcc'
        self.config.write_text(RULE)
        self.file = self.root/'target'/'out'
        self.file.parent.mkdir()
        self.file.write_bytes(b'x'*200)

    def settings(self):
        return load(self.root)

    def cli(self, *args):
        output,error = io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(error):
            code = main(['--home',str(self.store.home),'--config',str(self.config),'--json',*args])
        return code,output.getvalue(),error.getvalue()

    def test_rule_history_prune_is_opt_in_preview_first_and_preserves_state(self):
        reconcile(self.store, self.settings(), [self.root])
        self.store.db.execute('UPDATE auto_events SET at=0')
        before = {table: [tuple(r) for r in self.store.db.execute('SELECT * FROM ' + table)]
                  for table in ('auto_marks', 'sync_baselines', 'files', 'sessions', 'quarantine')}
        code, out, err = self.cli('prune', '--days', '30', '--apply')
        self.assertEqual(code, 0, err)
        self.assertNotIn('rule_history', json.loads(out))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM auto_events').fetchone()[0], 1)
        code, out, err = self.cli('prune', '--days', '30', '--rule-history')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['rule_history']['events'], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM auto_events').fetchone()[0], 1)
        code, out, err = self.cli('prune', '--days', '30', '--rule-history', '--apply')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['rule_history']['events'], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM auto_events').fetchone()[0], 0)
        after = {table: [tuple(r) for r in self.store.db.execute('SELECT * FROM ' + table)]
                 for table in before}
        self.assertEqual(after, before)

    def test_rule_history_prune_protects_running_and_stale_sessions(self):
        reconcile(self.store, self.settings(), [self.root])
        self.store.db.execute('UPDATE auto_events SET at=0')
        sid = self.store.start_session('writer', [self.root], 'poll')
        self.store.db.execute('UPDATE sessions SET heartbeat=0,pid=-1 WHERE id=?', (sid,))
        for apply in ((), ('--apply',)):
            code, out, err = self.cli('prune', '--rule-history', *apply)
            self.assertEqual(code, 0, err)
            history = json.loads(out)['rule_history']
            self.assertTrue(history['skipped'])
            self.assertEqual(history['events'], 0)
            self.assertIn('running session', history['reason'])
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM auto_events').fetchone()[0], 1)
        self.assertEqual(self.store.stats()['active_sessions'], 1)

    def test_rule_history_prune_keeps_cutoff_recent_and_unknown_timestamps(self):
        from unittest.mock import patch
        reconcile(self.store, self.settings(), [self.root])
        cutoff = 10_000_000 - 30 * 86400
        self.store.db.execute('UPDATE auto_events SET at=?', (cutoff - 1,))
        for at in (cutoff, cutoff + 1, None):
            self.store.db.execute('INSERT INTO auto_events(at) VALUES(?)', (at,))
        with patch('agentgc.cli.time.time', return_value=10_000_000):
            code, out, err = self.cli('prune', '--rule-history', '--apply')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['rule_history']['events'], 1)
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT at FROM auto_events ORDER BY id')],
                         [cutoff, cutoff + 1, None])

    def test_rule_history_prune_does_not_create_optional_tables(self):
        for apply in ((), ('--apply',)):
            code, out, err = self.cli('prune', '--rule-history', *apply)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['rule_history']['events'], 0)
            self.assertFalse(json.loads(out)['rule_history']['skipped'])
        self.assertIsNone(self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE name='auto_events'").fetchone())

    def test_discovery_from_subdirectory_and_git_boundary(self):
        self.assertEqual(load(self.file.parent)['config'],str(self.config))
        (self.file.parent/'.git').mkdir()
        self.assertIsNone(load(self.file.parent)['config'])
        self.assertIsNone(load(self.root,disabled=True)['config'])

    def test_unknown_fields_units_disposal_and_duplicate_ids_rejected(self):
        for text in (RULE+'\nfoo = true\n', RULE.replace('100 B','2G'),
                     RULE.replace('attention = "large"','suggest = "disposable"'),
                     RULE+'\n[[rules]]\nid = "test"\nenabled = false\n',
                     RULE.replace('version = 1','version = 2'),
                     RULE.replace('[rules.match]','[rules.match]\ncategory = []'),
                     RULE.replace('"target", "**/target"','"../target"')):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(SafetyError):self.settings()
        self.assertEqual(quantity('1 GiB'),1024**3)
        self.assertEqual(quantity('1 GB'),1000**3)

    def test_builtin_merge_replaces_arrays_and_keeps_other_fields(self):
        self.config.write_text('version = 1\n[[rules]]\nid="build-pressure"\n[rules.when]\nany=[{size_gte="2 GiB"}]\n')
        rule=next(r for r in self.settings()['rules'] if r['id']=='build-pressure')
        self.assertEqual(len(rule['when']['any']),1)
        self.assertEqual(rule['match']['category'],'build')
        self.assertIn('attention',rule['then'])

    def test_globs_are_anchored_and_double_star_includes_zero_segments(self):
        self.assertTrue(glob_match('target','**/target'))
        self.assertTrue(glob_match('apps/rust/target','**/target'))
        self.assertFalse(glob_match('apps/rust/target','*/target'))
        self.assertFalse(glob_match('target/debug','target'))

    def test_preview_no_new_state_and_manual_keep_untouched(self):
        scan_once(self.store,Scope([self.root],self.store.home))
        self.store.db.execute("UPDATE files SET label='keep' WHERE path=?",(str(self.file),))
        result=reconcile(self.store,self.settings(),[self.root],dry_run=True)
        self.assertEqual(result['observations'][0]['state'],'active')
        self.assertEqual(marks(self.store),[])
        reconcile(self.store,self.settings(),[self.root])
        self.assertEqual(self.store.file(self.file)['label'],'keep')
        self.assertIsNone(self.store.file(self.file)['review_hash'])

    def test_hysteresis_cooldown_and_missing(self):
        first=reconcile(self.store,self.settings(),[self.root])
        self.assertEqual(len(first['notifications']),1)
        self.file.write_bytes(b'x'*90)
        second=reconcile(self.store,self.settings(),[self.root])
        self.assertEqual(second['observations'][0]['state'],'active')
        self.assertEqual(second['notifications'],[])
        self.file.write_bytes(b'x'*70)
        self.assertEqual(reconcile(self.store,self.settings(),[self.root])['observations'][0]['state'],'inactive')
        self.file.unlink()
        self.file.parent.rmdir()
        result=reconcile(self.store,self.settings(),[self.root])
        self.assertEqual(result['observations'][0]['state'],'missing')

    def test_incomplete_scan_cannot_clear_or_claim_missing(self):
        reconcile(self.store,self.settings(),[self.root])
        self.file.write_bytes(b'x'*10)
        result=reconcile(self.store,self.settings(),[self.root],max_entries=1)
        self.assertFalse(result['complete'])
        self.assertEqual(marks(self.store)[0]['state'],'active')

    def test_growth_uses_complete_compatible_baseline(self):
        self.config.write_text(RULE.replace('size_gte = "100 B"','growth_gte = "100 B", growth_percent_gte = 25'))
        self.assertEqual(reconcile(self.store,self.settings(),[self.root])['observations'][0]['state'],'unknown')
        self.file.write_bytes(b'x'*400)
        self.assertEqual(reconcile(self.store,self.settings(),[self.root])['observations'][0]['state'],'active')

    def test_custom_file_suggestions_and_disabled_rules(self):
        self.config.write_text(RULE.replace('kind = "directory"','kind = "file"').replace('"target", "**/target"','"target/out"').replace('attention = "large"','suggest = "temporary"'))
        result=reconcile(self.store,self.settings(),[self.root])
        self.assertEqual(result['observations'][0]['action']['suggest'],'temporary')
        self.assertEqual(result['observations'][0]['measurement']['kind'],'file')
        self.config.write_text(RULE.replace('id = "test"','id = "test"\nenabled = false'))
        result=reconcile(self.store,self.settings(),[self.root])
        self.assertEqual(result['observations'][0]['state'],'disabled')

    def test_link_config_and_external_roots_rejected(self):
        other=Path(self.tmp.name)/'other'
        other.mkdir()
        with self.assertRaises(SafetyError):reconcile(self.store,self.settings(),[other])
        self.config.unlink()
        real=other/'rules'
        real.write_text(RULE)
        try:self.config.symlink_to(real)
        except OSError:self.skipTest('symlinks unavailable')
        with self.assertRaises(SafetyError):self.settings()

    def test_cli_sync_preview_validation_and_auto_run_exit_code(self):
        code,out,err=self.cli('sync','--dry-run')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['mode'],'preview')
        self.assertEqual(marks(self.store),[])
        code,out,err=self.cli('sync')
        self.assertEqual(code,0,err)
        self.assertTrue(marks(self.store))
        code,out,err=self.cli('rules','validate')
        self.assertEqual(code,0,err)
        import sys, subprocess
        child=subprocess.run([sys.executable,'-m','agentgc','--home',str(self.store.home),
                              '--config',str(self.config),'--json','run','--root',str(self.root),
                              '--backend','poll','--',sys.executable,'-c','print("child");raise SystemExit(7)'],
                             capture_output=True,text=True)
        self.assertEqual(child.returncode,7,child.stderr)
        self.assertEqual(child.stdout,'child\n')
        self.assertIn('automatic',json.loads(child.stderr.splitlines()[-1]))

    def test_partial_measurement_does_not_replace_growth_baseline(self):
        self.config.write_text(RULE.replace('size_gte = "100 B"','growth_gte = "100 B"'))
        reconcile(self.store,self.settings(),[self.root])
        self.file.write_bytes(b'x'*300)
        reconcile(self.store,self.settings(),[self.root],max_entries=1)
        self.file.write_bytes(b'x'*350)
        result=reconcile(self.store,self.settings(),[self.root])
        self.assertEqual(result['observations'][0]['state'],'active')

    def test_rule_change_records_fingerprint_and_transition(self):
        first=reconcile(self.store,self.settings(),[self.root])
        self.config.write_text(RULE.replace('100 B','1000 B'))
        second=reconcile(self.store,self.settings(),[self.root])
        self.assertNotEqual(first['config_fingerprint'],second['config_fingerprint'])
        self.assertEqual(second['observations'][0]['state'],'inactive')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM auto_events').fetchone()[0],2)

    def test_dry_run_does_not_update_baseline_or_cooldown(self):
        first=reconcile(self.store,self.settings(),[self.root])
        before=marks(self.store)
        changes=self.store.db.total_changes
        self.file.write_bytes(b'x'*70)
        reconcile(self.store,self.settings(),[self.root],dry_run=True)
        self.assertEqual(marks(self.store),before)
        self.assertEqual(self.store.db.total_changes,changes)

    def test_exclusions_preserved_and_link_replacement_not_called_missing(self):
        self.config.write_text(RULE.replace('100 B','1 B'))
        scope=Scope([self.root],self.store.home,['out'])
        result=reconcile(self.store,self.settings(),[self.root],include=scope.includes,excludes=scope.excludes)
        self.assertEqual(result['observations'][0]['state'],'inactive')
        reconcile(self.store,self.settings(),[self.root])
        self.file.unlink()
        self.file.parent.rmdir()
        external=Path(self.tmp.name)/'external'
        external.mkdir()
        try:self.file.parent.symlink_to(external,target_is_directory=True)
        except OSError:self.skipTest('symlinks unavailable')
        result=reconcile(self.store,self.settings(),[self.root])
        self.assertEqual(marks(self.store)[0]['state'],'active')
        self.assertFalse(any(r['state']=='missing' for r in result['observations']))

    def test_project_pressure_uses_policy_and_command_override(self):
        code,out,err=self.cli('pressure','--root',str(self.root))
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['observations'][0]['state'],'active')
        self.assertEqual(marks(self.store),[])
        self.config.write_text('version=1\n')
        code,out,err=self.cli('pressure','--root',str(self.root),'--file-threshold','1','--apply')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['observations'][0]['state'],'active')

    def test_zero_budget_rejected(self):
        with self.assertRaises(SafetyError):reconcile(self.store,self.settings(),[self.root],max_entries=0)


if __name__ == '__main__':unittest.main()
