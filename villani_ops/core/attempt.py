from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel
from villani_ops.validation.base import ValidationResult

AttemptStatus = Literal["pending","running","succeeded","failed","validated","rejected","skipped"]

class Attempt(BaseModel):
    attempt_id: str
    run_id: str
    backend_name: str
    runner_name: str
    status: AttemptStatus = "pending"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    isolated_repo_path: str | None = None
    diff_path: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    events_path: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0
    validation: ValidationResult | None = None
    error: str | None = None
