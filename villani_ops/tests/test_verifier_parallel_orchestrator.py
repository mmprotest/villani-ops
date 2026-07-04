from __future__ import annotations
import json, subprocess
from pathlib import Path
from typer.testing import CliRunner
from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerResult
from villani_ops.orchestrator.selection import select_winner
from villani_ops.orchestrator.verifier_parallel import VerifierParallelConfig, VerifierParallelOrchestrator


def test_selection_single_success_wins():
    s=select_winner([{'candidateId':'a','result':0},{'candidateId':'b','result':1}],1)
    assert s.winnerCandidateId=='b' and s.winnerResult==1

def test_selection_seed_reproducible_and_pool_recorded():
    cs=[{'candidateId':f'c{i}','result':1} for i in range(5)]
    assert select_winner(cs,42).winnerCandidateId==select_winner(cs,42).winnerCandidateId
    s=select_winner(cs,42); assert s.tieBreak and s.candidatePool==[f'c{i}' for i in range(5)]

def test_selection_failures_ignored_when_success_exists():
    assert select_winner([{'candidateId':'z','result':0,'confidence':1},{'candidateId':'s','result':1,'confidence':0}],2).winnerCandidateId=='s'

def test_selection_all_fail_fail_no_winner():
    s=select_winner([{'candidateId':'a','result':0}],1,'fail'); assert s.winnerCandidateId is None

def test_selection_all_fail_random_and_errors_rank_below_zero():
    s=select_winner([{'candidateId':'e','result':None},{'candidateId':'z','result':0}],3,'random')
    assert s.winnerCandidateId=='z'

def test_selection_best_confidence():
    s=select_winner([{'candidateId':'a','result':0,'confidence':.1},{'candidateId':'b','result':0,'confidence':.9}],1,'best-confidence')
    assert s.winnerCandidateId=='b'

def test_selection_all_errors_random():
    s=select_winner([{'candidateId':'e1','result':None},{'candidateId':'e2','verdict':'error'}],4,'random')
    assert s.winnerCandidateId in {'e1','e2'}

def init_repo(p:Path):
    p.mkdir(); subprocess.run(['git','init'],cwd=p,check=True,capture_output=True); subprocess.run(['git','config','user.email','a@b.c'],cwd=p,check=True); subprocess.run(['git','config','user.name','T'],cwd=p,check=True); (p/'a.txt').write_text('a'); subprocess.run(['git','add','.'],cwd=p,check=True); subprocess.run(['git','commit','-m','init'],cwd=p,check=True,capture_output=True)

class FakeRunner:
    def __init__(self): self.calls=[]
    def run_task(self, *, repo_path, task, success_criteria, backend_name, backend_config, timeout_seconds, context, artifacts_dir):
        self.calls.append((repo_path, task, context)); (Path(repo_path)/'a.txt').write_text(context['attempt_id'])
        dbg=artifacts_dir/'debug'; dbg.mkdir(parents=True); (dbg/'metadata.json').write_text('{}')
        return RunnerResult(exit_code=0, stdout='out', stderr='', debug_artifact_dir=str(dbg), duration_ms=1)

def test_flow_creates_worktrees_verifies_integrates_and_writes_artifacts(tmp_path):
    repo=tmp_path/'repo'; init_repo(repo); ws=tmp_path/'ws'
    runner=FakeRunner(); ver_calls=[]
    def verifier(**kw):
        ver_calls.append(kw); return {'result':1 if kw['repo_dir'].parent.name=='candidate-002' else 0,'verdict':'success','confidence':.8,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='do it',candidates=3,parallelism=2,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert len(runner.calls)==3 and len(ver_calls)==3
    assert all(c['repo_dir'].name == 'worktree' and c['repo_dir'].parent.name.startswith('candidate-') for c in ver_calls)
    assert out['winnerCandidateId']=='candidate-002'
    assert (repo/'a.txt').read_text()=='candidate-002'
    for name in ['orchestration.json','candidates.jsonl','candidate-runs.jsonl','verifier-results.jsonl','selection.json','integration.json','transcript.md']:
        assert (od/name).exists()
    assert len((od/'candidates.jsonl').read_text().splitlines())==3



def test_non_git_source_uses_copied_git_baseline_and_integrates(tmp_path):
    repo=tmp_path/'plain'; repo.mkdir(); (repo/'a.txt').write_text('original')
    ws=tmp_path/'ws'; runner=FakeRunner()
    def verifier(**kw):
        assert kw['repo_dir'].name=='worktree'
        assert (kw['repo_dir']/'.git').exists()
        assert not (repo/'.git').exists()
        return {'result':1,'verdict':'success','confidence':.9,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='do it',candidates=1,parallelism=1,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); od=Path(out['orchestrationDir']); cdir=od/'candidates'/'candidate-001'
    assert out['status']=='completed'
    assert (cdir/'worktree'/'.git').exists()
    assert (cdir/'diff.patch').exists() and 'a.txt' in (cdir/'diff.patch').read_text()
    assert (cdir/'run').is_dir() and (cdir/'verifier').is_dir()
    assert not (cdir/'worktrees').exists()
    assert not (repo/'.git').exists()
    assert (repo/'a.txt').read_text()=='candidate-001'
    rec=json.loads((od/'integration.json').read_text())
    assert rec['status']=='integrated' and rec['winnerCandidateId']=='candidate-001'


def test_git_source_uses_copied_git_baseline_and_keeps_source_repo_valid(tmp_path):
    repo=tmp_path/'repo'; init_repo(repo); ws=tmp_path/'ws'; runner=FakeRunner()
    def verifier(**kw): return {'result':1,'verdict':'success','confidence':.9,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='do it',candidates=1,parallelism=1,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert (od/'candidates'/'candidate-001'/'worktree'/'.git').exists()
    assert subprocess.run(['git','status','--porcelain'],cwd=repo,text=True,capture_output=True).returncode==0
    assert (repo/'a.txt').read_text()=='candidate-001'


def test_candidate_setup_helper_does_not_mutate_source(tmp_path):
    from villani_ops.isolation.copy_git import create_git_baselined_copy
    repo=tmp_path/'plain'; repo.mkdir(); (repo/'file.txt').write_text('x')
    before=sorted(str(p.relative_to(repo)) for p in repo.rglob('*'))
    copied=create_git_baselined_copy(repo, tmp_path/'cand')
    after=sorted(str(p.relative_to(repo)) for p in repo.rglob('*'))
    assert before==after
    assert not (repo/'.git').exists()
    assert (copied.worktree_path/'.git').exists()


def test_empty_patch_candidate_is_recorded_and_verified(tmp_path):
    repo=tmp_path/'plain'; repo.mkdir(); (repo/'a.txt').write_text('original')
    class NoopRunner:
        def run_task(self, **kw):
            dbg=kw['artifacts_dir']/'debug'; dbg.mkdir(parents=True); (dbg/'metadata.json').write_text('{}')
            return RunnerResult(exit_code=0, stdout='', stderr='', debug_artifact_dir=str(dbg), duration_ms=1)
    def verifier(**kw): return {'result':1,'verdict':'success','confidence':.9,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='noop',candidates=1,workspace=tmp_path/'ws',backend='b')
    orch=VerifierParallelOrchestrator(cfg, runner=NoopRunner(), verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); od=Path(out['orchestrationDir'])
    row=json.loads((od/'candidates.jsonl').read_text().splitlines()[0])
    assert row['patchStatus']=='empty' and row['result']==1
    assert out['status']=='failed'  # empty patch cannot be integrated by safe_apply


def test_one_candidate_failure_does_not_kill_successful_candidate(tmp_path):
    repo=tmp_path/'plain'; repo.mkdir(); (repo/'a.txt').write_text('original')
    class MixedRunner:
        def run_task(self, **kw):
            if kw['context']['attempt_id']=='candidate-001':
                raise RuntimeError('boom')
            (kw['repo_path']/'a.txt').write_text('good')
            dbg=kw['artifacts_dir']/'debug'; dbg.mkdir(parents=True); (dbg/'metadata.json').write_text('{}')
            return RunnerResult(exit_code=0, stdout='', stderr='', debug_artifact_dir=str(dbg), duration_ms=1)
    def verifier(**kw): return {'result':1,'verdict':'success','confidence':.9,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='x',candidates=2,parallelism=1,seed=1,workspace=tmp_path/'ws',backend='b')
    orch=VerifierParallelOrchestrator(cfg, runner=MixedRunner(), verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run()
    assert out['winnerCandidateId']=='candidate-002'
    assert (repo/'a.txt').read_text()=='good'

def test_failure_no_debug_becomes_verifier_error_and_all_fail_does_not_integrate(tmp_path):
    repo=tmp_path/'repo'; init_repo(repo)
    class BadRunner:
        def run_task(self, **kw): return RunnerResult(exit_code=1, stdout='', stderr='bad')
    cfg=VerifierParallelConfig(repo=repo,task='x',candidates=1,workspace=tmp_path/'ws',backend='b')
    orch=VerifierParallelOrchestrator(cfg, runner=BadRunner()); orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); assert out['status']=='failed' and out['winnerCandidateId'] is None


def test_cli_validation_and_json(monkeypatch, tmp_path):
    r=CliRunner(); repo=tmp_path/'r'; repo.mkdir()
    assert r.invoke(app,['orchestrate','verifier-parallel','--task','x']).exit_code!=0
    assert r.invoke(app,['orchestrate','verifier-parallel','--repo',str(repo),'--task','x','--task-file',str(repo/'t')]).exit_code!=0
    assert r.invoke(app,['orchestrate','verifier-parallel','--repo',str(repo),'--task','x','--candidates','0']).exit_code!=0
    assert r.invoke(app,['orchestrate','verifier-parallel','--repo',str(repo),'--task','x','--candidates','1','--parallelism','2']).exit_code!=0
    def fake_run(self): return {'schemaVersion':'villani-ops-verifier-parallel-output-v1','status':'completed','winnerCandidateId':'c','winnerResult':1,'selectionPath':'s','integrationPath':'i','orchestrationDir':'o','candidates':[]}
    monkeypatch.setattr(VerifierParallelOrchestrator,'run',fake_run)
    res=r.invoke(app,['orchestrate','verifier-parallel','--repo',str(repo),'--task','x','--json'])
    assert res.exit_code==0 and json.loads(res.stdout)['status']=='completed'
