from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field

class Task(BaseModel):
    instruction: str
    repo_path: str
    success_criteria: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
