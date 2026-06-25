from __future__ import annotations
from typing import Protocol, Mapping, Any
from pydantic import BaseModel
from villani_ops.core.backend import Backend as BackendConfig
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.nodes import OrchestrationNode, NodeResult

class BackendSelection(BaseModel):
    backend_name: str
    backend: BackendConfig | None = None
    reason: str
    confidence: float = 0.0
    escalated: bool = False
    escalation_reason: str | None = None

class ExecutionPolicy(Protocol):
    mode: str
    def select_backend(self, *, node: OrchestrationNode, backends: Mapping[str, BackendConfig], task_context: TaskContext, prior_results: list[NodeResult] | None = None) -> BackendSelection: ...

def enabled_backends(backends: Mapping[str, BackendConfig]) -> list[tuple[str, BackendConfig]]:
    vals=[(n,b) for n,b in backends.items() if getattr(b,'enabled',True)]
    if not vals: raise ValueError('No enabled backends configured')
    return vals

def required_role_for_node(node: OrchestrationNode) -> str:
    if node.kind == 'code': return 'coding'
    if node.kind in {'review','final_review'}: return 'review'
    if node.kind in {'classify'}: return 'classification'
    if node.kind in {'investigate'}: return 'investigation'
    if node.kind in {'plan','decompose'}: return 'policy'
    if node.kind in {'select','verify','integration_validate'}: return 'selection'
    if node.kind in {'integrate','integration_repair'}: return 'coding'
    return node.kind

def filter_backends_for_role(backends: Mapping[str, BackendConfig], role: str) -> dict[str, BackendConfig]:
    vals={n:b for n,b in backends.items() if getattr(b,'enabled',True) and role in (getattr(b,'roles',[]) or [])}
    return vals

def select_backend_for_role(role: str, policy: str, registry: Mapping[str, BackendConfig], task_context: TaskContext | None = None, *, preferred: str = 'highest') -> tuple[str, BackendConfig, str]:
    requested_role=role
    candidates=filter_backends_for_role(registry, role)
    fallback_role=None
    reason_extra=''
    if not candidates and role in {'selection', 'policy'}:
        fallback_order=('review', 'policy') if role == 'selection' else ('review',)
        for alt in fallback_order:
            candidates=filter_backends_for_role(registry, alt)
            if candidates:
                fallback_role=alt
                reason_extra=f' requested_role={requested_role}; fallback_role={alt}; reason=no enabled requested-role backend exists;'
                break
    if not candidates:
        raise ValueError(f"No enabled backends support required role {role!r}.")
    ordered=sort_by_capability(candidates)
    if preferred == 'lowest': n,b=ordered[0]
    elif preferred == 'middle': n,b=ordered[len(ordered)//2]
    else: n,b=sorted(candidates.items(), key=lambda x:(-x[1].capability_score, x[0]))[0]
    if fallback_role:
        reason=f'Fallback role selection:{reason_extra} selected_backend={n}; selected by {preferred} capability.'
    else:
        reason=f'Filtered by required role {role} before {preferred} capability selection.'
    return n,b,reason

def sort_by_capability(backends: Mapping[str, BackendConfig]) -> list[tuple[str, BackendConfig]]:
    return sorted(enabled_backends(backends), key=lambda x:(x[1].capability_score, x[0]))
def highest_capability(backends): return sorted(enabled_backends(backends), key=lambda x:(-x[1].capability_score, x[0]))[0]
def lowest_capability(backends): return sort_by_capability(backends)[0]
def middle_capability(backends):
    vals=sort_by_capability(backends); return vals[len(vals)//2]
def role_highest(node, backends, task_context=None):
    return select_backend_for_role(required_role_for_node(node), 'policy', backends, task_context, preferred='highest')
def role_lowest(node, backends, task_context=None):
    return select_backend_for_role(required_role_for_node(node), 'policy', backends, task_context, preferred='lowest')
def role_middle(node, backends, task_context=None):
    return select_backend_for_role(required_role_for_node(node), 'policy', backends, task_context, preferred='middle')

def _strongest_signal(values: list[str], order: list[str]) -> str:
    ranks={v:i for i,v in enumerate(order)}; return max(values, key=lambda v:ranks.get(v,0)) if values else order[0]

def estimate_node_difficulty(node: OrchestrationNode, task_context: TaskContext) -> tuple[str,str,float]:
    diffs=[node.difficulty]; risks=[node.risk]; confs=[]
    for src in [task_context.classification, task_context.investigation, task_context.plan, task_context.decomposition]:
        if not src: continue
        d=src.get('difficulty') or src.get('expected_difficulty')
        r=src.get('risk') or src.get('overall_risk')
        if d in {'easy','medium','hard'}: diffs.append(d)
        if r in {'low','medium','high'}: risks.append(r)
        if isinstance(src.get('confidence'), (int,float)): confs.append(float(src['confidence']))
        if src is task_context.investigation and isinstance(src.get('relevant_files'), list) and len(src['relevant_files']) >= 6: diffs.append('hard')
    if node.kind in {'review','select'} and 'easy' not in diffs: diffs.append('medium')
    if node.kind in {'code','verify','select'} and any('blocker' in str(x).lower() for x in (task_context.plan or {}).get('risks', [])): risks.append('high')
    if task_context.decomposition and len(task_context.decomposition.get('subtasks') or []) > 2: diffs.append('hard')
    difficulty=_strongest_signal(diffs, ['unknown','easy','medium','hard'])
    risk=_strongest_signal(risks, ['unknown','low','medium','high'])
    confidence=node.confidence if node.confidence is not None else (min(confs) if confs else task_context.confidence)
    return difficulty, risk, float(confidence or 0.0)

def prior_forces_escalation(prior_results: list[NodeResult] | None) -> bool:
    return any(
        r.has_failure
        or r.has_review_blocker
        or r.has_acceptance_blocker
        or r.status in {'failed', 'uncertain'}
        for r in (prior_results or [])
    )
