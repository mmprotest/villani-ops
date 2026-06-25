from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field

NodeKind = Literal['investigate','decompose','plan','code','test','review','select','merge','verify']

class NodeResult(BaseModel):
    status: str = 'pending'
    summary: str = ''
    data: dict[str, Any] = Field(default_factory=dict)

class OrchestrationNode(BaseModel):
    id: str
    kind: NodeKind
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    parallel_group: str | None = None
    runner: str | None = None
    assigned_backend: str | None = None
    status: str = 'pending'
    result: dict[str, Any] | None = None
    artifacts: list[str] = Field(default_factory=list)
