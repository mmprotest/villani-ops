from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

from villani_ops.materialize import materialize_latest


def init_repo(repo: Path, text: str = 'original\n') -> None:
    repo.mkdir(parents=True)
    subprocess.run(['git', 'init'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'a@b.c'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'T'], cwd=repo, check=True)
    (repo / 'file.txt').write_text(text)
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo, check=True, capture_output=True)


def make_patch(repo: Path, value: str, patch: Path) -> None:
    (repo / 'file.txt').write_text(value)
    diff = subprocess.run(['git', 'diff', '--', 'file.txt'], cwd=repo, check=True, text=True, capture_output=True).stdout
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(diff)
    subprocess.run(['git', 'checkout', '--', 'file.txt'], cwd=repo, check=True)


def write_materialization(ws: Path, repo: Path, oid: str, source: str, winner: str = 'candidate-002', value: str = 'winner\n', created: str = '2026-07-05T00:00:00+00:00') -> Path:
    odir = ws / 'orchestrations' / oid
    patch = odir / 'candidates' / winner / 'diff.patch'
    make_patch(repo, value, patch)
    mat = {
        'schemaVersion': 'villani-ops-materializable-selection-v1',
        'source': source,
        'orchestrationId': oid,
        'orchestrationDir': str(odir),
        'winnerCandidateId': winner,
        'winnerResult': 1,
        'winnerVerdict': 'success',
        'winnerConfidence': 0.9,
        'targetRepo': str(repo),
        'patchPath': str(patch),
        'selectionPath': str(odir / 'selection.json'),
        'integrationPath': str(odir / 'integration.json'),
        'createdAt': created,
        'status': 'selected',
    }
    odir.mkdir(parents=True, exist_ok=True)
    (odir / 'materialization.json').write_text(json.dumps(mat, indent=2))
    (odir / 'selection.json').write_text(json.dumps({'winnerCandidateId': winner, 'winnerResult': 1, 'winnerPatchPath': str(patch)}))
    (odir / 'integration.json').write_text(json.dumps({'status': 'integrated', 'winnerCandidateId': winner, 'patchPath': str(patch), 'targetRepo': str(repo)}))
    return odir


def test_discovers_verifier_parallel_materialization_json(tmp_path):
    repo = tmp_path / 'repo'; ws = tmp_path / 'workspace'; init_repo(repo)
    write_materialization(ws, repo, 'orch-1', 'verifier-parallel')
    result = materialize_latest(ws, repo, policy='accepted')
    assert result.status == 'applied'
    assert result.source == 'verifier-parallel'
    assert result.winner_candidate_id == 'candidate-002'
    assert (repo / 'file.txt').read_text() == 'winner\n'


def test_discovers_verifier_sequential_materialization_json(tmp_path):
    repo = tmp_path / 'repo'; ws = tmp_path / 'workspace'; init_repo(repo)
    write_materialization(ws, repo, 'orch-1', 'verifier-sequential')
    result = materialize_latest(ws, repo, policy='accepted')
    assert result.status == 'applied'
    assert result.source == 'verifier-sequential'
    assert (repo / 'file.txt').read_text() == 'winner\n'


def test_wrapper_script_materialize_delegates_to_canonical_materializer(tmp_path):
    repo = tmp_path / 'repo'; ws = tmp_path / 'workspace'; init_repo(repo)
    write_materialization(ws, repo, 'orch-1', 'verifier-parallel')
    script = Path(__file__).parents[2] / 'scripts' / 'apply_villani_ops_result.py'
    env = os.environ.copy(); env['PYTHONPATH'] = str(Path(__file__).parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
    proc = subprocess.run([sys.executable, str(script), 'materialize', '--workspace', str(ws), '--repo', str(repo), '--policy', 'accepted'], text=True, capture_output=True, env=env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert 'found verifier orchestration' in proc.stdout
    assert (repo / 'file.txt').read_text() == 'winner\n'


def test_restore_then_materialize_reapplies_selected_patch(tmp_path):
    repo = tmp_path / 'repo'; ws = tmp_path / 'workspace'; init_repo(repo)
    write_materialization(ws, repo, 'orch-1', 'verifier-sequential')
    (repo / 'file.txt').write_text('winner\n')
    subprocess.run(['git', 'checkout', '--', 'file.txt'], cwd=repo, check=True)
    assert (repo / 'file.txt').read_text() == 'original\n'
    assert materialize_latest(ws, repo).applied
    assert (repo / 'file.txt').read_text() == 'winner\n'


def test_missing_patch_fails_clearly_and_wrapper_exits_nonzero(tmp_path):
    repo = tmp_path / 'repo'; ws = tmp_path / 'workspace'; init_repo(repo)
    odir = write_materialization(ws, repo, 'orch-1', 'verifier-parallel')
    patch = odir / 'candidates' / 'candidate-002' / 'diff.patch'
    patch.unlink()
    result = materialize_latest(ws, repo)
    assert result.status == 'failed'
    assert 'selected patch does not exist' in (result.error or '')
    script = Path(__file__).parents[2] / 'scripts' / 'apply_villani_ops_result.py'
    env = os.environ.copy(); env['PYTHONPATH'] = str(Path(__file__).parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
    proc = subprocess.run([sys.executable, str(script), 'materialize', '--workspace', str(ws), '--repo', str(repo), '--policy', 'accepted'], text=True, capture_output=True, env=env)
    assert proc.returncode == 1
    assert 'selected patch does not exist' in proc.stderr + proc.stdout


def test_no_materializable_result_preserves_noop_success(tmp_path):
    repo = tmp_path / 'repo'; ws = tmp_path / 'workspace'; init_repo(repo); ws.mkdir()
    result = materialize_latest(ws, repo)
    assert result.status == 'no-op'
    script = Path(__file__).parents[2] / 'scripts' / 'apply_villani_ops_result.py'
    env = os.environ.copy(); env['PYTHONPATH'] = str(Path(__file__).parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
    proc = subprocess.run([sys.executable, str(script), 'materialize', '--workspace', str(ws), '--repo', str(repo), '--policy', 'accepted'], text=True, capture_output=True, env=env)
    assert proc.returncode == 0


def test_newest_verifier_orchestration_wins(tmp_path):
    repo = tmp_path / 'repo'; ws = tmp_path / 'workspace'; init_repo(repo)
    write_materialization(ws, repo, 'old', 'verifier-parallel', value='old\n', created='2026-07-05T00:00:00+00:00')
    write_materialization(ws, repo, 'new', 'verifier-sequential', value='new\n', created='2026-07-05T01:00:00+00:00')
    result = materialize_latest(ws, repo)
    assert result.selected_path == ws / 'orchestrations' / 'new'
    assert (repo / 'file.txt').read_text() == 'new\n'


def test_fallback_discovery_from_selection_and_integration(tmp_path):
    repo = tmp_path / 'repo'; ws = tmp_path / 'workspace'; init_repo(repo)
    odir = write_materialization(ws, repo, 'orch-1', 'verifier-parallel')
    (odir / 'materialization.json').unlink()
    result = materialize_latest(ws, repo)
    assert result.applied
    assert (repo / 'file.txt').read_text() == 'winner\n'


def test_materializer_code_does_not_reference_reward_or_ground_truth():
    text = (Path(__file__).parents[1] / 'materialize.py').read_text()
    assert 'reward.txt' not in text
    assert 'ground truth' not in text.lower()
    assert 'result.json' not in text
