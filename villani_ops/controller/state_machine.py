from __future__ import annotations
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, Field
from pathlib import Path
import json
from villani_ops.policy_engine.engine import ExecutionStrategy
from villani_ops.review.reviewer import ReviewResult
from villani_ops.core.acceptance import is_attempt_acceptance_eligible

class ControllerState(StrEnum):
    planned='planned'; classifying='classifying'; planning='planning'; attempting='attempting'; reviewing='reviewing'; human_review='human_review'; deciding='deciding'; accepted='accepted'; retrying='retrying'; escalating='escalating'; failed='failed'
class ControllerAction(StrEnum):
    start='start'; classify='classify'; generate_strategy='generate_strategy'; run_attempt='run_attempt'; review_attempt='review_attempt'; decide='decide'; ask_human='ask_human'; accept='accept'; retry_same_backend='retry_same_backend'; escalate='escalate'; fail='fail'

class HumanApprovalResult(BaseModel):
    requested: bool=False
    request_reasons: list[str]=Field(default_factory=list)
    prompted: bool=False
    skipped_reason: str|None=None
    decision: str='skipped'
    reason: str|None=None
    approved_by: str|None=None
    valid_override: bool=False
    shown_evidence: dict[str, Any]=Field(default_factory=dict)
    created_at: datetime=Field(default_factory=lambda: datetime.now(timezone.utc))

class ControllerStep(BaseModel):
    step_id: str
    run_id: str
    attempt_id: str|None=None
    state_before: str
    action: str
    state_after: str
    reason: str
    data: dict[str, Any]=Field(default_factory=dict)
    created_at: datetime=Field(default_factory=lambda: datetime.now(timezone.utc))

class ControllerDecisionContext(BaseModel):
    run_id: str
    attempt: dict[str, Any]|None=None
    review: ReviewResult|None=None
    human_approval: HumanApprovalResult|None=None
    strategy: ExecutionStrategy
    current_plan_index: int=0
    current_attempt_number: int=1
    attempts_remaining_for_backend: int=0
    escalation_available: bool=False
    non_interactive: bool=True
    human_override_allowed: bool=False

class ControllerActionDecision(BaseModel):
    action: ControllerAction
    reason: str
    acceptance_eligible: bool=False
    acceptance_blockers: list[str]=Field(default_factory=list)
    should_stop: bool=False
    should_retry_same_backend: bool=False
    should_escalate: bool=False
    should_ask_human: bool=False

class ControllerStateRecorder:
    def __init__(self, run_id: str, run_dir: str|Path, initial_state: ControllerState=ControllerState.planned):
        self.run_id=run_id; self.run_dir=Path(run_dir); self.current_state=initial_state; self.steps: list[dict[str, Any]]=[]
        self.path=self.run_dir/'controller_steps.jsonl'; self.path.write_text('')

    def transition(self, action: ControllerAction|str, state_after: ControllerState|str, reason: str, attempt_id: str|None=None, data: dict[str, Any]|None=None) -> ControllerStep:
        action_v = action.value if isinstance(action, ControllerAction) else str(action)
        after_v = state_after.value if isinstance(state_after, ControllerState) else str(state_after)
        before_v = self.current_state.value if isinstance(self.current_state, ControllerState) else str(self.current_state)
        step=ControllerStep(step_id=f"step_{len(self.steps)+1:03d}", run_id=self.run_id, attempt_id=attempt_id, state_before=before_v, action=action_v, state_after=after_v, reason=reason, data=data or {})
        row=step.model_dump(mode='json'); self.steps.append(row)
        with self.path.open('a') as f: f.write(json.dumps(row)+"\n")
        self.current_state=ControllerState(after_v) if after_v in ControllerState._value2member_map_ else after_v
        return step

def _retry_or_escalate_or_fail(ctx: ControllerDecisionContext, reason: str) -> ControllerActionDecision:
    if ctx.attempts_remaining_for_backend > 0:
        return ControllerActionDecision(action=ControllerAction.retry_same_backend, reason=reason, should_retry_same_backend=True)
    if ctx.escalation_available:
        return ControllerActionDecision(action=ControllerAction.escalate, reason=reason, should_escalate=True)
    return ControllerActionDecision(action=ControllerAction.fail, reason=reason, should_stop=True)

def decide_next_action(context: ControllerDecisionContext) -> ControllerActionDecision:
    a=context.attempt
    if not a:
        return ControllerActionDecision(action=ControllerAction.run_attempt, reason='No attempt exists yet; run the next planned attempt.')
    review=context.review
    if review is None:
        return ControllerActionDecision(action=ControllerAction.review_attempt, reason='Attempt has no reviewer result; never accept without review.')
    h=context.human_approval
    eligible, blockers = is_attempt_acceptance_eligible(a, h)
    if h and h.decision != 'skipped':
        if h.decision == 'accept':
            if context.human_override_allowed:
                return ControllerActionDecision(action=ControllerAction.accept, reason='Human accepted after legitimate approval request.', acceptance_eligible=True, acceptance_blockers=[], should_stop=True)
            return _retry_or_escalate_or_fail(context, 'Human accepted but override is not enabled; continuing safely.')
        if h.decision == 'reject':
            return _retry_or_escalate_or_fail(context, 'Human rejected the attempt; do not accept.')
        if h.decision == 'retry':
            if context.attempts_remaining_for_backend > 0:
                return ControllerActionDecision(action=ControllerAction.retry_same_backend, reason='Human requested retry.', should_retry_same_backend=True)
            if context.escalation_available:
                return ControllerActionDecision(action=ControllerAction.escalate, reason='Human requested retry but backend attempts are exhausted; escalating.', should_escalate=True)
            return ControllerActionDecision(action=ControllerAction.fail, reason='Human requested retry but no retry or escalation remains.', should_stop=True)
        if h.decision == 'escalate':
            if context.escalation_available:
                return ControllerActionDecision(action=ControllerAction.escalate, reason='Human requested escalation.', should_escalate=True)
            return ControllerActionDecision(action=ControllerAction.fail, reason='Human requested escalation but none is available.', should_stop=True)
        if h.decision == 'fail':
            return ControllerActionDecision(action=ControllerAction.fail, reason='Human requested failure.', should_stop=True)
    if eligible and review.decision=='pass' and review.recommended_action=='accept':
        return ControllerActionDecision(action=ControllerAction.accept, reason='Review passed and acceptance gates are eligible.', acceptance_eligible=True, should_stop=True)
    if review.decision=='uncertain' and (review.requires_human_approval or review.recommended_action=='ask_human'):
        if not context.non_interactive:
            return ControllerActionDecision(action=ControllerAction.ask_human, reason='Uncertain review requires human input.', acceptance_eligible=eligible, acceptance_blockers=blockers, should_ask_human=True)
        return _retry_or_escalate_or_fail(context, 'Human review skipped in non-interactive mode; continuing safely.')
    if review.recommended_action=='retry_same_backend' and context.attempts_remaining_for_backend>0:
        return ControllerActionDecision(action=ControllerAction.retry_same_backend, reason='Reviewer recommended retry and attempts remain.', acceptance_blockers=blockers, should_retry_same_backend=True)
    if review.recommended_action=='escalate' and context.escalation_available:
        return ControllerActionDecision(action=ControllerAction.escalate, reason='Reviewer recommended escalation and another backend is available.', acceptance_blockers=blockers, should_escalate=True)
    d=_retry_or_escalate_or_fail(context, 'Attempt is not acceptable; exhausted recommended safe path.'); d.acceptance_blockers=blockers; return d

def next_state(action: ControllerAction) -> ControllerState:
    return {ControllerAction.run_attempt:ControllerState.attempting,ControllerAction.review_attempt:ControllerState.reviewing,ControllerAction.ask_human:ControllerState.human_review,ControllerAction.accept:ControllerState.accepted,ControllerAction.retry_same_backend:ControllerState.retrying,ControllerAction.escalate:ControllerState.escalating,ControllerAction.fail:ControllerState.failed}.get(action, ControllerState.deciding)
