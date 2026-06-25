from __future__ import annotations
from .graph import OrchestrationGraph
from .nodes import OrchestrationNode

class GraphScheduler:
    def can_run(self, node: OrchestrationNode, graph: OrchestrationGraph) -> bool:
        return node.status in {'pending','ready'} and not graph.has_failed_required_dependency(node) and all(graph.dependency_satisfied(d, node) for d in node.dependencies)
    def next_ready_nodes(self, graph: OrchestrationGraph) -> list[OrchestrationNode]:
        for node in graph.nodes:
            if node.status == 'pending' and graph.has_failed_required_dependency(node):
                graph.mark_skipped(node.id, 'Required dependency failed or was unsafe to skip.')
            elif node.status == 'pending' and self.can_run(node, graph):
                node.status='ready'
        return [n for n in graph.nodes if self.can_run(n, graph)]
