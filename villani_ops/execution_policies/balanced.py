from __future__ import annotations
from typing import Mapping
from villani_ops.core.backend import Backend as BackendConfig
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult
from .base import BackendSelection, role_lowest, role_middle, role_highest, estimate_node_difficulty, prior_forces_escalation

class BalancedExecutionPolicy:
    mode='balanced'
    def select_backend(self, *, node: OrchestrationNode, backends: Mapping[str, BackendConfig], task_context: TaskContext, prior_results: list[NodeResult] | None = None) -> BackendSelection:
        d,r,c=estimate_node_difficulty(node, task_context); node.difficulty=d; node.risk=r; node.confidence=c
        if prior_forces_escalation(prior_results) or d=='hard' or r=='high' or c < .80:
            n,b,rr=role_highest(node, backends, task_context); return BackendSelection(backend_name=n, backend=b, reason=f'Balanced mode escalated for difficulty={d}, risk={r}, confidence={c:.2f}, or prior failure/review blocker. {rr}', confidence=c, escalated=True)
        if d=='easy' and r=='low' and c >= .85:
            n,b,rr=role_lowest(node, backends, task_context); return BackendSelection(backend_name=n, backend=b, reason='Balanced mode allowed smaller backend for clearly easy, low-risk, high-confidence work. '+rr, confidence=c)
        n,b,rr=role_middle(node, backends, task_context); return BackendSelection(backend_name=n, backend=b, reason='Balanced mode used middle backend for sufficiently confident medium work. '+rr, confidence=c)
