import json, os, stat, subprocess
from pathlib import Path
from typer.testing import CliRunner

from villani_ops.cli.main import app
from villani_ops.controller.executor import VillaniOps
from villani_ops.core.backend import Backend
from villani_ops.core.task import Task, TaskClassification
from villani_ops.llm.client import LLMCallResult
from villani_ops.review.reviewer import ReviewResult
from villani_ops.storage.files import FileStorage

runner=CliRunner()

def git_repo(path):
    path.mkdir(); subprocess.run(['git','init'],cwd=path,check=True,capture_output=True)
    subprocess.run(['git','config','user.email','a@b.c'],cwd=path,check=True); subprocess.run(['git','config','user.name','A'],cwd=path,check=True)
    (path/'hello.txt').write_text('hello\n'); subprocess.run(['git','add','.'],cwd=path,check=True); subprocess.run(['git','commit','-m','init'],cwd=path,check=True,capture_output=True)

def fake_villani(path):
    exe=path/'villani-code'; exe.write_text("#!/usr/bin/env python\nimport pathlib, sys\nrepo=pathlib.Path(sys.argv[sys.argv.index('--repo')+1])\n(repo/'hello.txt').write_text('changed\\n')\nsys.exit(0)\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR); os.environ['PATH']=str(path)+os.pathsep+os.environ['PATH']

def make_ops(tmp_path, monkeypatch, reviews, selection=None):
    ws=tmp_path/'.villani-ops'; s=FileStorage(ws); s.init_workspace(); fake_villani(tmp_path)
    s.save_backends({'code': Backend(name='code',provider='openai-compatible',base_url='http://x/v1',model='m',api_key='dummy',roles=['coding','classification','review','investigation','selection'])})
    
    def classify(self, task, backends, out_path=None, backend_override=None):
        c=TaskClassification(difficulty='easy',category='bugfix',risk='low')
        if out_path: Path(out_path).write_text(c.model_dump_json(indent=2))
        return c, LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')
    monkeypatch.setattr('villani_ops.classification.classifier.TaskClassifier.classify', classify)
    monkeypatch.setattr('villani_ops.performance.investigator.Investigator.investigate', lambda self, task, cls, backend_name, backend, run_dir: (__import__('villani_ops.performance.models', fromlist=['InvestigationResult']).InvestigationResult(summary='look'), LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')))
    q=list(reviews)
    def review(self, task, classification, coding_backend, attempt, backends, out_path=None, backend_override=None):
        r=q.pop(0)
        if out_path: Path(out_path).write_text(r.model_dump_json(indent=2))
        return r, LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')
    monkeypatch.setattr('villani_ops.review.reviewer.LLMReviewer.review', review)
    if selection:
        def sel(self, task, inv, candidates, backend_name, backend, run_dir):
            return selection, None
        monkeypatch.setattr('villani_ops.performance.selector.Selector.select', sel)
    return VillaniOps(s), ws

def test_run_rejects_policy(tmp_path):
    res=runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--policy','balanced'])
    assert res.exit_code != 0
    assert 'Cost policies moved to villani-ops cost-run' in res.output

def test_performance_artifacts_and_all_candidates(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops, ws=make_ops(tmp_path, monkeypatch, [ReviewResult(passed=True,decision='pass',recommended_action='accept',score=.9) for _ in range(3)])
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), candidate_attempts=3, non_interactive=True)
    rd=Path(res.run_dir)
    for rel in ['task.json','classification.json','investigation.json','attempts/attempt_001/attempt.json','attempts/attempt_001/review.json','selection.json','decision.json','report.md']:
        assert (rd/rel).exists()
    assert len(res.attempts)==3
    assert res.decision.accepted

def test_selector_cannot_choose_ineligible_fallbacks(tmp_path, monkeypatch):
    from villani_ops.performance.models import SelectionResult
    repo=tmp_path/'repo'; git_repo(repo)
    ops, ws=make_ops(tmp_path, monkeypatch, [ReviewResult(decision='fail',recommended_action='fail',score=.1), ReviewResult(passed=True,decision='pass',recommended_action='accept',score=.8)], SelectionResult(decision='select', selected_attempt_id='attempt_001'))
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), candidate_attempts=2, non_interactive=True)
    assert res.decision.accepted and res.decision.winning_attempt_id=='attempt_002'


def test_performance_backend_selection_rules():
    from villani_ops.performance.backend_selection import select_performance_backend
    backends={
        'expensive': Backend(name='expensive',provider='openai-compatible',model='m1',capability_score=10,input_cost_per_million=100,output_cost_per_million=100,roles=['review']),
        'cheap': Backend(name='cheap',provider='openai-compatible',model='m2',capability_score=10,input_cost_per_million=0,output_cost_per_million=0,roles=['coding']),
        'disabled': Backend(name='disabled',provider='openai-compatible',model='m3',capability_score=99,enabled=False),
    }
    name, backend=select_performance_backend(backends)
    assert name == 'cheap'
    assert backend.model == 'm2'


def test_no_enabled_backend_errors():
    from villani_ops.performance.backend_selection import select_performance_backend
    try:
        select_performance_backend({'x': Backend(name='x', provider='openai-compatible', model='m', enabled=False)})
    except ValueError as e:
        assert 'No enabled backend configured for performance orchestration' in str(e)
    else:
        raise AssertionError('expected ValueError')


def test_run_rejects_backend_human_and_unknown(tmp_path):
    cases=[
        (['--backend','local-qwen'], 'Performance orchestration always uses the most capable enabled backend'),
        (['--human-approval'], 'Human approval is not supported in performance orchestration'),
        (['--bogus','value'], 'Unknown option: --bogus'),
    ]
    for args, msg in cases:
        res=runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x', *args])
        assert res.exit_code != 0
        assert msg in res.output


def test_decision_mode_and_selector_input_no_cost(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops, ws=make_ops(tmp_path, monkeypatch, [ReviewResult(passed=True,decision='pass',recommended_action='accept',score=.9)])
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), candidate_attempts=1, non_interactive=True)
    rd=Path(res.run_dir)
    assert res.decision.mode == 'performance'
    payload=json.loads((rd/'selection_input.json').read_text())
    cand=payload['candidates'][0]
    for key in ['patch_text','review','stdout_tail','stderr_tail','git_status','changed_files','acceptance_blockers']:
        assert key in cand
    assert 'cost' not in json.dumps(payload).lower()
    report=(rd/'report.md').read_text().lower()
    for term in ['cheap','balanced','quality','cost savings','selected by cost']:
        assert term not in report
