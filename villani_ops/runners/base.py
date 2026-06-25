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
    debug_artifact_dir: str | None = None
    resolved_trace_dir: str | None = None
    telemetry_path: str | None = None
    duration_ms: int | None = None

    model_requests: int = 0
    model_failures: int = 0
    total_tool_calls: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    total_file_reads: int = 0
    total_file_writes: int = 0
    commands_executed: int = 0
    commands_failed: int = 0

    first_substantive_file_read_tool_index: int | None = None
    first_substantive_file_read_seconds: float | None = None
    first_file_mutation_tool_index: int | None = None
    first_file_mutation_seconds: float | None = None
    first_command_tool_index: int | None = None
    first_command_seconds: float | None = None

    token_accounting_status: str = "missing"
    token_accounting_warnings: list[str] = Field(default_factory=list)
    telemetry: dict[str, Any] = Field(default_factory=dict)

class RunnerAdapter(Protocol):
    name: str
    def run(self, context: RunnerContext) -> RunnerResult: ...

Runner = RunnerAdapter
