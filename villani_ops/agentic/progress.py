from __future__ import annotations
from rich.console import Console

class AgenticProgressReporter:
    def __init__(self, enabled: bool = True, verbose: bool = False, console: Console | None = None):
        self.enabled=enabled; self.verbose=verbose; self.console=console or Console()
    def _print(self, msg: str) -> None:
        if not self.enabled: return
        try: self.console.print(msg, soft_wrap=True, markup=False)
        except UnicodeEncodeError: self.console.print(msg.encode('ascii','backslashreplace').decode('ascii'), soft_wrap=True, markup=False)
        except Exception: pass
    def on_event(self, event) -> None:
        t=event.type; p=event.payload or {}
        detail=''
        if self.verbose:
            paths=p.get('artifact_paths') or {}
            if paths: detail=' artifacts='+', '.join(f'{k}={v}' for k,v in paths.items() if v)
        if t=='run_started':
            self._print('Villani Ops agentic run started')
            if p.get('run_dir'): self._print(f"Run directory: {p.get('run_dir')}")
        elif t=='investigation_submitted': self._print('[agentic] Investigation submitted')
        elif t=='classification_submitted': self._print(f"[agentic] Classification submitted: {p.get('difficulty') or p.get('classification',{}).get('difficulty') or ''} / {p.get('category') or p.get('classification',{}).get('category') or ''}".rstrip())
        elif t=='plan_submitted': self._print(f"[agentic] Plan submitted: {p.get('strategy') or p.get('execution_strategy') or p.get('plan_type') or ''}".rstrip())
        elif t=='decomposition_submitted': self._print(f"[agentic] Decomposition submitted: {p.get('subtask_count') or len(p.get('subtasks') or [])} subtasks")
        elif t=='decomposition_validation_completed': self._print(f"[agentic] Decomposition validated: {'accepted' if p.get('accepted') else 'rejected'}")
        elif t=='execution_path_selected': self._print(f"[agentic] Execution path selected: {p.get('execution_path') or p.get('path')}")
        elif t=='recovery_deterministic_action_executed':
            tool=p.get('tool_name') or p.get('recommendation',{}).get('tool_name') or ''
            reason=p.get('reason') or p.get('recommendation',{}).get('reason') or ''
            if tool=='ops_select_execution_path':
                path=(p.get('tool_input') or {}).get('path') or (p.get('recommendation',{}).get('tool_input') or {}).get('path')
                self._print(f"[agentic] Recovery selected execution path: {path or reason}".rstrip())
            elif tool=='ops_launch_subtasks':
                self._print(f"[agentic] Recovery launching accepted decomposition subtasks: {reason}".rstrip())
            else:
                self._print(f"[agentic] Recovery executed {tool}: {reason}".rstrip())
        elif t in {'candidate_attempt_started','subtask_attempt_started'}:
            kind=('Fallback candidate' if t.startswith('candidate') and p.get('fallback') else ('Candidate' if t.startswith('candidate') else f"Subtask {p.get('subtask_id') or ''}".strip()))
            self._print(f"[agentic] {kind} attempt {p.get('attempt_id')} started" + (f": backend={p.get('backend_name')}" if self.verbose and p.get('backend_name') else '') + detail)
        elif t in {'candidate_attempt_completed','subtask_attempt_completed','candidate_attempt_failed','subtask_attempt_failed'}:
            kind=('Fallback candidate' if t.startswith('candidate') and p.get('fallback') else ('Candidate' if t.startswith('candidate') else f"Subtask {p.get('subtask_id') or ''}".strip()))
            status='completed' if t.endswith('completed') else 'failed'
            extra=f"exit_code={p.get('exit_code')}" if p.get('exit_code') is not None else (p.get('failure_reason') or '')
            self._print(f"[agentic] {kind} attempt {p.get('attempt_id')} {status}: {extra}".rstrip()+detail)
        elif t in {'candidate_attempt_reviewed','subtask_attempt_reviewed'}:
            kind='Candidate' if t.startswith('candidate') else 'Subtask'
            self._print(f"[agentic] {kind} review {p.get('attempt_id')}: {p.get('review_decision')}, {p.get('review_recommended_action')}")
        elif t=='subtask_accepted': self._print(f"[agentic] Subtask {p.get('subtask_id')} accepted")
        elif t=='subtask_failed': self._print(f"[agentic] Subtask {p.get('subtask_id')} failed: {p.get('reason')}")
        elif t=='decomposition_deadlock_detected':
            fs=', '.join(p.get('failed_subtasks') or []) or 'unknown'; bc=len(p.get('blocked_subtasks') or [])
            self._print(f'[agentic] Decomposition deadlock detected: {fs} failed, {bc} dependents blocked')
        elif t=='candidate_fallback_started': self._print('[agentic] Falling back to full-task candidates after decomposition deadlock')
        elif t=='integration_started': self._print(f"[agentic] Integrating {p.get('accepted_subtasks', '')} accepted subtasks".rstrip())
        elif t=='integration_completed': self._print(f"[agentic] Integration completed: changed_files={len(p.get('changed_files') or [])}")
        elif t=='integration_failed': self._print(f"[agentic] Integration failed: {p.get('failure_reason') or p.get('reason')}")
        elif t=='validation_started':
            label=p.get('target_label') or p.get('target_id') or p.get('target')
            loc='repo' if p.get('target')=='repo' else 'worktree'
            self._print(f"[agentic] Validation started for {label} in {loc}: {p.get('cmd')}")
        elif t=='validation_command_rejected':
            label=p.get('target_id') or p.get('target')
            self._print(f"[agentic] Validation command rejected for {label}: {p.get('message') or p.get('reason')}")
        elif t in {'validation_completed','validation_failed'}: self._print(f"[agentic] Validation {'completed' if t.endswith('completed') else 'failed'}")
        elif t in {'winner_selected','selection_completed'}: self._print(f"[agentic] Selection completed: {p.get('selected_attempt_id')}")
        elif t=='run_finalized': self._print(f"[agentic] Final decision: {p.get('decision') or p.get('status') or p.get('summary')}")
        elif self.verbose and t in {'model_request_started','model_response_received','tool_result_appended','recovery_injected','tool_failed'}:
            self._print(f"[agentic] {t}: {p.get('message') or p.get('finish_reason') or p.get('tool_name') or ''}".rstrip())
