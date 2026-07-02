import json, os, stat, subprocess
from pathlib import Path

from villani_ops.cost_policy.executor import CostPolicyVillaniOps as VillaniOps
from villani_ops.controller.human_approval import TestHumanApprovalProvider, NonInteractiveHumanApprovalProvider
from villani_ops.core.backend import Backend
from villani_ops.core.task import Task, TaskClassification
from villani_ops.llm.client import LLMCallResult
from villani_ops.policy_engine.engine import ExecutionStrategy, StrategyAttempt
from villani_ops.review.reviewer import ReviewResult
from villani_ops.storage.files import FileStorage


def git_repo(path: Path):
    path.mkdir(); subprocess.run(['git','init'],cwd=path,check=True,capture_output=True, timeout=10)
    subprocess.run(['git','config','user.email','a@b.c'],cwd=path,check=True, timeout=10); subprocess.run(['git','config','user.name','A'],cwd=path,check=True, timeout=10)
    (path/'hello.txt').write_text('hello\n'); subprocess.run(['git','add','.'],cwd=path,check=True, timeout=10); subprocess.run(['git','commit','-m','init'],cwd=path,check=True,capture_output=True, timeout=10)


def fake_villani(path: Path, exit_code=0):
    exe=path/'villani-code'
    exe.write_text(f"#!/usr/bin/env python\nimport pathlib, sys\nrepo=pathlib.Path(sys.argv[sys.argv.index('--repo')+1])\n(repo/'hello.txt').write_text('changed '+str(len(list(repo.glob('marker*'))))+'\\n')\n(repo/('marker'+str(len(list(repo.glob('marker*')))))).write_text('x')\nsys.exit({exit_code})\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    os.environ['PATH']=str(path)+os.pathsep+os.environ['PATH']


def make_ops(tmp_path, monkeypatch, reviews, strategy=None, provider=None, exit_code=0):
    ws=tmp_path/'.villani-ops'; s=FileStorage(ws); s.init_workspace()
    backs={
      'code': Backend(name='code',provider='openai-compatible',base_url='http://x/v1',model='m',api_key='dummy',roles=['coding','classification','policy','review']),
      'strong': Backend(name='strong',provider='openai-compatible',base_url='http://x/v1',model='s',api_key='dummy',roles=['coding'])
    }
    s.save_backends(backs); fake_villani(tmp_path, exit_code=exit_code)
    monkeypatch.setattr('villani_ops.classification.classifier.TaskClassifier.classify', lambda self, task, backends, out_path=None: (TaskClassification(difficulty='easy',category='bugfix',risk='low'), LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')))
    strat=strategy or ExecutionStrategy(profile='balanced', attempts=[StrategyAttempt(backend='code', max_attempts=len(reviews))])
    monkeypatch.setattr('villani_ops.policy_engine.engine.PolicyEngine.generate', lambda self, cls, backends, profile, out_path=None: (strat, LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')))
    q=list(reviews)
    def review(self, task, classification, coding_backend, attempt, backends, out_path=None):
        r=q.pop(0)
        if isinstance(r, Exception): raise r
        if out_path: Path(out_path).write_text(r.model_dump_json(indent=2))
        return r, LLMCallResult(parsed_json={}, raw_text='{}', backend_name='code', model='m')
    monkeypatch.setattr('villani_ops.review.reviewer.LLMReviewer.review', review)
    return VillaniOps(s, human_approval_provider=provider), ws


def transitions(run_dir):
    return [(x['state_before'], x['action'], x['state_after']) for x in map(json.loads, (run_dir/'controller_steps.jsonl').read_text().splitlines())]


def test_happy_path_full_lifecycle_and_report(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops, ws=make_ops(tmp_path, monkeypatch, [ReviewResult(passed=True,decision='pass',recommended_action='accept',score=.9)])
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), non_interactive=True)
    t=transitions(Path(res.run_dir))
    assert [('planned','classify','classifying'),('classifying','generate_strategy','planning'),('planning','run_attempt','attempting'),('attempting','review_attempt','reviewing'),('reviewing','decide','deciding'),('deciding','accept','accepted')]==t[:6]
    d=json.loads((Path(res.run_dir)/'decision.json').read_text()); assert d['final_state']=='accepted' and d['controller_steps']
    report = (Path(res.run_dir)/'report.md').read_text()
    assert '| Step | Attempt | From | Action | To | Reason |' in report
    timeline = report.split('## Controller Timeline', 1)[1].split('## Controller Decision Steps', 1)[0]
    assert '\\n' not in timeline
    assert '| Step | Attempt | From | Action | To | Reason |\n| --- | --- | --- | --- | --- | --- |' in timeline
    assert (repo/'hello.txt').read_text()=='hello\n'


def test_retry_lifecycle_records_retrying(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops,_=make_ops(tmp_path, monkeypatch, [ReviewResult(decision='fail',recommended_action='retry_same_backend'), ReviewResult(passed=True,decision='pass',recommended_action='accept')])
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), non_interactive=True)
    assert res.decision.accepted and res.decision.retries_used==1 and ('deciding','retry_same_backend','retrying') in transitions(Path(res.run_dir))


def test_escalation_lifecycle_records_escalating(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    strat=ExecutionStrategy(profile='balanced', attempts=[StrategyAttempt(backend='code'), StrategyAttempt(backend='strong')])
    ops,_=make_ops(tmp_path, monkeypatch, [ReviewResult(decision='fail',recommended_action='escalate'), ReviewResult(passed=True,decision='pass',recommended_action='accept')], strategy=strat)
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), non_interactive=True)
    assert res.decision.accepted and res.decision.escalations_used==1 and ('deciding','escalate','escalating') in transitions(Path(res.run_dir))


def test_human_accept_requested_by_reviewer_overrides_nonzero(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops,_=make_ops(tmp_path, monkeypatch, [ReviewResult(decision='uncertain',recommended_action='ask_human',requires_human_approval=True)], provider=TestHumanApprovalProvider('accept'), exit_code=1)
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), non_interactive=False)
    assert res.decision.accepted and res.decision.human_override_used
    assert ('reviewing','ask_human','human_review') in transitions(Path(res.run_dir))


def test_noninteractive_human_skip_does_not_override(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops,_=make_ops(tmp_path, monkeypatch, [ReviewResult(decision='uncertain',recommended_action='ask_human',requires_human_approval=True)], provider=NonInteractiveHumanApprovalProvider(), exit_code=1)
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), non_interactive=True)
    assert not res.decision.accepted and res.decision.human_reviews_skipped==1


def test_review_failure_goes_to_deciding_and_fails(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops,_=make_ops(tmp_path, monkeypatch, [RuntimeError('bad json')])
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), non_interactive=True)
    assert not res.decision.accepted
    assert any(a=='decide' and to=='deciding' for _,a,to in transitions(Path(res.run_dir)))


def test_classification_failure_records_failed(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo); ops,ws=make_ops(tmp_path, monkeypatch, [])
    monkeypatch.setattr('villani_ops.classification.classifier.TaskClassifier.classify', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('classify boom')))
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), non_interactive=True)
    assert not res.decision.accepted and transitions(Path(res.run_dir))[-1]==('classifying','fail','failed')


def test_policy_failure_records_failed(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo); ops,ws=make_ops(tmp_path, monkeypatch, [])
    monkeypatch.setattr('villani_ops.policy_engine.engine.PolicyEngine.generate', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('policy boom')))
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), non_interactive=True)
    assert not res.decision.accepted and transitions(Path(res.run_dir))[-1]==('planning','fail','failed')
