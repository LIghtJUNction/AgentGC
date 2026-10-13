import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agentgc.cli import emit, main
from agentgc.output import inline, markdown


class OutputTests(unittest.TestCase):
    def test_unicode_markdown_and_hostile_values(self):
        row = {'path':'/测试/a`\n<script>\x1b[31m\u202e', 'size':None,
               'label':'review','source':'filesystem-event','present':0}
        text = markdown([row], 'list')
        self.assertIn('/测试/', text)
        self.assertIn(r'\n', text)
        self.assertIn(r'\u001b', text)
        self.assertIn(r'\u202e', text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('\x1b', text)
        self.assertIn('size=unknown', text)
        self.assertIn('Missing', text)
        self.assertTrue(inline('x``y').startswith('``` '))

    def test_json_payload_exact_and_formatting_non_mutating(self):
        data = {'observations':[{'path':'测试','rule_id':'x','state':'active','evaluation':'unknown'}],
                'changes':[], 'notifications':[], 'complete':False,'gaps':[{'reason':'queue overflow'}]}
        original = deepcopy(data)
        human = io.StringIO()
        emit(data, False, human, 'sync')
        machine = io.StringIO()
        emit(data, True, machine, 'sync')
        self.assertEqual(json.loads(machine.getvalue()),original)
        self.assertEqual(data,original)
        self.assertIn('unknown=1',human.getvalue())
        self.assertIn('changes=0',human.getvalue())
        self.assertIn('Incomplete',human.getvalue())
        self.assertIn('queue overflow',human.getvalue())

    def test_list_cap_offset_and_json_full_rows(self):
        data = [{'path':f'/p/{i}', 'size':1,'label':'keep','source':'baseline'} for i in range(25)]
        text = markdown(data,'list',offset=100)
        self.assertIn('Showing 20/25',text)
        self.assertIn('--offset 120',text)
        self.assertNotIn('` /p/24 `',text)
        out=io.StringIO();emit(data,True,out,'list')
        self.assertEqual(len(json.loads(out.getvalue())),25)

    def test_no_second_cap_on_gc_recover_or_pending_recovery(self):
        items=[{'path':f'/file/{i}','id':f'full-id-{i}','eligible':False,'reason':f'refusal-{i}',
                'original':f'/file/{i}','destination':f'/q/{i}','status':'needs-review'} for i in range(30)]
        gc=markdown({'mode':'preview','items':items,'truncated':True,'bytes_freed':0},'gc')
        self.assertIn('/file/29',gc)
        self.assertIn('refusal-29',gc)
        self.assertIn('truncated=true',gc)
        self.assertIn('does not free space',gc)
        for text in (markdown({'mode':'preview','items':items},'recover'),
                     markdown(items,'quarantine'),markdown({'pending_recovery':items},'doctor')):
            self.assertIn('full-id-29',text)
            self.assertIn('/q/29',text)
            self.assertIn('needs-review',text)

    def test_all_gaps_and_errors_visible_and_safe(self):
        gaps=[{'path':f'/gap/{i}','reason':f'reason-{i}'} for i in range(30)]
        text=markdown({'complete':False,'gaps':gaps,'gap_count':99,'gaps_truncated':True},'pressure')
        self.assertIn('/gap/29',text)
        self.assertIn('reason-29',text)
        self.assertIn('gap_count=99',text)
        self.assertIn('gaps_truncated=true',text)
        for command in ('gc','rules','sync','scan','pressure'):
            self.assertIn('permission denied',markdown({'error':'permission denied'},command))

    def test_run_missing_tracking_gap_data_is_not_success_claim(self):
        text=markdown({'session':'full-session-id','exit_code':0},'run')
        self.assertIn('full-session-id',text)
        self.assertIn('unavailable',text)
        self.assertNotIn('gaps=0',text)

    def test_summary_omits_internal_hashes_but_is_substantially_shorter(self):
        data=[{'path':f'/项目/{i}', 'size':1024,'label':'keep','source':'baseline',
               'signature':'x'*100,'review_hash':'y'*64,'first_seen':1,'last_seen':2,
               'id':i,'present':1,'note':''} for i in range(20)]
        text=markdown(data,'list')
        self.assertNotIn('review_hash',text)
        self.assertLess(len(text),len(json.dumps(data,ensure_ascii=False))/2)
        self.assertIn('logical bytes',text)

    def test_empty_and_single_record_outputs(self):
        self.assertIn('None returned',markdown([],'history'))
        self.assertIn('installed=',markdown({'installed':'/skills/agentgc'},'skill'))
        self.assertIn('label=',markdown({'path':'/a','label':'keep','note':'result'},'mark'))
        self.assertIn('rules',markdown({'rules':[{'id':'x','enabled':False}],'project':'/p'},'rules'))

    def test_default_cli_markdown_and_json_and_error_exit(self):
        with tempfile.TemporaryDirectory() as root:
            for machine in (False,True):
                out,err=io.StringIO(),io.StringIO()
                args=['--home',root,'stats'] + (['--json'] if machine else [])
                with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                    code=main(args)
                self.assertEqual(code,0,err.getvalue())
                if machine:self.assertEqual(json.loads(out.getvalue())['files'],0)
                else:self.assertTrue(out.getvalue().startswith('## stats'))
            out,err=io.StringIO(),io.StringIO()
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                code=main(['--home',root,'--no-project-rules','mark',str(Path(root)/'missing'),'keep'])
            self.assertEqual(code,2)
            self.assertEqual(out.getvalue(),'')
            self.assertIn('### error',err.getvalue())

    def test_bundled_skill_prefers_markdown_except_programmatic_queries(self):
        from importlib.resources import files
        skill=files('agentgc').joinpath('skills/agentgc/SKILL.md').read_text()
        query=skill.split('## Query',1)[1].split('## Finish',1)[0]
        self.assertIn('Prefer default compact Markdown',query)
        self.assertIn('programmatic parsing',query)
        self.assertIn('agcc list --sort size --limit 20',query)
        self.assertNotIn('agcc --json list',query)
        self.assertIn('agcc --json doctor',skill)

    def test_real_run_preserves_child_streams_and_exit_code(self):
        with tempfile.TemporaryDirectory() as base:
            root=Path(base)/'work';root.mkdir()
            home=Path(base)/'state'
            env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
            for machine in (False,True):
                args=[sys.executable,'-m','agentgc','--home',str(home),'--no-project-rules']
                if machine:args+=['--json']
                args+=['run','--root',str(root),'--backend','poll','--',sys.executable,'-c',
                       "import sys; print('child stdout'); print('child stderr',file=sys.stderr); sys.exit(7)"]
                proc=subprocess.run(args,cwd=root,env=env,text=True,capture_output=True,timeout=10)
                self.assertEqual(proc.returncode,7,proc.stderr)
                self.assertEqual(proc.stdout,'child stdout\n')
                self.assertIn('child stderr\n',proc.stderr)
                if machine:self.assertEqual(json.loads(proc.stderr.splitlines()[-1])['exit_code'],7)
                else:self.assertIn('## run',proc.stderr)


if __name__ == '__main__':
    unittest.main()
