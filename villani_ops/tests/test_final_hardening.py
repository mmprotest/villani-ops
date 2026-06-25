import json, os, stat, subprocess
import pytest
from pathlib import Path
from typer.testing import CliRunner

from villani_ops.cli.main import app
from villani_ops.controller.state_machine import HumanApprovalResult
from villani_ops.core.acceptance import is_attempt_acceptance_eligible, human_override_blockers
from villani_ops.controller.human_approval import TestHumanApprovalProvider
from villani_ops.review.reviewer import ReviewResult
from villani_ops.policy_engine.engine import ExecutionStrategy, StrategyAttempt
from villani_ops.tests.test_controller_lifecycle_hardening import git_repo, make_ops, transitions
from villani_ops.tests.test_v02_hardening import init_git, make_run

runner = CliRunner()


def _attempt(human, patch=True, changed=True):
    return {
        'status':'human_approved','exit_code':1,'error':'boom',
        'patch_path':__file__ if patch else None,
        'changed_files':['hello.txt'] if changed else [],
        'review':{'decision':'fail','passed':False,'recommended_action':'fail'},
        'human_approval':human,
    }


def _valid_human(**updates):
    h={
        'requested':True,'request_reasons':['reviewer_recommended_ask_human'],
        'prompted':True,'skipped_reason':None,'decision':'accept','valid_override':True,
        'shown_evidence':{'patch_path':__file__,'changed_files':['hello.txt'],'reviewer_summary':'bad but approved','acceptance_blockers':['runner exit code is 1']},
    }
    h.update(updates)
    return h


def test_human_override_strict_acceptance_matrix():
    assert is_attempt_acceptance_eligible(_attempt(_valid_human())) == (True, [])
    for human in [
        {k:v for k,v in _valid_human().items() if k!='valid_override'},
        _valid_human(valid_override=False),
        _valid_human(skipped_reason='non_interactive'),
        _valid_human(request_reasons=[]),
        _valid_human(shown_evidence={'changed_files':['hello.txt'],'reviewer_summary':'x','acceptance_blockers':[]}),
    ]:
        ok, blockers = is_attempt_acceptance_eligible(_attempt(human))
        assert not ok and blockers
    for decision in ['reject','retry','escalate','fail','skipped']:
        ok, blockers = human_override_blockers(_attempt(_valid_human(decision=decision)))
        assert not ok and any('human decision' in b for b in blockers)


@pytest.mark.parametrize("decision", ['reject','retry','escalate','fail','skipped'])
def test_human_reject_retry_escalate_fail_skipped_never_override(decision):
    ok, blockers = human_override_blockers(_attempt(_valid_human(decision=decision)))
    assert not ok and any('human decision' in b for b in blockers)


@pytest.mark.parametrize("mutator, expected", [
    (lambda h: {k:v for k,v in h.items() if k!='valid_override'}, 'valid_override'),
    (lambda h: {**h, 'valid_override': False}, 'valid_override'),
    (lambda h: {**h, 'skipped_reason': 'non_interactive'}, 'skipped'),
    (lambda h: {**h, 'request_reasons': []}, 'request reasons'),
    (lambda h: {**h, 'shown_evidence': {'changed_files':['hello.txt'],'reviewer_summary':'x','acceptance_blockers':[]}}, 'patch path'),
])
def test_human_accept_malformed_variants_do_not_override(mutator, expected):
    ok, blockers = is_attempt_acceptance_eligible(_attempt(mutator(_valid_human())))
    assert not ok and any(expected in b for b in blockers)


class MalformedAcceptProvider:
    def __init__(self, approval): self.approval=approval
    def request_approval(self, context):
        h=_valid_human(); h.update(self.approval)
        return HumanApprovalResult.model_validate(h)


def test_malformed_human_approval_does_not_override_and_report_has_blockers(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops,_=make_ops(tmp_path, monkeypatch, [ReviewResult(decision='uncertain',recommended_action='ask_human',requires_human_approval=True)], provider=MalformedAcceptProvider({'valid_override':False}), exit_code=1)
    res=ops.run(repo, task=__import__('villani_ops.core.task', fromlist=['Task']).Task(repo_path=str(repo), objective='edit'), non_interactive=False)
    assert not res.decision.accepted
    step=json.loads((Path(res.run_dir)/'attempts'/'attempt_001'/'controller_decision.json').read_text())
    assert step['human_override_used'] is False and step['human_override_blockers']
    text=(Path(res.run_dir)/'report.md').read_text()
    assert 'human_override_used: false' in text and 'Human override blockers' in text


def test_valid_human_override_accepts_nonzero_and_uncertain(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    ops,_=make_ops(tmp_path, monkeypatch, [ReviewResult(decision='uncertain',recommended_action='ask_human',requires_human_approval=True)], provider=TestHumanApprovalProvider('accept'), exit_code=1)
    res=ops.run(repo, task=__import__('villani_ops.core.task', fromlist=['Task']).Task(repo_path=str(repo), objective='edit'), non_interactive=False)
    assert res.decision.accepted and res.decision.human_override_used
    assert 'human_override_used: true' in (Path(res.run_dir)/'report.md').read_text()


def test_retry_and_escalation_attempt_ids_are_new_on_next_attempt(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    strat=ExecutionStrategy(profile='balanced', attempts=[StrategyAttempt(backend='code', max_attempts=2), StrategyAttempt(backend='strong')])
    reviews=[ReviewResult(decision='fail',recommended_action='retry_same_backend'), ReviewResult(decision='fail',recommended_action='escalate'), ReviewResult(passed=True,decision='pass',recommended_action='accept')]
    ops,_=make_ops(tmp_path, monkeypatch, reviews, strategy=strat)
    res=ops.run(repo, task=__import__('villani_ops.core.task', fromlist=['Task']).Task(repo_path=str(repo), objective='edit'), non_interactive=True)
    steps=[json.loads(x) for x in (Path(res.run_dir)/'controller_steps.jsonl').read_text().splitlines()]
    pairs=[(s['attempt_id'], s['state_before'], s['action'], s['state_after']) for s in steps]
    assert ('attempt_001','deciding','retry_same_backend','retrying') in pairs
    assert ('attempt_002','retrying','run_attempt','attempting') in pairs
    assert ('attempt_002','deciding','escalate','escalating') in pairs
    assert ('attempt_003','escalating','run_attempt','attempting') in pairs
    assert [a['attempt_id'] for a in res.attempts] == ['attempt_001','attempt_002','attempt_003']
    report=(Path(res.run_dir)/'report.md').read_text()
    assert '| attempt_002 | retrying | run_attempt | attempting |' in report
    assert '| attempt_003 | escalating | run_attempt | attempting |' in report


def _fake_villani(path: Path, count_file: Path, fail=False):
    exe=path/'villani-code'
    exe.write_text(f"#!/usr/bin/env python\nimport pathlib, sys\npathlib.Path(r'{count_file}').write_text(str(int(pathlib.Path(r'{count_file}').read_text() or '0')+1))\nrepo=pathlib.Path(sys.argv[sys.argv.index('--repo')+1])\n(repo/'hello.txt').write_text('changed\\n')\nsys.exit({1 if fail else 0})\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


def _setup_compare(tmp_path, monkeypatch, reviews):
    repo=tmp_path/'repo'; git_repo(repo); ws=tmp_path/'.villani-ops'
    subprocess.run(['git','status','--porcelain'],cwd=repo,check=True,capture_output=True)
    assert runner.invoke(app,['init','--workspace',str(ws)]).exit_code==0
    for name,cost in [('cheap',1),('balanced',2),('quality',3)]:
        assert runner.invoke(app,['backend','add',name,'--provider','openai-compatible','--base-url','http://x/v1','--model',name,'--api-key','dummy','--input-cost',str(cost),'--output-cost',str(cost),'--roles','coding,classification,policy,review','--workspace',str(ws)]).exit_code==0
    count=tmp_path/'count.txt'; count.write_text('0'); _fake_villani(tmp_path, count)
    monkeypatch.setenv('PATH', str(tmp_path)+os.pathsep+os.environ['PATH'])
    from villani_ops.core.task import TaskClassification
    from villani_ops.llm.client import LLMCallResult
    cats=[('easy','bugfix'),('hard','feature')]
    def classify(self, task, backends, out_path=None):
        idx=0 if (task.task_id or '').endswith('1') else 1
        return TaskClassification(difficulty=cats[idx][0], category=cats[idx][1], risk='low'), LLMCallResult(parsed_json={}, raw_text='{}', backend_name='cheap', model='cheap', input_tokens=10, output_tokens=5, estimated_cost=.01)
    monkeypatch.setattr('villani_ops.classification.classifier.TaskClassifier.classify', classify)
    def generate(self, cls, backends, profile, out_path=None):
        return ExecutionStrategy(profile=profile, strategy_summary=f'{profile} strategy', attempts=[StrategyAttempt(backend=profile, max_attempts=1)]), LLMCallResult(parsed_json={}, raw_text='{}', backend_name=profile, model=profile, input_tokens=8, output_tokens=4, estimated_cost=.02)
    monkeypatch.setattr('villani_ops.policy_engine.engine.PolicyEngine.generate', generate)
    q=list(reviews)
    def review(self, task, classification, coding_backend, attempt, backends, out_path=None):
        r=q.pop(0) if q else ReviewResult(passed=True,decision='pass',recommended_action='accept',score=.8)
        if out_path: Path(out_path).write_text(r.model_dump_json())
        return r, LLMCallResult(parsed_json={}, raw_text='{}', backend_name=coding_backend.name, model=coding_backend.model, input_tokens=6, output_tokens=3, estimated_cost=.03)
    monkeypatch.setattr('villani_ops.review.reviewer.LLMReviewer.review', review)
    tasks=tmp_path/'tasks.jsonl'; tasks.write_text('\n'.join([json.dumps({'id':'t1','objective':'edit 1','success_criteria':'ok'}), json.dumps({'id':'t2','objective':'edit 2','success_criteria':'ok'}), json.dumps({'id':'t3','objective':'edit 3','success_criteria':'ok'})])+'\n')
    return repo, ws, tasks, count


def test_compare_basic_repeat_resume_breakdowns_and_safety(tmp_path, monkeypatch):
    reviews=[ReviewResult(passed=True,decision='pass',recommended_action='accept',score=.9)]*12
    repo, ws, tasks, count = _setup_compare(tmp_path, monkeypatch, reviews)
    before=(repo/'hello.txt').read_text(); status_before=subprocess.run(['git','status','--porcelain'],cwd=repo,text=True,capture_output=True).stdout
    out=tmp_path/'comparison.md'
    res=runner.invoke(app,['compare','--repo',str(repo),'--tasks',str(tasks),'--policies','cheap','--policies','balanced','--policies','quality','--repeat','2','--max-tasks','2','--workspace',str(ws),'--out',str(out)], catch_exceptions=False)
    assert res.exit_code==0, res.output
    data=json.loads(out.with_suffix('.json').read_text())
    assert len(data)==12 and {r['trial_index'] for r in data}=={0,1} and {r['task_id'] for r in data}=={'t1','t2'}
    required={'comparison_id','policy','task_id','trial_index','run_id','accepted','final_action','failure_reason','difficulty','category','risk','strategy_summary','attempts_used','retries_used','escalations_used','winning_backend','winning_model','reviewer_score','total_cost','classification_cost','policy_cost','coding_cost','review_cost','total_input_tokens','total_output_tokens','wall_time_seconds','report_path'}
    assert required <= set(data[0])
    assert out.exists() and out.with_suffix('.csv').exists() and all(Path(r['report_path']).exists() for r in data)
    text=out.read_text(); assert '## Thesis Signal' in text and 'Failure reason breakdown' in text and 'Category/difficulty breakdown' in text and 'significance' not in text.lower()
    assert (repo/'hello.txt').read_text()==before and subprocess.run(['git','status','--porcelain'],cwd=repo,text=True,capture_output=True).stdout==status_before
    calls=int(count.read_text())
    res2=runner.invoke(app,['compare','--repo',str(repo),'--tasks',str(tasks),'--policies','cheap','--policies','balanced','--policies','quality','--repeat','2','--max-tasks','2','--resume','--workspace',str(ws),'--out',str(out)], catch_exceptions=False)
    assert res2.exit_code==0 and len(json.loads(out.with_suffix('.json').read_text()))==12 and int(count.read_text())==calls
    assert len({r['run_id'] for r in data}) == len(data)


def test_compare_zero_accepts_and_failure_breakdown(tmp_path, monkeypatch):
    repo, ws, tasks, count = _setup_compare(tmp_path, monkeypatch, [ReviewResult(decision='fail',recommended_action='fail',score=.1)]*6)
    out=tmp_path/'comparison.md'
    res=runner.invoke(app,['compare','--repo',str(repo),'--tasks',str(tasks),'--policies','cheap','--policies','balanced','--policies','quality','--max-tasks','2','--workspace',str(ws),'--out',str(out)], catch_exceptions=False)
    assert res.exit_code==0
    rows=json.loads(out.with_suffix('.json').read_text())
    assert all(not r['accepted'] and r['failure_reason'] for r in rows)
    text=out.read_text(); assert 'N/A' in text and 'Failure reason breakdown' in text and 'Attempt is not acceptable' in text


def test_pr_prepare_success_push_and_gh_failures_branch_dirty_patch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); repo=tmp_path/'repo'; init_git(repo); ws=tmp_path/'.villani-ops'
    good='diff --git a/hello.txt b/hello.txt\nindex ce01362..2e09960 100644\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+pr\n'
    rd=make_run(ws, repo, good)
    monkeypatch.setattr('shutil.which', lambda n: None if n=='gh' else '/usr/bin/'+n)
    res=runner.invoke(app,['pr','r1','--title','T','--body','B','--prepare-branch','--workspace',str(ws)])
    assert res.exit_code==0 and json.loads((rd/'pr.json').read_text())['commit_sha']
    subprocess.run(['git','checkout','master'],cwd=repo,check=True,capture_output=True)
    init_git(tmp_path/'repo2'); rd2=make_run(ws, tmp_path/'repo2', good); (tmp_path/'repo2'/'hello.txt').write_text('dirty\n')
    assert runner.invoke(app,['pr','r1','--title','T','--body','B','--prepare-branch','--workspace',str(ws)]).exit_code!=0
    assert 'dirty' in json.loads((rd2/'pr.json').read_text())['stderr']

    repo3=tmp_path/'repo3'; init_git(repo3); rd3=make_run(ws, repo3, good)
    remote=tmp_path/'remote.git'; subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True); subprocess.run(['git','remote','add','origin',str(remote)],cwd=repo3,check=True)
    gh=tmp_path/'gh'; calls=tmp_path/'gh_calls.txt'; gh.write_text(f"#!/usr/bin/env python\nimport pathlib, sys\npathlib.Path(r'{calls}').write_text(' '.join(sys.argv))\nprint('https://example.test/pr/1')\n") ; gh.chmod(gh.stat().st_mode|stat.S_IXUSR)
    monkeypatch.setenv('PATH', str(tmp_path)+os.pathsep+os.environ['PATH']); monkeypatch.setattr('shutil.which', lambda n: str(gh) if n=='gh' else None)
    res=runner.invoke(app,['pr','r1','--title','T','--body','B','--force-branch','--workspace',str(ws)])
    art=json.loads((rd3/'pr.json').read_text()); assert res.exit_code==0 and art['url']=='https://example.test/pr/1' and '--title T --body B' in calls.read_text()

    repo4=tmp_path/'repo4'; init_git(repo4); rd4=make_run(ws, repo4, good)
    calls.unlink(missing_ok=True)
    monkeypatch.setattr('shutil.which', lambda n: str(gh) if n=='gh' else None)
    res=runner.invoke(app,['pr','r1','--title','T','--body','B','--workspace',str(ws)])
    art=json.loads((rd4/'pr.json').read_text()); assert res.exit_code!=0 and art['commit_sha'] and art['manual_commands'] and art['recovery_instructions'] and not calls.exists()

    repo5=tmp_path/'repo5'; init_git(repo5); bad='diff --git a/missing.txt b/missing.txt\nindex 1111111..2222222 100644\n--- a/missing.txt\n+++ b/missing.txt\n@@ -1 +1 @@\n-x\n+y\n'; rd5=make_run(ws, repo5, bad)
    res=runner.invoke(app,['pr','r1','--title','T','--body','B','--prepare-branch','--workspace',str(ws)])
    assert res.exit_code!=0 and subprocess.run(['git','branch','--show-current'],cwd=repo5,text=True,capture_output=True).stdout.strip()=='master'
    assert 'git apply --check failed' in json.loads((rd5/'pr.json').read_text())['stderr']
