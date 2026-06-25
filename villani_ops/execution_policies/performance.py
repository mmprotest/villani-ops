from __future__ import annotations
from typing import Mapping
from villani_ops.core.backend import Backend as BackendConfig
from villani_ops.core.task import Task as TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult
from .base import BackendSelection, enabled_backends

class PerformanceExecutionPolicy:
    mode='performance'
    def select_backend(self, *, node: OrchestrationNode, backends: Mapping[str, BackendConfig], task_context: TaskContext, confidence: float | None = None, prior_results: list[NodeResult] | None = None) -> BackendSelection:
        name,b=sorted(enabled_backends(backends), key=lambda x:(-x[1].capability_score, x[0]))[0]
        return BackendSelection(backend_name=name, reason='Performance mode used the most capable enabled backend for every node.')
