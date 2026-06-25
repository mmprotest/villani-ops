from __future__ import annotations
import json
from pathlib import Path
from typing import Literal, Any
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
    planner_normalized: bool = False
    planner_normalization_notes: list[str] = Field(default_factory=list)
    planner_fallback_used: bool = False
    planner_fallback_reason: str | None = None
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
    planner_normalized: bool = False
    planner_normalization_notes: list[str] = Field(default_factory=list)
    planner_fallback_used: bool = False
    planner_fallback_reason: str | None = None
    fallback_used: bool = False


def _as_list(value: Any) -> list[str]:
    if value is None: return []
    if isinstance(value, list): return [str(x) for x in value if str(x).strip()]
    if isinstance(value, str): return [value.strip()] if value.strip() else []
    return [str(value)]

def normalize_plan_payload(payload: dict[str, Any], *, requested_candidate_attempts: int) -> tuple[dict[str, Any], list[str]]:
    data=dict(payload or {}); notes=[]
    def map_alias(alias, target):
        if target not in data and alias in data:
            data[target]=data[alias]; notes.append(f"Mapped {alias} to {target}")
    map_alias('approach','summary'); map_alias('strategy_summary','summary'); map_alias('execution_strategy','strategy'); map_alias('strategy_name','strategy')
    map_alias('requires_decomposition','should_decompose'); map_alias('num_candidates','candidate_attempts'); map_alias('candidates','candidate_attempts'); map_alias('attempts','candidate_attempts'); map_alias('candidate_count','candidate_attempts'); map_alias('difficulty','expected_difficulty')
    if 'risks' not in data:
        for a in ('risk_factors','warnings'):
            if a in data: data['risks']=data[a]; notes.append(f"Mapped {a} to risks"); break
    if 'should_decompose' not in data and 'decompose' in data: data['should_decompose']=data['decompose']; notes.append('Mapped decompose to should_decompose')
    if 'decomposition' in data and 'should_decompose' not in data:
        v=data['decomposition']; data['should_decompose']=bool(v) if not isinstance(v, str) else v.lower() in {'true','yes','needed','required'}; notes.append('Mapped decomposition to should_decompose')
    if 'decomposition' in data and 'decomposition_reason' not in data and isinstance(data['decomposition'], str): data['decomposition_reason']=data['decomposition']; notes.append('Mapped decomposition to decomposition_reason')
    if not str(data.get('summary') or '').strip() and isinstance(data.get('plan'), list): data['summary']='; '.join(str(x) for x in data['plan'] if str(x).strip()); notes.append('Mapped plan list to summary')
    elif not str(data.get('summary') or '').strip() and isinstance(data.get('plan'), str): data['summary']=data['plan']; notes.append('Mapped plan to summary')
    if not str(data.get('summary') or '').strip() and isinstance(data.get('steps'), list): data['summary']='; '.join(str(x) for x in data['steps'] if str(x).strip()); notes.append('Mapped steps to summary')
    if not str(data.get('summary') or '').strip(): return data, notes
    if not data.get('strategy'): data['strategy']='parallel_candidates'; notes.append('Defaulted strategy to parallel_candidates')
    maps={'parallel':'parallel_candidates','multi_candidate':'parallel_candidates','multiple_candidates':'parallel_candidates','single':'single_task','decompose':'decompose_then_execute'}
    if data.get('strategy') in maps: data['strategy']=maps[data['strategy']]; notes.append('Mapped strategy alias')
    if data.get('strategy') not in {'single_task','parallel_candidates','decompose_then_execute'}: data['strategy']='parallel_candidates'; notes.append('Defaulted invalid strategy to parallel_candidates')
    if not data.get('candidate_attempts'): data['candidate_attempts']=requested_candidate_attempts; notes.append('Defaulted candidate_attempts to requested value')
    try:
        c=int(data['candidate_attempts']); data['candidate_attempts']=max(1,min(8,c))
        if c!=data['candidate_attempts']: notes.append('Clamped candidate_attempts to 1..8')
    except Exception: data['candidate_attempts']=requested_candidate_attempts; notes.append('Defaulted invalid candidate_attempts to requested value')
    if 'should_decompose' not in data or data.get('should_decompose') is None: data['should_decompose']=data.get('strategy')=='decompose_then_execute'; notes.append('Defaulted should_decompose from strategy')
    if isinstance(data.get('risks'), str): data['risks']=[data['risks']]; notes.append('Converted risks string to list')
    data['risks']=_as_list(data.get('risks'))
    try: conf=float(data.get('confidence',0.0)); data['confidence']=max(0.0,min(1.0,conf));
    except Exception: data['confidence']=0.0
    if data.get('expected_difficulty') not in {'easy','medium','hard','unknown'}: data['expected_difficulty']='unknown'; notes.append('Defaulted invalid expected_difficulty to unknown')
    return data, notes

def build_fixed_graph(candidate_attempts: int, runner: str = 'villani-code', *, run_id: str='', mode: str='performance', classify: bool=True, include_decompose: bool=True) -> OrchestrationGraph:
    nodes=[]
    deps=[]
    if classify:
        nodes.append(OrchestrationNode(id='classify', kind='classify', objective='Classify task difficulty, risk, and category.')); deps=['classify']
    nodes.append(OrchestrationNode(id='investigate', kind='investigate', objective='Understand task, repo context, risks, likely files, and validation plan.', dependencies=deps))
    nodes.append(OrchestrationNode(id='plan', kind='plan', objective='Plan strategy, candidate count, risks, and decomposition choice.', dependencies=['investigate']))
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
        normalized_payload=None; notes=[]
        try:
            call=self.client.complete_json(backend, 'Return JSON matching PlanResult.', json.dumps(ctx, indent=2)[:80000], 'PlanResult', estimate_cost=(mode != 'performance'))
            try:
                plan=PlanResult.model_validate(call.parsed_json)
                plan.candidate_attempts=max(1, min(8, int(plan.candidate_attempts or candidate_attempts)))
                (run_dir/'plan_normalized.json').write_text(json.dumps({'normalized': False, 'notes': [], 'payload': plan.model_dump(mode='json')}, indent=2))
            except Exception as original_error:
                try:
                    normalized_payload, notes = normalize_plan_payload(call.parsed_json if isinstance(call.parsed_json, dict) else {}, requested_candidate_attempts=candidate_attempts)
                    plan=PlanResult.model_validate(normalized_payload)
                    plan.planner_normalized=True; plan.planner_normalization_notes=notes; plan.planner_fallback_used=False; plan.planner_fallback_reason=None
                    (run_dir/'plan_normalized.json').write_text(json.dumps({'normalized': True, 'payload': normalized_payload, 'notes': notes}, indent=2))
                except Exception:
                    raise original_error
        except Exception as e:
            call=locals().get('call')
            reason=str(e)
            plan=PlanResult(summary=f'Planner fallback used: {reason}', strategy='parallel_candidates', should_decompose=False, candidate_attempts=candidate_attempts, expected_difficulty='unknown', confidence=0.0, fallback_used=True, planner_fallback_used=True, planner_fallback_reason=reason)
            (run_dir/'plan_normalized.json').write_text(json.dumps({'normalized': False, 'payload': getattr(call, 'parsed_json', {}) if call else {}, 'notes': [], 'error': reason}, indent=2, default=str))
        (run_dir/'plan.raw.txt').write_text((call.raw_text if call else f'ERROR: {plan.planner_fallback_reason or ""}') or '')
        (run_dir/'plan.json').write_text(plan.model_dump_json(indent=2))
        return plan, call if 'call' in locals() else None
    def decompose(self, *, task, plan: PlanResult, investigation, backend: Backend, run_dir: Path, estimate_cost: bool = True) -> tuple[DecompositionResult, LLMCallResult|None]:
        ctx={'task':task.model_dump(mode='json'),'plan':plan.model_dump(mode='json'),'investigation':investigation}
        try:
            call=self.client.complete_json(backend, 'Return JSON matching DecompositionResult. Decomposition is advisory only.', json.dumps(ctx, indent=2)[:80000], 'DecompositionResult', estimate_cost=estimate_cost)
            dec=DecompositionResult.model_validate(call.parsed_json)
            dec.advisory_only=True
        except Exception as e:
            call=locals().get('call')
            dec=DecompositionResult(should_use_decomposition=False, reason=f'Decomposition fallback used: {e}', subtasks=[], confidence=0.0, advisory_only=True, fallback_used=True)
        (run_dir/'decomposition.json').write_text(dec.model_dump_json(indent=2)); (run_dir/'decomposition.raw.txt').write_text((call.raw_text if call else '') or '')
        return dec, call if 'call' in locals() else None
