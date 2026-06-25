from __future__ import annotations
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMClient, LLMCallResult
from .graph import OrchestrationGraph
from .nodes import OrchestrationNode

class PlanResult(BaseModel):
    summary: str
    strategy: Literal['single_task','parallel_candidates','decompose_then_execute'] = 'parallel_candidates'
    should_decompose: bool = False
    decomposition_reason: str | None = None
    candidate_attempts: int
    risks: list[str] = Field(default_factory=list)
    expected_difficulty: Literal['easy','medium','hard','unknown'] = 'unknown'
    confidence: float = 0.0
    fallback_used: bool = False

class Subtask(BaseModel):
    id: str
    title: str
    objective: str
    success_criteria: str | None = None
    relevant_files: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    expected_difficulty: Literal['easy','medium','hard','unknown'] = 'unknown'
    risk: Literal['low','medium','high','unknown'] = 'unknown'
    confidence: float = 0.0

class DecompositionResult(BaseModel):
    should_use_decomposition: bool
    reason: str
    subtasks: list[Subtask] = Field(default_factory=list)
    merge_strategy: str | None = None
    confidence: float = 0.0
    advisory_only: bool = True
    fallback_used: bool = False

def build_fixed_graph(candidate_attempts: int, runner: str = 'villani-code', *, run_id: str='', mode: str='performance', classify: bool=True, include_decompose: bool=False) -> OrchestrationGraph:
    nodes=[]
    deps=[]
    if classify:
        nodes.append(OrchestrationNode(id='classify', kind='classify', objective='Classify task difficulty, risk, and category.')); deps=['classify']
    nodes.append(OrchestrationNode(id='investigate', kind='investigate', objective='Understand task, repo context, risks, likely files, and validation plan.', dependencies=deps))
    nodes.append(OrchestrationNode(id='plan', kind='plan', objective='Plan strategy, candidate count, risks, and decomposition choice.', dependencies=['investigate']))
    code_dep='plan'
    if include_decompose:
        nodes.append(OrchestrationNode(id='decompose', kind='decompose', objective='Break the task into advisory subtasks if useful.', dependencies=['plan']))
        code_dep='decompose'
    for i in range(1, candidate_attempts+1):
        aid=f'attempt_{i:03d}'
        nodes.append(OrchestrationNode(id=f'code_{aid}', kind='code', objective=f'Generate independent candidate patch {i}.', dependencies=[code_dep], parallel_group='candidate_code', runner=runner))
        nodes.append(OrchestrationNode(id=f'review_{aid}', kind='review', objective=f'Review candidate patch {i}.', dependencies=[f'code_{aid}'], parallel_group='candidate_review'))
    nodes.append(OrchestrationNode(id='select', kind='select', objective='Select the best eligible candidate.', dependencies=[f'review_attempt_{i:03d}' for i in range(1,candidate_attempts+1)]))
    nodes.append(OrchestrationNode(id='verify', kind='verify', objective='Make final acceptance decision and write artifacts.', dependencies=['select']))
    edges=[(d,n.id) for n in nodes for d in n.dependencies]
    return OrchestrationGraph(run_id=run_id, mode=mode, runner=runner, nodes=nodes, edges=edges)

class Planner:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def plan(self, *, task, classification, investigation, repo_summary: str|None, candidate_attempts: int, mode: str, backend_name: str, backend: Backend, run_dir: Path) -> tuple[PlanResult, LLMCallResult|None]:
        ctx={'task':task.model_dump(mode='json'),'classification':classification,'investigation':investigation,'repo_summary':repo_summary,'candidate_attempts':candidate_attempts,'mode':mode}
        try:
            call=self.client.complete_json(backend, 'Return JSON matching PlanResult.', json.dumps(ctx, indent=2)[:80000], 'PlanResult')
            plan=PlanResult.model_validate(call.parsed_json)
            plan.candidate_attempts=max(1, min(8, int(plan.candidate_attempts or candidate_attempts)))
        except Exception as e:
            call=locals().get('call')
            plan=PlanResult(summary=f'Planner fallback used: {e}', strategy='parallel_candidates', should_decompose=False, candidate_attempts=candidate_attempts, expected_difficulty='unknown', confidence=0.0, fallback_used=True)
        (run_dir/'plan.json').write_text(plan.model_dump_json(indent=2)); (run_dir/'plan.raw.txt').write_text((call.raw_text if call else '') or '')
        return plan, call if 'call' in locals() else None
    def decompose(self, *, task, plan: PlanResult, investigation, backend: Backend, run_dir: Path) -> tuple[DecompositionResult, LLMCallResult|None]:
        ctx={'task':task.model_dump(mode='json'),'plan':plan.model_dump(mode='json'),'investigation':investigation}
        try:
            call=self.client.complete_json(backend, 'Return JSON matching DecompositionResult. Decomposition is advisory only.', json.dumps(ctx, indent=2)[:80000], 'DecompositionResult')
            dec=DecompositionResult.model_validate(call.parsed_json)
            dec.advisory_only=True
        except Exception as e:
            call=locals().get('call')
            dec=DecompositionResult(should_use_decomposition=False, reason=f'Decomposition fallback used: {e}', subtasks=[], confidence=0.0, advisory_only=True, fallback_used=True)
        (run_dir/'decomposition.json').write_text(dec.model_dump_json(indent=2)); (run_dir/'decomposition.raw.txt').write_text((call.raw_text if call else '') or '')
        return dec, call if 'call' in locals() else None
