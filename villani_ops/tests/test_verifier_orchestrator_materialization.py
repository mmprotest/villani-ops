from __future__ import annotations
import json, subprocess
from pathlib import Path

from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerResult
from villani_ops.materialize import materialize_latest
from villani_ops.orchestrator.verifier_parallel import VerifierParallelConfig, VerifierParallelOrchestrator
from villani_ops.orchestrator.verifier_sequential import VerifierSequentialConfig, VerifierSequentialOrchestrator


def init_repo(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git','init'], cwd=p, check=True, capture_output=True)
    subprocess.run(['git','config','user.email','a@b.c'], cwd=p, check=True)
    subprocess.run(['git','config','user.name','T'], cwd=p, check=True)
    (p/'a.txt').write_text('original')
    subprocess.run(['git','add','.'], cwd=p, check=True)
    subprocess.run(['git','commit','-m','init'], cwd=p, check=True, capture_output=True)


class FakeRunner:
    def run_task(self, **kw):
        cid = kw['context']['attempt_id']
        (Path(kw['repo_path'])/'a.txt').write_text(cid)
        debug = kw['artifacts_dir']/'villani_code_debug'/'trace'
        debug.mkdir(parents=True)
        (debug/'session_meta.json').write_text('{}')
        return RunnerResult(exit_code=0, stdout='', stderr='', debug_artifact_dir=str(debug.parent), resolved_trace_dir=str(debug), duration_ms=1)


def _verifier(results):
    def verifier(**kw):
        cid = kw['repo_dir'].parent.name
        result = results.get(cid, 0)
        return {'result':result,'verdict':'success' if result == 1 else 'failure','confidence':0.9,'recommendedAction':'accept' if result == 1 else 'reject','traceDir':str(kw['trace_dir'])}
    return verifier


def _orch(tmp_path, mode='sequential', results=None, keep_worktrees=True, on_all_fail='fail'):
    repo = tmp_path/'repo'; init_repo(repo); ws = tmp_path/'ws'
    cls, cfgcls = (VerifierSequentialOrchestrator, VerifierSequentialConfig) if mode == 'sequential' else (VerifierParallelOrchestrator, VerifierParallelConfig)
    kwargs = {'repo':repo,'task':'change file','candidates':2,'seed':1,'workspace':ws,'backend':'b','keep_worktrees':keep_worktrees,'on_all_fail':on_all_fail}
    if mode == 'parallel': kwargs['parallelism'] = 1
    orch = cls(cfgcls(**kwargs), runner=FakeRunner(), verifier=_verifier(results or {'candidate-001':1}))
    orch._backend_obj = lambda: Backend(name='b', provider='local', model='m', api_key='x')
    return orch, repo, ws


def test_sequential_writes_materialization_artifact(tmp_path):
    orch, repo, ws = _orch(tmp_path, 'sequential')
    out = orch.run(); od = Path(out['orchestrationDir']); mat = json.loads((od/'materialization.json').read_text())
    assert mat['source'] == 'verifier-sequential'
    assert mat['winnerCandidateId'] == 'candidate-001'
    assert mat['winnerResult'] == 1
    assert Path(mat['patchPath']).exists()
    assert out['materializationPath'] == str(od/'materialization.json')


def test_parallel_writes_materialization_artifact(tmp_path):
    orch, repo, ws = _orch(tmp_path, 'parallel')
    out = orch.run(); od = Path(out['orchestrationDir']); mat = json.loads((od/'materialization.json').read_text())
    assert mat['source'] == 'verifier-parallel'
    assert mat['winnerCandidateId'] == 'candidate-001'
    assert mat['winnerResult'] == 1
    assert Path(mat['patchPath']).exists()


def test_materializer_discovers_sequential_after_restore(tmp_path):
    orch, repo, ws = _orch(tmp_path, 'sequential')
    out = orch.run()
    assert (repo/'a.txt').read_text() == 'candidate-001'
    subprocess.run(['git','checkout','--','a.txt'], cwd=repo, check=True)
    assert (repo/'a.txt').read_text() == 'original'
    result = materialize_latest(ws)
    assert result.applied
    assert (repo/'a.txt').read_text() == 'candidate-001'
    assert any('found verifier orchestration' in line for line in (result.logs or []))


def test_materializer_discovers_parallel_after_restore(tmp_path):
    orch, repo, ws = _orch(tmp_path, 'parallel')
    orch.run(); subprocess.run(['git','checkout','--','a.txt'], cwd=repo, check=True)
    result = materialize_latest(ws)
    assert result.applied
    assert (repo/'a.txt').read_text() == 'candidate-001'


def test_worktree_cleanup_preserves_selected_patch_and_materializes(tmp_path):
    orch, repo, ws = _orch(tmp_path, 'sequential', keep_worktrees=False)
    out = orch.run(); od = Path(out['orchestrationDir']); cdir = od/'candidates'/'candidate-001'
    assert not (cdir/'worktree').exists()
    assert (cdir/'diff.patch').exists()
    assert (od/'materialization.json').exists()
    subprocess.run(['git','checkout','--','a.txt'], cwd=repo, check=True)
    assert materialize_latest(ws).applied


def test_no_winner_records_no_winner_and_does_not_materialize(tmp_path):
    orch, repo, ws = _orch(tmp_path, 'sequential', results={'candidate-001':0,'candidate-002':0}, on_all_fail='fail')
    out = orch.run(); od = Path(out['orchestrationDir']); mat = json.loads((od/'materialization.json').read_text())
    assert out['winnerCandidateId'] is None
    assert mat['status'] == 'no_winner'
    assert not materialize_latest(ws).applied


def test_fallback_winner_is_materializable(tmp_path):
    orch, repo, ws = _orch(tmp_path, 'sequential', results={'candidate-001':0,'candidate-002':0}, on_all_fail='best-confidence')
    out = orch.run(); od = Path(out['orchestrationDir']); mat = json.loads((od/'materialization.json').read_text())
    assert mat['fallbackWinner'] is True
    assert mat['winnerResult'] == 0
    subprocess.run(['git','checkout','--','a.txt'], cwd=repo, check=True)
    assert materialize_latest(ws).applied


def test_trace_fields_are_distinct(tmp_path):
    orch, repo, ws = _orch(tmp_path, 'sequential')
    out = orch.run(); od = Path(out['orchestrationDir'])
    sel = json.loads((od/'selection.json').read_text())
    ver = json.loads((od/'verifier-results.jsonl').read_text().splitlines()[0])
    assert sel['candidateDebugDir'].endswith('villani_code_debug/trace')
    assert sel['verifierTraceDir'].endswith('candidate-001/verifier/trace')
    assert sel['traceDir'] == sel['verifierTraceDir']
    assert ver['traceDir'] == ver['verifierTraceDir']
