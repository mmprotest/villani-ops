from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field

NodeKind = Literal['classify','investigate','plan','decompose','code','test','review','select','merge','verify']
NodeStatus = Literal['pending','ready','running','succeeded','failed','skipped']
Difficulty = Literal['easy','medium','hard','unknown']
Risk = Literal['low','medium','high','unknown']

class NodeResult(BaseModel):
    node_id: str | None = None
    status: str = 'pending'
    summary: str = ''
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
    # legacy shim
    result: dict[str, Any] | None = None
