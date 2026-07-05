from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
import json, subprocess
from typing import Any


MATERIALIZABLE_STATUSES = {'selected', 'accepted', 'integrated', 'materializable'}


@dataclass
class VerifierParallelRunState:
    orchestration_dir: Path
    selection: dict[str, Any]
    integration: dict[str, Any]
    orchestration: dict[str, Any]

    @property
    def winner_candidate_id(self) -> str | None:
        return self.selection.get('winnerCandidateId') or self.orchestration.get('winnerCandidateId')

    @property
    def integration_succeeded(self) -> bool:
        return self.integration.get('status') == 'integrated'

    @property
    def accepted_result(self) -> bool:
        return bool(self.winner_candidate_id and self.integration_succeeded)


@dataclass
class MaterializableSelection:
    source: str
    created_at: datetime | None
    completed_at: datetime | None
    workspace_path: Path
    target_repo: Path | None
    patch_path: Path
    winner_candidate_id: str | None
    winner_result: int | None
    fallback_winner: bool
    metadata: dict[str, Any]


@dataclass
class MaterializationResult:
    status: str
    source: str | None
    workspace: Path
    repo: Path
    selected_path: Path | None
    patch_path: Path | None
    winner_candidate_id: str | None
    winner_result: int | None
    message: str
    changed_files: list[str]
    error: str | None = None
    selection: MaterializableSelection | None = None
    applied: bool = False
    artifact: dict[str, Any] | None = None
    logs: list[str] | None = None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _mtime_dt(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except Exception:
        return None


def _resolve_path(value: Any, base: Path) -> Path | None:
    if not value:
        return None
    p = Path(str(value))
    return p if p.is_absolute() else base / p


def _candidate_patch_from_records(odir: Path, candidate_id: str | None) -> Path | None:
    if not candidate_id:
        return None
    records = odir / 'candidates.jsonl'
    if records.exists():
        for line in records.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get('candidateId') == candidate_id and row.get('patchPath'):
                p = _resolve_path(row.get('patchPath'), odir)
                if p:
                    return p
    return odir / 'candidates' / candidate_id / 'diff.patch'


def _selection_sort_key(selection: MaterializableSelection) -> datetime:
    return selection.completed_at or selection.created_at or _mtime_dt(selection.workspace_path / 'materialization.json') or _mtime_dt(selection.workspace_path) or datetime.min.replace(tzinfo=timezone.utc)


def discover_latest_verifier_parallel_state(workspace: str | Path) -> VerifierParallelRunState | None:
    root = Path(workspace) / 'orchestrations'
    if not root.exists():
        return None
    dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
    for odir in dirs:
        if (odir / 'selection.json').exists() and (odir / 'integration.json').exists():
            return VerifierParallelRunState(odir, _read_json(odir / 'selection.json'), _read_json(odir / 'integration.json'), _read_json(odir / 'orchestration.json'))
    return None


def selection_from_orchestration_dir(odir: Path) -> MaterializableSelection | None:
    mat_path = odir / 'materialization.json'
    mat = _read_json(mat_path) if mat_path.exists() else {}
    sel = _read_json(odir / 'selection.json')
    integ = _read_json(odir / 'integration.json')
    orch = _read_json(odir / 'orchestration.json')
    if mat_path.exists() and mat.get('status') not in MATERIALIZABLE_STATUSES:
        return None
    candidate_id = mat.get('winnerCandidateId') or sel.get('winnerCandidateId') or integ.get('winnerCandidateId') or orch.get('winnerCandidateId')
    if not candidate_id:
        return None
    result = mat.get('winnerResult') if 'winnerResult' in mat else sel.get('winnerResult')
    fallback = bool(mat.get('fallbackWinner') or sel.get('fallback') or sel.get('fallbackWinner'))
    if result not in (1, '1', None) and not fallback:
        return None
    patch = (_resolve_path(mat.get('patchPath'), odir) or _resolve_path(sel.get('winnerPatchPath'), odir) or _resolve_path(integ.get('patchPath'), odir) or _candidate_patch_from_records(odir, candidate_id))
    if not patch:
        return None
    source = mat.get('source') or orch.get('mode') or 'verifier-orchestration'
    if source not in {'verifier-parallel', 'verifier-sequential'} and not str(source).startswith('verifier'):
        source = 'verifier-orchestration'
    target = _resolve_path(mat.get('targetRepo') or integ.get('targetRepo') or orch.get('repo'), odir)
    meta = {'materialization': mat, 'selection': sel, 'integration': integ, 'orchestration': orch, 'orchestrationDir': str(odir), 'materializationPath': str(mat_path) if mat_path.exists() else None}
    created = _parse_dt(mat.get('createdAt') or orch.get('createdAt')) or _mtime_dt(mat_path if mat_path.exists() else odir)
    completed = _parse_dt(mat.get('completedAt') or orch.get('completedAt') or integ.get('completedAt'))
    return MaterializableSelection(str(source), created, completed, odir, target, patch, candidate_id, int(result) if str(result).isdigit() else result, fallback, meta)


def discover_verifier_orchestration_materializations(workspace: str | Path) -> list[MaterializableSelection]:
    root = Path(workspace) / 'orchestrations'
    if not root.exists():
        return []
    return [s for odir in root.iterdir() if odir.is_dir() for s in [selection_from_orchestration_dir(odir)] if s]


def discover_legacy_materializations(workspace: str | Path) -> list[MaterializableSelection]:
    root = Path(workspace) / 'runs'
    out: list[MaterializableSelection] = []
    if not root.exists():
        return out
    for rdir in root.iterdir():
        if not rdir.is_dir() or not (rdir / 'decision.json').exists() or not (rdir / 'task.json').exists():
            continue
        dec = _read_json(rdir / 'decision.json')
        if not dec.get('accepted') or not dec.get('winning_patch_path'):
            continue
        task = _read_json(rdir / 'task.json')
        patch = _resolve_path(dec.get('winning_patch_path'), rdir)
        repo = _resolve_path(task.get('repo_path'), rdir)
        if patch:
            out.append(MaterializableSelection('legacy-run', _mtime_dt(rdir / 'decision.json'), None, rdir, repo, patch, dec.get('winning_attempt_id'), 1, False, {'decision': dec, 'task': task}))
    return out


def discover_materializable_selections(workspace: str | Path) -> list[MaterializableSelection]:
    return discover_legacy_materializations(workspace) + discover_verifier_orchestration_materializations(workspace)


def discover_materializable_selection(workspace: Path, *, policy: str = 'accepted') -> MaterializableSelection | None:
    sels = discover_materializable_selections(workspace)
    return max(sels, key=_selection_sort_key) if sels else None


def newest_materializable_selection(workspace: str | Path) -> MaterializableSelection | None:
    return discover_materializable_selection(Path(workspace))


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['git', *args], cwd=repo, text=True, capture_output=True)


def _changed_files_from_patch(patch: Path) -> list[str]:
    proc = subprocess.run(['git', 'diff', '--name-only', '--no-index', '/dev/null', str(patch)], text=True, capture_output=True)
    # For patch files, parse headers instead of depending on diff semantics.
    files: list[str] = []
    for line in patch.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('+++ b/'):
            files.append(line[6:])
        elif line.startswith('+++ ') and not line.startswith('+++ /dev/null'):
            files.append(line[4:])
    return sorted(set(files))


def _apply_patch(repo: Path, patch: Path) -> dict[str, Any]:
    if not repo.exists():
        raise FileNotFoundError(f'target repo does not exist: {repo}')
    if not patch.exists():
        raise FileNotFoundError(f'selected patch does not exist: {patch}')
    text = patch.read_text(encoding='utf-8', errors='replace')
    if not text.strip():
        return {'attempted': True, 'exit_code': 0, 'empty_patch': True, 'stdout': '', 'stderr': '', 'changed_files': []}
    chk = _git(repo, ['apply', '--check', str(patch)])
    if chk.returncode != 0:
        raise RuntimeError('git apply --check failed: ' + (chk.stderr.strip() or chk.stdout.strip()))
    ap = _git(repo, ['apply', str(patch)])
    if ap.returncode != 0:
        raise RuntimeError('git apply failed: ' + (ap.stderr.strip() or ap.stdout.strip()))
    return {'attempted': True, 'exit_code': 0, 'empty_patch': False, 'stdout': ap.stdout, 'stderr': ap.stderr, 'changed_files': _changed_files_from_patch(patch)}


def materialize_latest(workspace: str | Path, repo: str | Path | None = None, *, policy: str = 'accepted') -> MaterializationResult:
    workspace = Path(workspace)
    logs = [f'[materialize] scanning workspace: {workspace}']
    selection = discover_materializable_selection(workspace, policy=policy)
    logs.append(f'[materialize] newest run path: {selection.workspace_path if selection else None}')
    if not selection:
        logs.append('[materialize] no-op: no run state found')
        for line in logs: print(line)
        return MaterializationResult('no-op', None, workspace, Path(repo or ''), None, None, None, None, 'no run state found', [], selection=None, applied=False, logs=logs)
    target_repo = Path(repo) if repo is not None else selection.target_repo
    if target_repo is None:
        err = 'target repo was not supplied and selection did not record one'
        logs.append(f'[materialize] failed: {err}')
        for line in logs: print(line)
        return MaterializationResult('failed', selection.source, workspace, Path(''), selection.workspace_path, selection.patch_path, selection.winner_candidate_id, selection.winner_result, err, [], err, selection, False, None, logs)
    if selection.source.startswith('verifier'):
        logs += [f'[materialize] found verifier orchestration: {selection.workspace_path.name}', f'[materialize] source: {selection.source}', f'[materialize] winner candidate: {selection.winner_candidate_id}', f'[materialize] patch: {selection.patch_path}']
    try:
        artifact = _apply_patch(Path(target_repo), selection.patch_path)
        changed = artifact.get('changed_files') or []
        msg = 'empty selected patch; materialized no-op' if artifact.get('empty_patch') else 'applied selected patch'
        if selection.source.startswith('verifier'):
            logs.append('[materialize] applied verifier orchestration patch' if not artifact.get('empty_patch') else '[materialize] verifier orchestration patch was empty; no changes applied')
        for line in logs: print(line)
        return MaterializationResult('applied', selection.source, workspace, Path(target_repo), selection.workspace_path, selection.patch_path, selection.winner_candidate_id, selection.winner_result, msg, changed, None, selection, True, artifact, logs)
    except Exception as e:
        err = str(e)
        logs.append(f'[materialize] failed: {err}')
        for line in logs: print(line)
        return MaterializationResult('failed', selection.source, workspace, Path(target_repo), selection.workspace_path, selection.patch_path, selection.winner_candidate_id, selection.winner_result, 'materialization failed', [], err, selection, False, None, logs)


def materialization_succeeded_with_accepted_result(workspace: str | Path) -> bool:
    state = discover_latest_verifier_parallel_state(workspace)
    return bool(state and state.accepted_result)


def wrapper_exit_code(villani_ops_exit_code: int, workspace: str | Path) -> int:
    if villani_ops_exit_code == 0:
        return 0
    if materialization_succeeded_with_accepted_result(workspace):
        return 0
    return villani_ops_exit_code
