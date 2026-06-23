from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field

class ValidationResult(BaseModel):
    passed: bool
    score: float = Field(ge=0, le=1)
    summary: str
    reasons: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    validator: str

class DiffReviewValidator:
    name = "diff_review"
    def validate(self, diff_path: str | Path, require_test_evidence: bool = False) -> ValidationResult:
        path = Path(diff_path)
        if not path.exists():
            return ValidationResult(passed=False, score=0, summary="No diff artifact was produced.", reasons=["diff.patch is missing"], validator=self.name)
        text = path.read_text(errors="replace")
        if not text.strip():
            return ValidationResult(passed=False, score=0, summary="No repository changes were detected.", reasons=["diff.patch is empty"], evidence=[str(path)], validator=self.name)
        score = 0.8
        reasons = ["Non-empty diff was produced"]
        if require_test_evidence:
            score -= 0.2
            reasons.append("Policy requires test evidence; deterministic diff review cannot prove tests ran")
        return ValidationResult(passed=True, score=max(score, 0), summary="Diff exists and passed deterministic sanity checks.", reasons=reasons, evidence=[str(path)], validator=self.name)

class LLMReviewValidator:
    name = "llm_review"
    def validate(self, *_args, **_kwargs) -> ValidationResult:
        return ValidationResult(passed=False, score=0, summary="LLM review is not configured in v0.1.", reasons=["No reviewer backend client is implemented/configured"], validator=self.name)
