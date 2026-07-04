from __future__ import annotations
import json
from pathlib import Path
from typer.testing import CliRunner
from villani_ops.cli.main import app
from villani_ops.orchestrator.verifier_parallel import VerifierParallelConfig, VerifierParallelOrchestrator
from villani_ops.materialize import discover_latest_verifier_parallel_state, wrapper_exit_code

runner = CliRunner()


def _ok_run(orchestration_dir='o'):
    return {'schemaVersion':'villani-ops-verifier-parallel-output-v1','status':'completed','winnerCandidateId':'candidate-001','winnerResult':1,'selectionPath':'s','integrationPath':'i','orchestrationDir':orchestration_dir,'candidates':[]}


def test_run_accepts_verifier_parallel_and_maps_observed_command(monkeypatch, tmp_path):
    captured=[]
    def fake_run(self):
        captured.append(self.config)
        return _ok_run(str(tmp_path/'ws'/'orchestrations'/'o'))
    monkeypatch.setattr(VerifierParallelOrchestrator, 'run', fake_run)
    res=runner.invoke(app,[
        'run','--repo','/app','--task','dummy task','--mode','performance','--runner','villani-code',
        '--orchestrator','verifier-parallel','--candidate-attempts','4','--timeout-seconds','1500',
        '--non-interactive','--no-ui','--workspace',str(tmp_path/'workspace')
    ], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert 'Invalid orchestrator' not in res.output
    cfg=captured[0]
    assert cfg.repo == Path('/app')
    assert cfg.task == 'dummy task'
    assert cfg.agent == 'villani-code'
    assert cfg.candidates == 4
    assert cfg.candidate_timeout_seconds == 1500
    assert cfg.workspace == tmp_path/'workspace'


def test_existing_orchestrator_values_still_validate(monkeypatch, tmp_path):
    import villani_ops.cli.main as main
    class FakeDecision:
        accepted=True; mode='performance'; performance_backend_name=None; performance_backend_model=None
        decomposition_executed=False; candidate_attempts_requested=1; candidate_attempts_completed=1
        winning_attempt_id='attempt_001'; reason='ok'
    class FakeOpsRunner:
        def __init__(self,*a,**k): pass
        def run(self, req): return type('R', (), {'decision':FakeDecision(), 'run_dir':str(tmp_path/'run'), 'state':None})()
    class FakeGraph:
        def __init__(self,*a,**k): pass
        def run(self, **kw): return type('R', (), {'decision':FakeDecision(), 'run_dir':str(tmp_path/'run'), 'state':None})()
    monkeypatch.setattr('villani_ops.agentic.OpsRunner', FakeOpsRunner)
    monkeypatch.setattr(main, 'VillaniOps', FakeGraph)
    for orch in ['adaptive','agentic','graph']:
        res=runner.invoke(app,['run','--repo',str(tmp_path),'--task','x','--orchestrator',orch], catch_exceptions=False)
        assert res.exit_code == 0, (orch, res.output)


def test_unknown_orchestrator_lists_verifier_parallel(tmp_path):
    res=runner.invoke(app,['run','--repo',str(tmp_path),'--task','x','--orchestrator','nope'])
    assert res.exit_code != 0
    assert 'verifier-parallel' in res.output


def test_verifier_parallel_dispatch_skips_legacy_paths(monkeypatch, tmp_path):
    calls=[]
    def fake_run(self):
        calls.append(self.config)
        return _ok_run()
    monkeypatch.setattr(VerifierParallelOrchestrator, 'run', fake_run)
    monkeypatch.setattr('villani_ops.agentic.OpsRunner', lambda *a, **k: (_ for _ in ()).throw(AssertionError('agentic touched')))
    import villani_ops.cli.main as main
    monkeypatch.setattr(main, 'VillaniOps', lambda *a, **k: (_ for _ in ()).throw(AssertionError('graph touched')))
    res=runner.invoke(app,['run','--repo',str(tmp_path),'--task','x','--orchestrator','verifier-parallel'], catch_exceptions=False)
    assert res.exit_code == 0
    assert len(calls) == 1
    assert calls[0].candidates == 5


def test_task_file_backend_and_verifier_backend_mapping(monkeypatch, tmp_path):
    task_file=tmp_path/'task.txt'; task_file.write_text('file task')
    captured=[]
    monkeypatch.setattr(VerifierParallelOrchestrator, 'run', lambda self: captured.append(self.config) or _ok_run())
    res=runner.invoke(app,['run','--repo',str(tmp_path),'--task-file',str(task_file),'--orchestrator','verifier-parallel','--backend','b','--verifier-backend','vb'], catch_exceptions=False)
    assert res.exit_code == 0
    cfg=captured[0]
    assert cfg.task == 'file task'
    assert cfg.backend == 'b'
    assert cfg.verifier_backend == 'vb'


def test_materialization_discovers_verifier_parallel_artifacts(tmp_path):
    od=tmp_path/'ws'/'orchestrations'/'orch-1'; od.mkdir(parents=True)
    (od/'selection.json').write_text(json.dumps({'winnerCandidateId':'candidate-001'}))
    (od/'integration.json').write_text(json.dumps({'status':'integrated','winnerCandidateId':'candidate-001'}))
    (od/'orchestration.json').write_text(json.dumps({'status':'completed','winnerCandidateId':'candidate-001'}))
    state=discover_latest_verifier_parallel_state(tmp_path/'ws')
    assert state is not None
    assert state.orchestration_dir == od
    assert state.winner_candidate_id == 'candidate-001'
    assert state.integration_succeeded
    assert state.accepted_result


def test_wrapper_exit_code_does_not_swallow_startup_failure_without_acceptance(tmp_path):
    assert wrapper_exit_code(2, tmp_path/'missing') == 2
    od=tmp_path/'ws'/'orchestrations'/'orch-1'; od.mkdir(parents=True)
    (od/'selection.json').write_text(json.dumps({'winnerCandidateId':None}))
    (od/'integration.json').write_text(json.dumps({'status':'skipped'}))
    assert wrapper_exit_code(2, tmp_path/'ws') == 2
    assert wrapper_exit_code(0, tmp_path/'ws') == 0


def test_wrapper_exit_code_allows_failed_process_only_after_accepted_materialization(tmp_path):
    od=tmp_path/'ws'/'orchestrations'/'orch-1'; od.mkdir(parents=True)
    (od/'selection.json').write_text(json.dumps({'winnerCandidateId':'candidate-001'}))
    (od/'integration.json').write_text(json.dumps({'status':'integrated'}))
    assert wrapper_exit_code(2, tmp_path/'ws') == 0
