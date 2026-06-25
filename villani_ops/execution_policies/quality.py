from __future__ import annotations
from typing import Mapping
from villani_ops.core.backend import Backend as BackendConfig
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult
from .base import BackendSelection, middle_capability, highest_capability, estimate_node_difficulty, prior_forces_escalation

class QualityExecutionPolicy:
    mode='quality'
    def select_backend(self, *, node: OrchestrationNode, backends: Mapping[str, BackendConfig], task_context: TaskContext, prior_results: list[NodeResult] | None = None) -> BackendSelection:
        d,r,c=estimate_node_difficulty(node, task_context); node.difficulty=d; node.risk=r; node.confidence=c
        if prior_forces_escalation(prior_results) or node.kind in {'code','review','select','verify'} or d=='hard' or r=='high' or c < .90:
            n,b=highest_capability(backends); return BackendSelection(backend_name=n, backend=b, reason=f'Quality mode used highest backend for high-impact/uncertain node kind={node.kind}, difficulty={d}, risk={r}, confidence={c:.2f}.', confidence=c, escalated=node.kind not in {'code','review','select','verify'})
        n,b=middle_capability(backends); return BackendSelection(backend_name=n, backend=b, reason='Quality mode used middle backend only for very safe summarisation/planning work.', confidence=c)
