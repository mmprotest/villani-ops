from __future__ import annotations
from typing import Protocol, Any
from pydantic import BaseModel, Field
from villani_ops.core.backend import Backend

class RunnerContext(BaseModel):
    attempt_id: str
    repo_path: str
    task_instruction: str
    success_criteria: str | None = None
    backend: Backend
    timeout_seconds: int
    run_dir: str
    env: dict[str, str] = Field(default_factory=dict)
    command: str | None = None

class RunnerResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    events: list[dict[str, Any]] = Field(default_factory=list)

class Runner(Protocol):
    name: str
    def run(self, context: RunnerContext) -> RunnerResult: ...
