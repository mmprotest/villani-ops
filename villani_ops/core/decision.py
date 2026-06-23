from __future__ import annotations
from pydantic import BaseModel, Field
from .attempt import Attempt
from .policy import SelectionConfig

class Decision(BaseModel):
    run_id: str
    winning_attempt_id: str | None = None
    accepted: bool = False
    reason: str
    total_attempts: int
    total_cost: float
    total_input_tokens: int
    total_output_tokens: int
    discarded_attempts: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

def select_attempt(run_id: str, attempts: list[Attempt], selection: SelectionConfig, warnings: list[str] | None = None) -> Decision:
    warnings = warnings or []
    valid = [a for a in attempts if a.validation and a.validation.passed]
    winner = None
    if valid:
        if selection.choose_lowest_cost_valid_attempt:
            winner = min(valid, key=lambda a: (a.estimated_cost, -(a.validation.score if a.validation else 0)))
            reason = "Selected lowest-cost valid attempt."
        else:
            winner = max(valid, key=lambda a: ((a.validation.score if a.validation else 0), -a.estimated_cost))
            reason = "Selected highest-scoring valid attempt, tie-broken by lower cost."
    else:
        reason = "No valid attempt was found."
    return Decision(
        run_id=run_id, winning_attempt_id=winner.attempt_id if winner else None, accepted=winner is not None, reason=reason,
        total_attempts=len(attempts), total_cost=sum(a.estimated_cost for a in attempts), total_input_tokens=sum(a.input_tokens for a in attempts),
        total_output_tokens=sum(a.output_tokens for a in attempts),
        discarded_attempts=[{"attempt_id": a.attempt_id, "status": a.status, "reason": a.error or (a.validation.summary if a.validation else "not selected")} for a in attempts if not winner or a.attempt_id != winner.attempt_id], warnings=warnings)
