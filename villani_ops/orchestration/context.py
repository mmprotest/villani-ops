from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel

class TaskContext(BaseModel):
    objective: str
    success_criteria: str | None = None
    classification: dict[str, Any] | None = None
    investigation: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    decomposition: dict[str, Any] | None = None
    repo_summary: str | None = None
    overall_difficulty: Literal['easy','medium','hard','unknown'] = 'unknown'
    overall_risk: Literal['low','medium','high','unknown'] = 'unknown'
    confidence: float = 0.0
