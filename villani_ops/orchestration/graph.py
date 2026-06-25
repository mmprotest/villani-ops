from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field
from .nodes import OrchestrationNode

def _now() -> str: return datetime.now(timezone.utc).isoformat()

class OrchestrationGraph(BaseModel):
    run_id: str = ''
    mode: str = 'performance'
    runner: str = 'villani-code'
    nodes: list[OrchestrationNode] = Field(default_factory=list)
    edges: list[tuple[str, str]] = Field(default_factory=list)

    def get(self, node_id: str) -> OrchestrationNode:
        for node in self.nodes:
            if node.id == node_id: return node
        raise KeyError(node_id)
    get_node = get

    def update_node(self, node_id: str, **updates: Any) -> OrchestrationNode:
        n=self.get(node_id)
        for k,v in updates.items(): setattr(n,k,v)
        return n
    def dependency_satisfied(self, dep_id: str, node: OrchestrationNode | None = None) -> bool:
        dep = self.get(dep_id)
        if dep.status == 'succeeded':
            return True
        if dep.status == 'skipped' and dep.kind == 'decompose':
            return True
        if dep.status == 'skipped' and dep.kind == 'integration_repair':
            return True
        if node is not None and node.kind == 'review' and dep.kind == 'code' and dep.status in {'failed', 'skipped'}:
            return True
        if node is not None and node.kind == 'select' and dep.kind == 'review' and dep.status in {'succeeded', 'failed', 'skipped'}:
            return True
        return False
    def has_failed_required_dependency(self, node: OrchestrationNode) -> bool:
        return any(not self.dependency_satisfied(d, node) and self.get(d).status in {'failed','skipped'} for d in node.dependencies)
    def ready_nodes(self) -> list[OrchestrationNode]:
        return [n for n in self.nodes if n.status in {'pending','ready'} and not self.has_failed_required_dependency(n) and all(self.dependency_satisfied(d, n) for d in n.dependencies)]
    def is_terminal(self) -> bool:
        return all(n.status in {'succeeded','failed','skipped'} for n in self.nodes)
    def mark_running(self, node_id: str) -> None:
        self.update_node(node_id, status='running', started_at=_now(), error=None)
    def mark_succeeded(self, node_id: str, *, summary: str | None=None, artifacts: dict[str,str] | None=None, confidence: float | None=None, difficulty: str | None=None, risk: str | None=None) -> None:
        n=self.get(node_id); n.status='succeeded'; n.completed_at=_now(); n.result_summary=summary or n.result_summary; n.error=None
        if artifacts: n.artifacts.update(artifacts)
        if confidence is not None: n.confidence=confidence
        if difficulty is not None: n.difficulty=difficulty
        if risk is not None: n.risk=risk
    def mark_failed(self, node_id: str, error: str, *, summary: str | None=None) -> None:
        n=self.get(node_id); n.status='failed'; n.completed_at=_now(); n.error=error; n.result_summary=summary or error
    def mark_skipped(self, node_id: str, reason: str) -> None:
        n=self.get(node_id); n.status='skipped'; n.completed_at=_now(); n.error=reason; n.result_summary=reason
    def to_json(self) -> str:
        return self.model_dump_json(indent=2)
    def terminal_nodes(self) -> list[OrchestrationNode]:
        return [n for n in self.nodes if n.status in {'succeeded','failed','skipped'}]
    def pending_nodes(self) -> list[OrchestrationNode]:
        return [n for n in self.nodes if n.status in {'pending','ready','running'}]
    def summary(self) -> dict:
        return {'run_id':self.run_id,'mode':self.mode,'runner':self.runner,'node_count':len(self.nodes),'nodes':[n.model_dump(mode='json') for n in self.nodes]}
    def write(self, path) -> None:
        from pathlib import Path
        Path(path).write_text(self.to_json(), encoding="utf-8")
