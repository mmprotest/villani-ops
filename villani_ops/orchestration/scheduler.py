from __future__ import annotations
from .graph import OrchestrationGraph
from .nodes import OrchestrationNode

class GraphScheduler:
    def can_run(self, node: OrchestrationNode, graph: OrchestrationGraph) -> bool:
        return node.status in {'pending','ready'} and all(graph.get(d).status == 'succeeded' for d in node.dependencies)
    def next_ready_nodes(self, graph: OrchestrationGraph) -> list[OrchestrationNode]:
        for node in graph.nodes:
            if node.status == 'pending' and any(graph.get(d).status in {'failed','skipped'} for d in node.dependencies):
                graph.mark_skipped(node.id, 'Dependency failed or was skipped.')
            elif node.status == 'pending' and self.can_run(node, graph):
                node.status='ready'
        return [n for n in graph.nodes if self.can_run(n, graph)]
