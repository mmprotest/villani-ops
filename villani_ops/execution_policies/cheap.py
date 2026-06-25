from __future__ import annotations
from typing import Mapping
from villani_ops.core.backend import Backend as BackendConfig
from villani_ops.core.task import Task as TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult
from .base import BackendSelection, enabled_backends

def _cheapest_capable(backends):
    return sorted(enabled_backends(backends), key=lambda x:(x[1].input_cost_per_million+x[1].output_cost_per_million, -x[1].capability_score, x[0]))[0]
def _strongest(backends): return sorted(enabled_backends(backends), key=lambda x:(-x[1].capability_score, x[0]))[0]
class CheapExecutionPolicy:
    mode='cheap'
    def select_backend(self, *, node: OrchestrationNode, backends: Mapping[str, BackendConfig], task_context: TaskContext, confidence: float | None = None, prior_results: list[NodeResult] | None = None) -> BackendSelection:
        prior_results=prior_results or []
        hard=node.kind in {'review','select','verify'} or any(r.status in {'failed','uncertain'} for r in prior_results)
        if hard or (confidence is not None and confidence < .65):
            n,_=_strongest(backends); return BackendSelection(backend_name=n, reason='Escalated to strongest backend for uncertainty, review/selection, or prior failure.', escalated=True)
        n,_=_cheapest_capable(backends); return BackendSelection(backend_name=n, reason='Cheap mode routed high-confidence/easy node to the cheapest enabled backend.')
