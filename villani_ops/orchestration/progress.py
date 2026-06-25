from __future__ import annotations
from typing import Any

STEP = {"classify":"1/8","investigate":"2/8","plan":"3/8","decompose":"4/8","code":"5/8","review":"6/8","select":"7/8","verify":"8/8"}

class ProgressReporter:
    verbose: bool = False
    def start_run(self, *, run_dir: str, mode: str, runner: str, candidate_attempts: int) -> None: pass
    def node_started(self, node: Any) -> None: pass
    def node_completed(self, node: Any, data: Any = None, summary: str | None = None) -> None: pass
    def node_failed(self, node: Any, error: str) -> None: pass
    def node_skipped(self, node: Any, reason: str) -> None: pass
    def candidate_started(self, attempt_id: str, index: int, total: int, backend: str | None = None) -> None: pass
    def candidate_completed(self, attempt_id: str, index: int, total: int, data: dict) -> None: pass
    def review_started(self, attempt_id: str, index: int, total: int) -> None: pass
    def review_completed(self, attempt_id: str, index: int, total: int, data: dict) -> None: pass
    def selector_started(self) -> None: pass
    def selector_completed(self, selection: Any, notes: list[str] | None = None) -> None: pass
    def fallback_used(self, reason: str, selected_attempt_id: str | None) -> None: pass
    def final_decision(self, accepted: bool, winner: str | None = None, reason: str | None = None) -> None: pass

class NullProgressReporter(ProgressReporter):
    pass

class ConsoleProgressReporter(ProgressReporter):
    def __init__(self, *, verbose: bool = False): self.verbose = verbose
    def _print(self, msg: str = "") -> None: print(msg, flush=True)
    def start_run(self, *, run_dir: str, mode: str, runner: str, candidate_attempts: int) -> None:
        self._print('Villani Ops run started'); self._print(f'Run directory: {run_dir}'); self._print(f'Mode: {mode}'); self._print(f'Runner: {runner}'); self._print(f'Candidate attempts: {candidate_attempts}'); self._print()
    def node_started(self, node: Any) -> None:
        labels={'classify':'Classifying task','investigate':'Investigating repo','plan':'Planning execution','decompose':'Checking decomposition'}
        if node.kind in labels: self._print(f'[{STEP[node.kind]}] {labels[node.kind]}...')
        if self.verbose and getattr(node,'assigned_backend',None): self._print(f'[{STEP.get(node.kind,"?")}] Backend: {node.assigned_backend}/{getattr(node,"assigned_model","")}')
    def node_completed(self, node: Any, data: Any = None, summary: str | None = None) -> None:
        data=data or {}; k=node.kind
        if k=='classify': self._print(f'[{STEP[k]}] Classification complete: {data.get("category") or data.get("task_type") or "unknown"}, difficulty={data.get("difficulty")}, confidence={data.get("confidence","")}')
        elif k=='investigate': self._print(f'[{STEP[k]}] Investigation complete: {len(data.get("relevant_files") or [])} relevant files, confidence={data.get("confidence","")}')
        elif k=='plan': self._print(f'[{STEP[k]}] Plan complete: strategy={data.get("strategy")}, candidates={data.get("candidate_attempts") or data.get("candidates")}, decompose={data.get("should_decompose")}')
        elif k=='decompose': self._print(f'[{STEP[k]}] Decomposition complete: subtask_count={len(data.get("subtasks") or [])}')
    def node_skipped(self, node: Any, reason: str) -> None:
        if node.kind=='decompose': self._print(f'[{STEP[node.kind]}] Decomposition skipped: {reason}')
    def node_failed(self, node: Any, error: str) -> None: self._print(f'[{STEP.get(node.kind,"?")}] {node.kind} failed: {error}')
    def candidate_started(self, attempt_id: str, index: int, total: int, backend: str | None = None) -> None: self._print(f'[{STEP["code"]}] Running candidate attempt {index}/{total}' + (f' with {backend}' if backend else '') + '...')
    def candidate_completed(self, attempt_id: str, index: int, total: int, data: dict) -> None: self._print(f'[{STEP["code"]}] Candidate attempt {index} complete: exit={data.get("exit_code")}, changed_files={len(data.get("changed_files") or [])}, patch={"yes" if data.get("patch_path") else "no"}')
    def review_started(self, attempt_id: str, index: int, total: int) -> None: self._print(f'[{STEP["review"]}] Reviewing candidate attempt {index}/{total}...')
    def review_completed(self, attempt_id: str, index: int, total: int, data: dict) -> None: self._print(f'[{STEP["review"]}] Review complete: {data.get("decision")}/{data.get("recommended_action")}, score={data.get("score")}, eligible={data.get("acceptance_eligible")}')
    def selector_started(self) -> None: self._print(f'[{STEP["select"]}] Selecting winner...')
    def selector_completed(self, selection: Any, notes: list[str] | None = None) -> None:
        for n in notes or []: self._print(f'[{STEP["select"]}] Selector output used alias/normalization: {n}')
        if getattr(selection,'decision',None)=='select': self._print(f'[{STEP["select"]}] Selector chose {selection.selected_attempt_id}')
        else: self._print(f'[{STEP["select"]}] Selector rejected all candidates')
    def fallback_used(self, reason: str, selected_attempt_id: str | None) -> None: self._print(f'[{STEP["select"]}] Selector returned invalid winner; deterministic fallback selected {selected_attempt_id}: {reason}')
    def final_decision(self, accepted: bool, winner: str | None = None, reason: str | None = None) -> None: self._print(f'[{STEP["verify"]}] Final decision: {"accepted" if accepted else "failed"}' + (f', winner={winner}' if winner else f', {reason}' if reason else ''))
