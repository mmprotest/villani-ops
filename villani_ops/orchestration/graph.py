from __future__ import annotations
from pydantic import BaseModel, Field
from .nodes import OrchestrationNode

class OrchestrationGraph(BaseModel):
    nodes: list[OrchestrationNode] = Field(default_factory=list)

    def get(self, node_id: str) -> OrchestrationNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def summary(self) -> dict:
        return {'node_count': len(self.nodes), 'nodes': [{'id': n.id, 'kind': n.kind, 'assigned_backend': n.assigned_backend, 'status': n.status, 'dependencies': n.dependencies, 'parallel_group': n.parallel_group, 'runner': n.runner} for n in self.nodes]}
