from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from villani_ops.controller.state_machine import HumanApprovalResult

@dataclass
class HumanApprovalPromptContext:
    run_id: str
    attempt_id: str
    backend_name: str
    backend_model: str
    runner_exit_code: int | None
    review_decision: str | None
    review_score: float | None
    review_summary: str
    review_evidence: list[str]
    review_issues: list[str]
    acceptance_blockers: list[str]
    patch_path: str | None
    changed_files: list[str]
    cost_so_far: float
    request_reasons: list[str]

class HumanApprovalProvider(Protocol):
    def request_approval(self, context: HumanApprovalPromptContext) -> HumanApprovalResult: ...

class NonInteractiveHumanApprovalProvider:
    def request_approval(self, context: HumanApprovalPromptContext) -> HumanApprovalResult:
        return HumanApprovalResult(requested=True, request_reasons=context.request_reasons, prompted=False, skipped_reason='non_interactive', decision='skipped', shown_evidence=_shown(context))

class TerminalHumanApprovalProvider:
    def request_approval(self, context: HumanApprovalPromptContext) -> HumanApprovalResult:
        print(f"Human approval requested for {context.attempt_id} ({context.backend_name}/{context.backend_model})")
        print(f"Reviewer: {context.review_decision}, score {context.review_score}")
        print(f"Summary: {context.review_summary}")
        print('Evidence: ' + '; '.join(context.review_evidence))
        print('Issues: ' + '; '.join(context.review_issues))
        print('Acceptance blockers: ' + '; '.join(context.acceptance_blockers))
        print(f"Patch: {context.patch_path}")
        print('Changed files: ' + ', '.join(context.changed_files))
        print(f"Cost so far: ${context.cost_so_far:.6f}")
        allowed={'accept','reject','retry','escalate','fail'}
        for _ in range(3):
            decision=input('Decision [accept/reject/retry/escalate/fail]: ').strip().lower()
            if decision in allowed: break
            print('Invalid decision; choose accept, reject, retry, escalate, or fail.')
        else:
            decision='reject'
        reason=input('Reason: ').strip() or 'local user decision'
        return HumanApprovalResult(requested=True, request_reasons=context.request_reasons, prompted=True, decision=decision, reason=reason, approved_by='local_user', valid_override=_valid(decision, context), shown_evidence=_shown(context), created_at=datetime.now(timezone.utc))

class TestHumanApprovalProvider:
    __test__ = False
    def __init__(self, decision: str='accept', reason: str='test'):
        self.decision=decision; self.reason=reason; self.contexts=[]
    def request_approval(self, context: HumanApprovalPromptContext) -> HumanApprovalResult:
        self.contexts.append(context)
        return HumanApprovalResult(requested=True, request_reasons=context.request_reasons, prompted=True, decision=self.decision, reason=self.reason, approved_by='test', valid_override=_valid(self.decision, context), shown_evidence=_shown(context))

def _shown(c: HumanApprovalPromptContext) -> dict[str, Any]:
    return {'patch_path':c.patch_path,'changed_files':c.changed_files,'reviewer_summary':c.review_summary,'reviewer_decision':c.review_decision,'reviewer_issues':c.review_issues,'acceptance_blockers':c.acceptance_blockers}

def _valid(decision: str, c: HumanApprovalPromptContext) -> bool:
    return decision == 'accept' and bool(c.request_reasons) and bool(c.patch_path) and bool(c.changed_files)
