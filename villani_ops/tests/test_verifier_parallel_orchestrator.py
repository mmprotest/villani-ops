from __future__ import annotations
import json, subprocess
from pathlib import Path
from typer.testing import CliRunner
from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerResult
from villani_ops.orchestrator.selection import select_winner
from villani_ops.orchestrator.verifier_parallel import VerifierParallelConfig, VerifierParallelOrchestrator, resolve_verifier_debug_dir, build_verifier_parallel_candidate_task




def _touch(path: Path, ts: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{}')
    if ts is not None:
        import os
        os.utime(path, (ts, ts))

def test_resolve_verifier_debug_dir_direct(tmp_path):
    debug=tmp_path/'debug'; _touch(debug/'session_meta.json')
    assert resolve_verifier_debug_dir(debug) == debug

def test_resolve_verifier_debug_dir_nested_timestamp(tmp_path):
    root=tmp_path/'villani_code_debug'; nested=root/'20260705T004006_753583Z'
    _touch(nested/'session_meta.json'); _touch(nested/'final_summary.json')
    assert resolve_verifier_debug_dir(root) == nested

def test_resolve_verifier_debug_dir_prefers_valid_resolved_trace_dir(tmp_path):
    root=tmp_path/'debug_root'; old=root/'old'; new=root/'new'
    _touch(old/'session_meta.json', 1); _touch(new/'session_meta.json', 2)
    assert resolve_verifier_debug_dir(root, old) == old

def test_resolve_verifier_debug_dir_invalid_resolved_trace_falls_back_to_child(tmp_path):
    root=tmp_path/'debug_root'; invalid=root/'invalid'; valid=root/'valid'
    invalid.mkdir(parents=True); _touch(valid/'session_meta.json')
    assert resolve_verifier_debug_dir(root, invalid) == valid

def test_resolve_verifier_debug_dir_chooses_newest_final_summary(tmp_path):
    root=tmp_path/'debug_root'; old=root/'old'; new=root/'new'
    _touch(old/'session_meta.json', 1); _touch(old/'final_summary.json', 10)
    _touch(new/'session_meta.json', 2); _touch(new/'final_summary.json', 20)
    assert resolve_verifier_debug_dir(root) == new

def test_resolve_verifier_debug_dir_returns_none_without_session_meta(tmp_path):
    root=tmp_path/'debug_root'; _touch(root/'child'/'final_summary.json')
    assert resolve_verifier_debug_dir(root) is None

def test_resolve_verifier_debug_dir_only_searches_one_extra_level(tmp_path):
    root=tmp_path/'debug_root'; deep=root/'a'/'b'/'c'
    _touch(deep/'session_meta.json')
    assert resolve_verifier_debug_dir(root) is None

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
        self.calls.append((repo_path, task, success_criteria, context)); (Path(repo_path)/'a.txt').write_text(context['attempt_id'])
        dbg=artifacts_dir/'debug'; dbg.mkdir(parents=True); (dbg/'session_meta.json').write_text('{}')
        return RunnerResult(exit_code=0, stdout='out', stderr='', debug_artifact_dir=str(dbg), duration_ms=1)



def test_verifier_parallel_config_stores_success_criteria():
    cfg=VerifierParallelConfig(repo=Path('.'), task='task', success_criteria='must pass')
    assert cfg.success_criteria == 'must pass'

def test_candidate_prompt_wrapper_preserves_task_and_criteria():
    task='Line 1\nLine 2 exactly'
    wrapped=build_verifier_parallel_candidate_task(task, 'criteria text', 'candidate-007')
    assert task in wrapped
    assert 'criteria text' in wrapped
    assert 'riskiest requirement' in wrapped
    assert 'not only the happy path' in wrapped

def test_flow_creates_worktrees_verifies_integrates_and_writes_artifacts(tmp_path):
    repo=tmp_path/'repo'; init_repo(repo); ws=tmp_path/'ws'
    runner=FakeRunner(); ver_calls=[]
    def verifier(**kw):
        ver_calls.append(kw); return {'result':1 if kw['repo_dir'].parent.name=='candidate-002' else 0,'verdict':'success','confidence':.8,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='do it',success_criteria='done criteria',candidates=3,parallelism=2,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert len(runner.calls)==3 and len(ver_calls)==3
    assert all(call[2]=='done criteria' for call in runner.calls)
    assert all('riskiest requirement' in call[1] for call in runner.calls)
    assert all(c['repo_dir'].name == 'worktree' and c['repo_dir'].parent.name.startswith('candidate-') for c in ver_calls)
    assert out['winnerCandidateId']=='candidate-002'
    assert (repo/'a.txt').read_text()=='candidate-002'
    for name in ['orchestration.json','candidates.jsonl','candidate-runs.jsonl','verifier-results.jsonl','selection.json','integration.json','transcript.md']:
        assert (od/name).exists()
    assert len((od/'candidates.jsonl').read_text().splitlines())==3
    assert json.loads((od/'task.json').read_text())['success_criteria']=='done criteria'



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
            dbg=kw['artifacts_dir']/'debug'; dbg.mkdir(parents=True); (dbg/'session_meta.json').write_text('{}')
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
            dbg=kw['artifacts_dir']/'debug'; dbg.mkdir(parents=True); (dbg/'session_meta.json').write_text('{}')
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


def test_verifier_parallel_passes_runner_resolved_nested_trace_dir(tmp_path):
    repo=tmp_path/'plain'; repo.mkdir(); (repo/'a.txt').write_text('original')
    seen=[]
    class NestedRunner:
        def run_task(self, **kw):
            (kw['repo_path']/'a.txt').write_text('changed')
            root=kw['artifacts_dir']/'villani_code_debug'; trace=root/'20260705T004006_753583Z'
            _touch(trace/'session_meta.json'); _touch(trace/'final_summary.json')
            return RunnerResult(exit_code=0, stdout='', stderr='', debug_artifact_dir=str(root), resolved_trace_dir=str(trace), duration_ms=1)
    def verifier(**kw):
        seen.append(kw['debug_dir'])
        assert (kw['debug_dir']/'session_meta.json').exists()
        return {'result':1,'verdict':'success','confidence':.9,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='x',candidates=1,workspace=tmp_path/'ws',backend='b')
    orch=VerifierParallelOrchestrator(cfg, runner=NestedRunner(), verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert seen and seen[0].name == '20260705T004006_753583Z'
    run_row=json.loads((od/'candidate-runs.jsonl').read_text().splitlines()[0])
    cand_row=json.loads((od/'candidates.jsonl').read_text().splitlines()[0])
    ver_row=json.loads((od/'verifier-results.jsonl').read_text().splitlines()[0])
    assert run_row['debugRoot'].endswith('villani_code_debug')
    assert run_row['debugDir'].endswith('20260705T004006_753583Z')
    assert cand_row['debugResolutionStatus'] == 'resolved'
    assert ver_row['debugDir'].endswith('20260705T004006_753583Z')
    assert 'villani_code_debug' in (od/'transcript.md').read_text()


def test_verifier_parallel_finds_nested_trace_without_runner_resolved_trace_dir(tmp_path):
    repo=tmp_path/'plain'; repo.mkdir(); (repo/'a.txt').write_text('original')
    seen=[]
    class NestedRunner:
        def run_task(self, **kw):
            (kw['repo_path']/'a.txt').write_text('changed')
            root=kw['artifacts_dir']/'villani_code_debug'; trace=root/'20260705T004006_753583Z'
            _touch(trace/'session_meta.json'); _touch(trace/'commands.jsonl')
            return RunnerResult(exit_code=0, stdout='', stderr='', debug_artifact_dir=str(root), duration_ms=1)
    def verifier(**kw):
        seen.append(kw['debug_dir'])
        return {'result':1,'verdict':'success','confidence':.9,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='x',candidates=1,workspace=tmp_path/'ws',backend='b')
    orch=VerifierParallelOrchestrator(cfg, runner=NestedRunner(), verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    orch.run()
    assert seen and seen[0].name == '20260705T004006_753583Z'


def test_verifier_parallel_invalid_debug_is_candidate_error_and_other_candidate_can_win(tmp_path):
    repo=tmp_path/'plain'; repo.mkdir(); (repo/'a.txt').write_text('original')
    class MixedDebugRunner:
        def run_task(self, **kw):
            if kw['context']['attempt_id']=='candidate-001':
                root=kw['artifacts_dir']/'villani_code_debug'; root.mkdir(parents=True)
                return RunnerResult(exit_code=0, stdout='', stderr='', debug_artifact_dir=str(root), duration_ms=1)
            (kw['repo_path']/'a.txt').write_text('winner')
            root=kw['artifacts_dir']/'villani_code_debug'; trace=root/'trace'
            _touch(trace/'session_meta.json')
            return RunnerResult(exit_code=0, stdout='', stderr='', debug_artifact_dir=str(root), resolved_trace_dir=str(trace), duration_ms=1)
    def verifier(**kw):
        return {'result':1,'verdict':'success','confidence':.9,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    cfg=VerifierParallelConfig(repo=repo,task='x',candidates=2,parallelism=1,seed=1,workspace=tmp_path/'ws',backend='b')
    orch=VerifierParallelOrchestrator(cfg, runner=MixedDebugRunner(), verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); od=Path(out['orchestrationDir'])
    assert out['winnerCandidateId']=='candidate-002'
    rows=[json.loads(line) for line in (od/'verifier-results.jsonl').read_text().splitlines()]
    c1=next(r for r in rows if r['candidateId']=='candidate-001')
    assert c1['verdict']=='error' and c1['result'] is None
    assert (repo/'a.txt').read_text()=='winner'


def test_selection_quality_tiebreak_beats_random_for_result_one():
    candidates = [
        {'candidateId':'candidate-001','result':1,'confidence':0.9,'riskFlags':['risk1','risk2'],'missingEvidence':['missing']},
        {'candidateId':'candidate-002','result':1,'confidence':0.9,'riskFlags':['risk1'],'missingEvidence':[],'successEvidence':['Implementation appears correct from source inspection']},
        {'candidateId':'candidate-003','result':1,'confidence':0.9,'riskFlags':['risk1'],'missingEvidence':[],'successEvidence':['Behavioral validation test passed','End-to-end runtime cleanup test passed'],'toolsUsed':[{'tool':'read_command','reason':'inspected validation'}]},
    ]
    s = select_winner(candidates, seed=7)
    assert s.winnerCandidateId == 'candidate-003'
    assert s.qualityTieBreakApplied is True
    assert s.tieBreak is False
    assert 'verifier quality tie-break' in s.reason


def test_selection_result_one_beats_cleaner_result_zero():
    s = select_winner([
        {'candidateId':'candidate-001','result':1,'riskFlags':['risk'],'confidence':0.5},
        {'candidateId':'candidate-002','result':0,'riskFlags':[],'confidence':0.99},
    ], seed=1)
    assert s.winnerCandidateId == 'candidate-001'
    assert s.winnerResult == 1


def test_selection_random_only_after_identical_quality_keys():
    candidates = [
        {'candidateId':'candidate-001','result':1,'confidence':0.9,'successEvidence':['test passed']},
        {'candidateId':'candidate-002','result':1,'confidence':0.9,'successEvidence':['test passed']},
    ]
    first = select_winner(candidates, seed=0)
    assert first.winnerCandidateId == select_winner(candidates, seed=0).winnerCandidateId
    assert first.winnerCandidateId != select_winner(candidates, seed=1).winnerCandidateId
    assert first.tieBreak is True
    assert first.qualityTieBreakApplied is True
    assert 'tied on verifier result and verifier quality key' in first.reason


def test_selection_missing_evidence_beats_more_behavioral_evidence():
    s = select_winner([
        {'candidateId':'candidate-001','result':1,'missingEvidence':[],'successEvidence':['source inspection suggests correct']},
        {'candidateId':'candidate-002','result':1,'missingEvidence':['no final runtime validation'],'successEvidence':['behavioral test passed','integration test passed']},
    ], seed=5)
    assert s.winnerCandidateId == 'candidate-001'


def test_selection_json_includes_quality_diagnostics(tmp_path):
    repo=tmp_path/'repo'; init_repo(repo); ws=tmp_path/'ws'
    runner=FakeRunner()
    def verifier(**kw):
        cid=kw['repo_dir'].parent.name
        return {
            'result':1,
            'verdict':'success',
            'confidence':0.9,
            'riskFlags':['risk'] if cid == 'candidate-001' else [],
            'successEvidence':['Behavioral validation test passed'] if cid == 'candidate-002' else ['source inspection suggests correct'],
            'recommendedAction':'accept',
            'traceDir':str(kw['trace_dir']),
        }
    cfg=VerifierParallelConfig(repo=repo,task='do it',candidates=2,parallelism=1,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier)
    orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x')
    out=orch.run(); od=Path(out['orchestrationDir'])
    selection=json.loads((od/'selection.json').read_text())
    assert selection['winnerCandidateId'] == 'candidate-002'
    assert selection['qualityTieBreakApplied'] is True
    assert selection['winnerQualityKey']['candidateId'] == 'candidate-002'
    assert len(selection['candidateQuality']) == 2
    assert 'verifier quality tie-break' in selection['reason']

def test_llm_comparison_called_for_multiple_successes_and_valid_id_wins(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; init_repo(repo); ws=tmp_path/'ws'; runner=FakeRunner(); calls=[]
    def verifier(**kw): return {'result':1,'verdict':'success','confidence':.5,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    def cmp(**kw):
        calls.append(kw); return {'selectedCandidateId':'candidate-002','reason':'better evidence'}
    monkeypatch.setattr('villani_ops.orchestrator.verifier_parallel.select_success_with_llm_comparison', cmp)
    cfg=VerifierParallelConfig(repo=repo,task='task',success_criteria='criteria',candidates=2,parallelism=1,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier); orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x',base_url='http://x')
    out=orch.run(); sel=json.loads((Path(out['orchestrationDir'])/'selection.json').read_text())
    assert calls and out['winnerCandidateId']=='candidate-002'
    assert sel['selectionPolicy']=='binary_verifier_llm_compare_tie'
    assert sel['llmComparison']['comparisonReason']=='better evidence'


def test_llm_comparison_invalid_id_falls_back(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; init_repo(repo); ws=tmp_path/'ws'; runner=FakeRunner()
    def verifier(**kw):
        cid=kw['repo_dir'].parent.name
        return {'result':1,'verdict':'success','confidence':.5,'recommendedAction':'accept','failureEvidence':['x'] if cid=='candidate-002' else [],'traceDir':str(kw['trace_dir'])}
    monkeypatch.setattr('villani_ops.orchestrator.verifier_parallel.select_success_with_llm_comparison', lambda **kw: {'selectedCandidateId':'bad','reason':'bad'})
    cfg=VerifierParallelConfig(repo=repo,task='task',candidates=2,parallelism=1,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier); orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x',base_url='http://x')
    out=orch.run(); sel=json.loads((Path(out['orchestrationDir'])/'selection.json').read_text())
    assert out['winnerCandidateId']=='candidate-001'
    assert sel['selectionPolicy']=='binary_verifier_llm_compare_tie_fallback_quality'
    assert sel['llmComparison']['fallbackUsed'] is True


def test_llm_comparison_exception_falls_back(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; init_repo(repo); ws=tmp_path/'ws'; runner=FakeRunner()
    def verifier(**kw): return {'result':1,'verdict':'success','confidence':.5,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    def boom(**kw): raise RuntimeError('timeout')
    monkeypatch.setattr('villani_ops.orchestrator.verifier_parallel.select_success_with_llm_comparison', boom)
    cfg=VerifierParallelConfig(repo=repo,task='task',candidates=2,parallelism=1,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier); orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x',base_url='http://x')
    out=orch.run(); sel=json.loads((Path(out['orchestrationDir'])/'selection.json').read_text())
    assert out['winnerCandidateId'] in {'candidate-001','candidate-002'}
    assert sel['llmComparison']['fallbackUsed'] is True
    assert 'timeout' in sel['llmComparison']['fallbackReason']


def test_llm_comparison_not_called_for_one_or_zero_successes(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; init_repo(repo); ws=tmp_path/'ws'; runner=FakeRunner(); calls=[]
    def verifier(**kw):
        return {'result':1 if kw['repo_dir'].parent.name=='candidate-001' else 0,'verdict':'success','confidence':.5,'recommendedAction':'accept','traceDir':str(kw['trace_dir'])}
    monkeypatch.setattr('villani_ops.orchestrator.verifier_parallel.select_success_with_llm_comparison', lambda **kw: calls.append(kw))
    cfg=VerifierParallelConfig(repo=repo,task='task',candidates=2,parallelism=1,seed=1,workspace=ws,backend='b',keep_worktrees=True)
    orch=VerifierParallelOrchestrator(cfg, runner=runner, verifier=verifier); orch._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x',base_url='http://x')
    out=orch.run()
    assert out['winnerCandidateId']=='candidate-001' and calls==[]

    repo2=tmp_path/'repo2'; init_repo(repo2); ws2=tmp_path/'ws2'; runner2=FakeRunner()
    def verifier0(**kw): return {'result':0,'verdict':'failure','confidence':.5,'recommendedAction':'reject','traceDir':str(kw['trace_dir'])}
    cfg2=VerifierParallelConfig(repo=repo2,task='task',candidates=2,parallelism=1,seed=1,workspace=ws2,backend='b',keep_worktrees=True,on_all_fail='fail')
    orch2=VerifierParallelOrchestrator(cfg2, runner=runner2, verifier=verifier0); orch2._backend_obj=lambda: Backend(name='b',provider='local',model='m',api_key='x',base_url='http://x')
    out2=orch2.run()
    assert out2['winnerCandidateId'] is None and calls==[]
