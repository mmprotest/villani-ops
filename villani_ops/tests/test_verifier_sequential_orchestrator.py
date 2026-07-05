from __future__ import annotations
import json, subprocess
from pathlib import Path
from typer.testing import CliRunner
from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerResult
from villani_ops.orchestrator.verifier_sequential import VerifierSequentialConfig, VerifierSequentialOrchestrator


def init_repo(p:Path):
    p.mkdir(); subprocess.run(['git','init'],cwd=p,check=True,capture_output=True); subprocess.run(['git','config','user.email','a@b.c'],cwd=p,check=True); subprocess.run(['git','config','user.name','T'],cwd=p,check=True); (p/'a.txt').write_text('a'); subprocess.run(['git','add','.'],cwd=p,check=True); subprocess.run(['git','commit','-m','init'],cwd=p,check=True,capture_output=True)

class FakeRunner:
    def __init__(self, fail_first=False, no_debug_first=False): self.calls=[]; self.fail_first=fail_first; self.no_debug_first=no_debug_first
    def run_task(self, *, repo_path, task, success_criteria, backend_name, backend_config, timeout_seconds, context, artifacts_dir):
        cid=context['attempt_id']; self.calls.append(cid)
        if self.fail_first and cid=='candidate-001': raise RuntimeError('boom')
        (Path(repo_path)/'a.txt').write_text(cid)
        if self.no_debug_first and cid=='candidate-001': return RunnerResult(exit_code=1, stdout='', stderr='bad')
        dbg=artifacts_dir/'debug'; dbg.mkdir(parents=True); (dbg/'session_meta.json').write_text('{}')
        return RunnerResult(exit_code=0, stdout='out', stderr='', debug_artifact_dir=str(dbg), duration_ms=1)

def make_orch(tmp_path, results, *, candidates=5, on_all_fail='fail', runner=None, repo_git=False):
    repo=tmp_path/'repo'
    if repo_git: init_repo(repo)
    else: repo.mkdir(parents=True); (repo/'a.txt').write_text('original')
    runner=runner or FakeRunner(); ver_calls=[]
    def verifier(**kw):
        cid=kw['repo_dir'].parent.name; ver_calls.append(cid); r=results.get(cid, 0)
        if r == 'error': raise RuntimeError('verifier timeout')
        return {'result':r,'verdict':'success' if r==1 else 'failure','confidence': {'candidate-001':.1,'candidate-002':.9}.get(cid,.5),'recommendedAction':'accept' if r==1 else 'reject','traceDir':str(kw['trace_dir'])}
    cfg=VerifierSequentialConfig(repo=repo,task='do it',candidates=candidates,seed=1,workspace=tmp_path/'ws',backend='b',on_all_fail=on_all_fail,keep_worktrees=True)
    orch=VerifierSequentialOrchestrator(cfg, runner=runner, verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    return orch, runner, ver_calls, repo

def test_first_candidate_succeeds_stops_and_skips(tmp_path):
    orch, runner, ver_calls, repo = make_orch(tmp_path, {'candidate-001':1})
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert runner.calls==['candidate-001'] and ver_calls==['candidate-001']
    assert out['winnerCandidateId']=='candidate-001' and out['stoppedEarly'] is True
    rows=[json.loads(l) for l in (od/'candidates.jsonl').read_text().splitlines()]
    assert [r['candidateId'] for r in rows if r['status']=='skipped']==['candidate-002','candidate-003','candidate-004','candidate-005']
    assert json.loads((od/'selection.json').read_text())['selectionPolicy']=='binary_verifier_first_success'
    assert (repo/'a.txt').read_text()=='candidate-001'

def test_second_candidate_succeeds(tmp_path):
    orch, runner, ver_calls, repo = make_orch(tmp_path, {'candidate-001':0,'candidate-002':1})
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert runner.calls==['candidate-001','candidate-002'] and ver_calls==['candidate-001','candidate-002']
    assert out['winnerCandidateId']=='candidate-002' and out['attemptedCandidates']==2 and out['skippedCandidates']==3
    assert len((od/'candidate-runs.jsonl').read_text().splitlines())==2
    assert len((od/'verifier-results.jsonl').read_text().splitlines())==2
    assert '## Skipped Candidates' in (od/'transcript.md').read_text()


def test_all_fail_fail_no_integration(tmp_path):
    orch, runner, ver_calls, repo = make_orch(tmp_path, {'candidate-001':0,'candidate-002':0}, candidates=2)
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert runner.calls==['candidate-001','candidate-002'] and out['status']=='failed' and out['winnerCandidateId'] is None
    assert out['skippedCandidates']==0 and json.loads((od/'integration.json').read_text())['status']=='skipped'


def test_all_fail_random_and_best_confidence(tmp_path):
    orch, *_ = make_orch(tmp_path/'r', {'candidate-001':0,'candidate-002':0}, candidates=2, on_all_fail='random')
    out=orch.run(); assert out['status']=='completed' and out['winnerCandidateId'] in {'candidate-001','candidate-002'}
    orch, *_ = make_orch(tmp_path/'b', {'candidate-001':0,'candidate-002':0}, candidates=2, on_all_fail='best-confidence')
    out=orch.run(); assert out['winnerCandidateId']=='candidate-002'


def test_candidate_error_and_verifier_error_continue(tmp_path):
    orch, runner, ver_calls, repo = make_orch(tmp_path/'c', {'candidate-002':1}, candidates=2, runner=FakeRunner(fail_first=True))
    out=orch.run(); assert out['winnerCandidateId']=='candidate-002'
    orch, runner, ver_calls, repo = make_orch(tmp_path/'v', {'candidate-001':'error','candidate-002':1}, candidates=2)
    out=orch.run(); assert out['winnerCandidateId']=='candidate-002'
    orch, runner, ver_calls, repo = make_orch(tmp_path/'n', {'candidate-002':1}, candidates=2, runner=FakeRunner(no_debug_first=True))
    out=orch.run(); assert out['winnerCandidateId']=='candidate-002' and ver_calls==['candidate-002']


def test_artifacts_and_non_git_source(tmp_path):
    orch, runner, ver_calls, repo = make_orch(tmp_path, {'candidate-001':1}, candidates=1)
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert json.loads((od/'orchestration.json').read_text())['mode']=='verifier-sequential'
    assert out['schemaVersion']=='villani-ops-verifier-sequential-output-v1'
    assert not (repo/'.git').exists()
    assert (od/'candidates'/'candidate-001'/'worktree'/'.git').exists()


def test_cli_validation_json_and_run_route(monkeypatch, tmp_path):
    r=CliRunner(); repo=tmp_path/'r'; repo.mkdir()
    assert r.invoke(app,['orchestrate','verifier-sequential','--task','x']).exit_code!=0
    assert r.invoke(app,['orchestrate','verifier-sequential','--repo',str(repo),'--task','x','--task-file',str(repo/'t')]).exit_code!=0
    assert r.invoke(app,['orchestrate','verifier-sequential','--repo',str(repo),'--task','x','--candidates','0']).exit_code!=0
    def fake_run(self): return {'schemaVersion':'villani-ops-verifier-sequential-output-v1','status':'completed','winnerCandidateId':'c','winnerResult':1,'stoppedEarly':True,'attemptedCandidates':1,'skippedCandidates':0,'selectionPath':'s','integrationPath':'i','orchestrationDir':'o','candidates':[]}
    monkeypatch.setattr(VerifierSequentialOrchestrator,'run',fake_run)
    res=r.invoke(app,['orchestrate','verifier-sequential','--repo',str(repo),'--task','x','--json'])
    assert res.exit_code==0 and json.loads(res.stdout)['schemaVersion'].endswith('sequential-output-v1')
    res=r.invoke(app,['run','--repo',str(repo),'--task','x','--orchestrator','verifier-sequential','--candidate-attempts','4','--workspace',str(tmp_path/'ws')])
    assert res.exit_code==0 and 'Verifier sequential orchestration: completed' in res.stdout


def test_bypasses_planner_classifier_and_ground_truth_names_absent(monkeypatch, tmp_path):
    import villani_ops.orchestrator.verifier_sequential as vs
    assert 'reward.txt' not in Path(vs.__file__).read_text()
    assert "/'result.json'" not in Path(vs.__file__).read_text()
    assert "/\"result.json\"" not in Path(vs.__file__).read_text()


def test_sequential_early_stop_ignores_later_higher_quality_candidate(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir(); (repo/'a.txt').write_text('original')
    runner=FakeRunner()
    ver_calls=[]
    def verifier(**kw):
        cid=kw['repo_dir'].parent.name; ver_calls.append(cid)
        if cid == 'candidate-001':
            return {'result':1,'verdict':'success','confidence':0.5,'riskFlags':['risk'],'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
        return {'result':1,'verdict':'success','confidence':0.99,'riskFlags':[],'successEvidence':['Behavioral validation test passed'],'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierSequentialConfig(repo=repo,task='do it',candidates=2,seed=1,workspace=tmp_path/'ws',backend='b',keep_worktrees=True)
    orch=VerifierSequentialOrchestrator(cfg, runner=runner, verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run()
    assert out['winnerCandidateId']=='candidate-001'
    assert runner.calls==['candidate-001']
    assert ver_calls==['candidate-001']
    assert out['stoppedEarly'] is True
