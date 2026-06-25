from __future__ import annotations
from typing import Mapping
from villani_ops.core.backend import Backend as BackendConfig
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult
from .base import BackendSelection, role_highest

class PerformanceExecutionPolicy:
    mode='performance'
    def select_backend(self, *, node: OrchestrationNode, backends: Mapping[str, BackendConfig], task_context: TaskContext, prior_results: list[NodeResult] | None = None) -> BackendSelection:
        name,b,rr=role_highest(node, backends, task_context)
        return BackendSelection(backend_name=name, backend=b, reason='Performance mode used the most capable enabled backend for required node role. '+rr, confidence=1.0)
