from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field

class Decision(BaseModel):
    run_id: str

    mode: str = "performance"
    runner: str = "villani-code"
    orchestration_graph_path: str | None = None
    selected_attempt_id: str | None = None
    node_backend_assignments: dict[str, str | None] = Field(default_factory=dict)
    plan: dict[str, Any] | None = None
    decomposition: dict[str, Any] | None = None
    performance_backend_name: str | None = None
    performance_backend_model: str | None = None
    investigation: dict[str, Any] | None = None
    selection: dict[str, Any] | None = None
    candidate_attempts_requested: int = 0
    candidate_attempts_completed: int = 0
    eligible_candidate_attempts: list[str] = Field(default_factory=list)
    orchestration_summary: str = ""
    accepted: bool = False
    lifecycle_completed: bool = False
    final_state: str = ''
    final_action: str = 'fail'
    winning_attempt_id: str | None = None
    winning_branch: str | None = None
    winning_worktree_path: str | None = None
    winning_patch_path: str | None = None
    reviewer_decision: str | None = None
    reviewer_score: float | None = None
    reviewer_evidence: list[str] = Field(default_factory=list)
    classification: dict[str, Any] | None = None
    execution_strategy: dict[str, Any] | None = None
    total_cost: float = 0
    coding_cost: float = 0
    classification_cost: float = 0
    policy_cost: float = 0
    review_cost: float = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_coding_input_tokens: int = 0
    total_coding_output_tokens: int = 0
    token_accounting_statuses: dict[str, int] = Field(default_factory=dict)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    apply_options: dict[str, Any] = Field(default_factory=dict)
    decision_steps: list[dict[str, Any]] = Field(default_factory=list)
    controller_steps: list[dict[str, Any]] = Field(default_factory=list)
    controller_steps_path: str | None = None
    failure_reason: str = ''
    acceptance_blockers: list[str] = Field(default_factory=list)
    retries_used: int = 0
    escalations_used: int = 0
    attempts_used: int = 0
    human_reviews_requested: int = 0
    human_reviews_completed: int = 0
    human_reviews_skipped: int = 0
    human_override_used: bool = False
    human_override_reasons: list[str] = Field(default_factory=list)
    human_override_blockers: list[str] = Field(default_factory=list)
    acceptance_blockers_before_override: list[str] = Field(default_factory=list)
    acceptance_blockers_after_override: list[str] = Field(default_factory=list)
    all_attempted_backends: list[str | None] = Field(default_factory=list)
    reason: str = ''
    total_attempts: int = 0
    discarded_attempts: list[dict] = Field(default_factory=list)
    decomposition_executed: bool = False
    decomposition_advisory_only: bool = False
    subtask_count: int = 0
    subtasks_executed: list[str] = Field(default_factory=list)
    subtasks_accepted: list[str] = Field(default_factory=list)
    subtasks_rejected: list[str] = Field(default_factory=list)
    integration_worktree_path: str | None = None
    integration_patch_path: str | None = None
    integration_validation: dict[str, Any] | None = None
    integration_validation_initial: dict[str, Any] | None = None
    integration_validation_after_repair: dict[str, Any] | None = None
    integration_scope_analysis: dict[str, Any] | None = None
    integration_repair_used: bool = False
    final_review: dict[str, Any] | None = None

# Backward-compatible helper with the P0 acceptance guard.
def select_attempt(run_id, attempts, selection=None, warnings=None):
    warnings=warnings or []
    valid=[a for a in attempts if getattr(a,'validation',None) and a.validation.passed and (getattr(a,'status',None) in {'validated','human_approved'} or getattr(a,'status',None)=='pending') and getattr(a,'error',None) is None]

    if valid and selection and getattr(selection, 'choose_lowest_cost_valid_attempt', False):
        winner=min(valid, key=lambda a: (a.estimated_cost, -a.validation.score))
    elif valid:
        winner=max(valid, key=lambda a: (a.validation.score, -a.estimated_cost))
    else:
        winner=None
    return Decision(run_id=run_id, accepted=winner is not None, final_action='accept' if winner else 'fail', winning_attempt_id=winner.attempt_id if winner else None, total_attempts=len(attempts), total_cost=sum(a.estimated_cost for a in attempts), total_input_tokens=sum(a.input_tokens for a in attempts), total_output_tokens=sum(a.output_tokens for a in attempts), warnings=warnings, reason='Selected valid successful attempt.' if winner else 'No valid successful attempt found.')
