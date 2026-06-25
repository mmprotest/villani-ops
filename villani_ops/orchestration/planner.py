from __future__ import annotations
from .graph import OrchestrationGraph
from .nodes import OrchestrationNode

def build_fixed_graph(candidate_attempts: int, runner: str = 'villani-code') -> OrchestrationGraph:
    nodes=[OrchestrationNode(id='investigate', kind='investigate', objective='Understand the task, repo context, risks, likely files, and validation plan.'), OrchestrationNode(id='plan', kind='plan', objective='Plan serial/parallel candidate execution.', dependencies=['investigate'])]
    for i in range(1, candidate_attempts+1):
        aid=f'attempt_{i:03d}'
        nodes.append(OrchestrationNode(id=f'code_{aid}', kind='code', objective=f'Generate independent candidate patch {i}.', dependencies=['plan'], parallel_group='candidates', runner=runner))
        nodes.append(OrchestrationNode(id=f'review_{aid}', kind='review', objective=f'Review candidate patch {i}.', dependencies=[f'code_{aid}']))
    nodes.append(OrchestrationNode(id='select', kind='select', objective='Select the best eligible candidate.', dependencies=[f'review_attempt_{i:03d}' for i in range(1,candidate_attempts+1)]))
    nodes.append(OrchestrationNode(id='verify', kind='verify', objective='Make final acceptance decision and write artifacts.', dependencies=['select']))
    return OrchestrationGraph(nodes=nodes)
