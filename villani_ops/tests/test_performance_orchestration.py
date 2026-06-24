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
    
    def classify(self, task, backends, out_path=None):
        c=TaskClassification(difficulty='easy',category='bugfix',risk='low')
        if out_path: Path(out_path).write_text(c.model_dump_json(indent=2))
        return c, LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')
    monkeypatch.setattr('villani_ops.classification.classifier.TaskClassifier.classify', classify)
    monkeypatch.setattr('villani_ops.performance.investigator.Investigator.investigate', lambda self, task, cls, backends, run_dir: (__import__('villani_ops.performance.models', fromlist=['InvestigationResult']).InvestigationResult(summary='look'), LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')))
    q=list(reviews)
    def review(self, task, classification, coding_backend, attempt, backends, out_path=None):
        r=q.pop(0)
        if out_path: Path(out_path).write_text(r.model_dump_json(indent=2))
        return r, LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')
    monkeypatch.setattr('villani_ops.review.reviewer.LLMReviewer.review', review)
    if selection:
        def sel(self, task, inv, candidates, backends, run_dir):
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
