from __future__ import annotations
from typing import Protocol, Mapping
from pydantic import BaseModel
from villani_ops.core.backend import Backend as BackendConfig
from villani_ops.core.task import Task as TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult

class BackendSelection(BaseModel):
    backend_name: str
    reason: str = ''
    escalated: bool = False

class ExecutionPolicy(Protocol):
    mode: str
    def select_backend(self, *, node: OrchestrationNode, backends: Mapping[str, BackendConfig], task_context: TaskContext, confidence: float | None = None, prior_results: list[NodeResult] | None = None) -> BackendSelection: ...

def enabled_backends(backends: Mapping[str, BackendConfig]) -> list[tuple[str, BackendConfig]]:
    vals=[(n,b) for n,b in backends.items() if b.enabled]
    if not vals: raise ValueError('No enabled backend configured')
    return vals
