from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field

class InvestigationResult(BaseModel):
    summary: str
    suspected_root_cause: str | None = None
    relevant_files: list[str] = Field(default_factory=list)
    relevant_tests: list[str] = Field(default_factory=list)
    implementation_plan: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    investigator_backend: str | None = None

class CandidateSummary(BaseModel):
    attempt_id: str
    backend_name: str
    model: str
    status: str
    exit_code: int | None = None
    changed_files: list[str] = Field(default_factory=list)
    patch_path: str | None = None
    review_decision: str | None = None
    review_score: float | None = None
    review_recommended_action: str | None = None
    review_summary: str = ""
    review_issues: list[str] = Field(default_factory=list)
    acceptance_eligible: bool = False
    acceptance_blockers: list[str] = Field(default_factory=list)
    telemetry: dict[str, Any] = Field(default_factory=dict)

class SelectionResult(BaseModel):
    selected_attempt_id: str | None = None
    decision: Literal["select", "reject_all"] = "reject_all"
    summary: str = ""
    reasons: list[str] = Field(default_factory=list)
    rejected_attempts: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    selector_backend: str | None = None
    fallback_used: bool = False
