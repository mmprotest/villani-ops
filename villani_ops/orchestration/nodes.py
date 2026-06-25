from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field

NodeKind = Literal['classify','investigate','plan','decompose','code','test','review','select','merge','verify','integrate','integration_validate','integration_repair','final_review']
NodeStatus = Literal['pending','ready','running','succeeded','failed','skipped']
Difficulty = Literal['easy','medium','hard','unknown']
Risk = Literal['low','medium','high','unknown']

class NodeResult(BaseModel):
    node_id: str = ''
    kind: str = ''
    status: str = 'pending'
    result_summary: str | None = None
    confidence: float | None = None
    difficulty: str | None = None
    risk: str | None = None
    has_failure: bool = False
    has_review_blocker: bool = False
    has_acceptance_blocker: bool = False
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # legacy alias accepted by old tests
    summary: str | None = None

class NodeExecutionResult(BaseModel):
    node_id: str
    status: Literal['succeeded','failed','skipped']
    result_summary: str | None = None
    confidence: float | None = None
    difficulty: str | None = None
    risk: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

class OrchestrationNode(BaseModel):
    id: str
    kind: NodeKind
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    parallel_group: str | None = None
    runner: str | None = None
    assigned_backend: str | None = None
    assigned_model: str | None = None
    status: NodeStatus = 'pending'
    result_summary: str | None = None
    confidence: float | None = None
    difficulty: Difficulty = 'unknown'
    risk: Risk = 'unknown'
    artifacts: dict[str, str] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
