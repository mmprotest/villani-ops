from __future__ import annotations
from typing import Mapping
from villani_ops.core.backend import Backend as BackendConfig
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult
from .base import BackendSelection, role_lowest, role_middle, role_highest, estimate_node_difficulty, prior_forces_escalation

class CheapExecutionPolicy:
    mode='cheap'
    def select_backend(self, *, node: OrchestrationNode, backends: Mapping[str, BackendConfig], task_context: TaskContext, prior_results: list[NodeResult] | None = None) -> BackendSelection:
        d,r,c=estimate_node_difficulty(node, task_context); node.difficulty=d; node.risk=r; node.confidence=c
        if prior_forces_escalation(prior_results) or d=='hard' or r=='high' or c < .65:
            n,b,rr=role_highest(node, backends, task_context); return BackendSelection(backend_name=n, backend=b, reason=f'Cheap mode escalated for difficulty={d}, risk={r}, confidence={c:.2f}, or prior failure/review blocker. {rr}', confidence=c, escalated=True)
        if d=='easy' and r=='low' and c >= .80:
            n,b,rr=role_lowest(node, backends, task_context); return BackendSelection(backend_name=n, backend=b, reason='Cheap mode routed easy, low-risk, high-confidence node to the lowest-capability enabled backend. '+rr, confidence=c)
        n,b,rr=role_middle(node, backends, task_context); return BackendSelection(backend_name=n, backend=b, reason=f'Cheap mode used middle backend for medium-confidence node difficulty={d}, risk={r}. {rr}', confidence=c)
