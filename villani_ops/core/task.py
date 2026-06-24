from __future__ import annotations
from typing import Any, Literal
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import secrets

class TaskClassification(BaseModel):
    difficulty: Literal["easy","medium","hard"] = "medium"
    category: str = "unknown"
    risk: Literal["low","medium","high"] = "medium"
    estimated_attempts_needed: int = 1
    needs_tests: bool = True
    likely_files: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    confidence: float = 0.0
    adjustment_notes: list[str] = Field(default_factory=list)
    relevant_file_paths: list[str] = Field(default_factory=list)
    task_shape_signals: dict[str, Any] = Field(default_factory=dict)
    original_difficulty: str | None = None
    original_risk: str | None = None

class Task(BaseModel):
    task_id: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("task_%Y%m%dT%H%M%SZ_")+secrets.token_hex(3))
    repo_path: str
    objective: str | None = None
    instruction: str | None = None
    success_criteria: str | None = None
    constraints: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
    classification: TaskClassification | None = None

    def model_post_init(self, __context):
        if self.objective is None and self.instruction is not None:
            self.objective=self.instruction
        if self.instruction is None and self.objective is not None:
            self.instruction=self.objective
