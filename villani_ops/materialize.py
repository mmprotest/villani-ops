from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
from typing import Any

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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def discover_latest_verifier_parallel_state(workspace: str | Path) -> VerifierParallelRunState | None:
    """Find the newest verifier-parallel orchestration with selection/integration artifacts."""
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
