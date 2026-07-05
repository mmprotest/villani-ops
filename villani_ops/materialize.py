from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
import json
from typing import Any

from villani_ops.git_ops import safe_apply


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
    workspace_path: Path
    target_repo: Path
    patch_path: Path
    winner_candidate_id: str | None
    winner_result: int | None
    fallback_winner: bool
    metadata: dict[str, Any]


@dataclass
class MaterializationResult:
    selection: MaterializableSelection | None
    status: str
    applied: bool = False
    artifact: dict[str, Any] | None = None
    logs: list[str] | None = None
    error: str | None = None


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
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _mtime_dt(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except Exception:
        return None


def discover_latest_verifier_parallel_state(workspace: str | Path) -> VerifierParallelRunState | None:
    """Find the newest verifier orchestration with selection/integration artifacts."""
    root = Path(workspace) / 'orchestrations'
    if not root.exists():
        return None
    dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
    for odir in dirs:
        selection_path = odir / 'selection.json'
        integration_path = odir / 'integration.json'
        orchestration_path = odir / 'orchestration.json'
        if selection_path.exists() and integration_path.exists():
            return VerifierParallelRunState(
                orchestration_dir=odir,
                selection=_read_json(selection_path),
                integration=_read_json(integration_path),
                orchestration=_read_json(orchestration_path),
            )
    return None


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
                return Path(row['patchPath'])
    fallback = odir / 'candidates' / candidate_id / 'diff.patch'
    return fallback if fallback.exists() else None


def selection_from_orchestration_dir(odir: Path) -> MaterializableSelection | None:
    mat_path = odir / 'materialization.json'
    mat = _read_json(mat_path) if mat_path.exists() else {}
    sel = _read_json(odir / 'selection.json')
    integ = _read_json(odir / 'integration.json')
    orch = _read_json(odir / 'orchestration.json')
    if mat and mat.get('status') == 'no_winner':
        return None
    candidate_id = mat.get('winnerCandidateId') or sel.get('winnerCandidateId') or integ.get('winnerCandidateId') or orch.get('winnerCandidateId')
    result = mat.get('winnerResult') if 'winnerResult' in mat else sel.get('winnerResult')
    fallback = bool(mat.get('fallbackWinner') or sel.get('fallback') or sel.get('fallbackWinner'))
    if not candidate_id:
        return None
    if result != 1 and not fallback:
        return None
    patch = Path(mat.get('patchPath') or sel.get('winnerPatchPath') or integ.get('patchPath') or '') if (mat.get('patchPath') or sel.get('winnerPatchPath') or integ.get('patchPath')) else _candidate_patch_from_records(odir, candidate_id)
    target = Path(mat.get('targetRepo') or integ.get('targetRepo') or orch.get('repo') or '')
    if not patch or not patch.exists() or not target.exists():
        return None
    source = mat.get('source') or orch.get('mode') or 'verifier-orchestration'
    created = _parse_dt(mat.get('createdAt') or orch.get('completedAt') or orch.get('createdAt')) or _mtime_dt(mat_path if mat_path.exists() else odir)
    meta = {'materialization': mat, 'selection': sel, 'integration': integ, 'orchestration': orch, 'orchestrationDir': str(odir), 'materializationPath': str(mat_path) if mat_path.exists() else None}
    return MaterializableSelection(source=source, created_at=created, workspace_path=odir, target_repo=target, patch_path=patch, winner_candidate_id=candidate_id, winner_result=result, fallback_winner=fallback, metadata=meta)


def discover_verifier_orchestration_materializations(workspace: str | Path) -> list[MaterializableSelection]:
    root = Path(workspace) / 'orchestrations'
    if not root.exists():
        return []
    selections: list[MaterializableSelection] = []
    for odir in root.iterdir():
        if not odir.is_dir():
            continue
        sel = selection_from_orchestration_dir(odir)
        if sel:
            selections.append(sel)
    return selections


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
        patch = Path(dec['winning_patch_path']); task = _read_json(rdir / 'task.json'); repo = Path(task.get('repo_path') or '')
        if patch.exists() and repo.exists():
            out.append(MaterializableSelection('legacy-run', _mtime_dt(rdir / 'decision.json'), rdir, repo, patch, dec.get('winning_attempt_id'), 1, False, {'decision': dec, 'task': task}))
    return out


def discover_materializable_selections(workspace: str | Path) -> list[MaterializableSelection]:
    return discover_legacy_materializations(workspace) + discover_verifier_orchestration_materializations(workspace)


def newest_materializable_selection(workspace: str | Path) -> MaterializableSelection | None:
    sels = discover_materializable_selections(workspace)
    return max(sels, key=lambda s: s.created_at or _mtime_dt(s.workspace_path) or datetime.min.replace(tzinfo=timezone.utc)) if sels else None


def materialize_selection(selection: MaterializableSelection) -> MaterializationResult:
    logs: list[str] = []
    if selection.source.startswith('verifier'):
        oid = selection.metadata.get('orchestration', {}).get('orchestrationId') or selection.workspace_path.name
        logs += [f'[materialize] found verifier orchestration: {oid}', f'[materialize] winner candidate: {selection.winner_candidate_id}', f'[materialize] patch: {selection.patch_path}']
    try:
        # safe_apply reads task.json/decision.json; verifier orchestrators write these legacy-compatible files in the orchestration dir.
        artifact = safe_apply(selection.workspace_path, artifact_name='materialize-apply.json', force=True)
        if selection.source.startswith('verifier'):
            logs.append('[materialize] applied verifier orchestration patch')
        return MaterializationResult(selection, 'applied', True, artifact, logs)
    except Exception as e:
        return MaterializationResult(selection, 'failed', False, None, logs, str(e))


def materialize_latest(workspace: str | Path) -> MaterializationResult:
    selection = newest_materializable_selection(workspace)
    print(f'[materialize] newest run path: {selection.workspace_path if selection else None}')
    if not selection:
        print('[materialize] no-op: no run state found')
        return MaterializationResult(None, 'no-op', False, None, ['[materialize] no-op: no run state found'])
    result = materialize_selection(selection)
    for line in result.logs or []:
        print(line)
    return result


def materialization_succeeded_with_accepted_result(workspace: str | Path) -> bool:
    state = discover_latest_verifier_parallel_state(workspace)
    return bool(state and state.accepted_result)


def wrapper_exit_code(villani_ops_exit_code: int, workspace: str | Path) -> int:
    """Return hardened wrapper status: never hide a Villani Ops failure with no accepted materialization."""
    if villani_ops_exit_code == 0:
        return 0
    if materialization_succeeded_with_accepted_result(workspace):
        return 0
    return villani_ops_exit_code
