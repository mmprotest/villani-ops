from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
import json
from pathlib import Path
from villani_ops.core.backend import Backend, select_backend, coding_backends
from villani_ops.core.task import TaskClassification
from villani_ops.llm.client import LLMClient, LLMCallResult
from .defaults import DEFAULT_PROFILES
from .prompts import SYSTEM, USER

class StrategyAttempt(BaseModel):
    backend: str; runner: str='villani_code'; max_attempts:int=1; timeout_seconds:int=1200; reason:str=''
class ExecutionStrategy(BaseModel):
    profile: str; strategy_summary: str=''; attempts: list[StrategyAttempt]=Field(default_factory=list); stop_conditions: dict[str, Any]=Field(default_factory=dict); escalation_rules:list[dict[str,Any]]=Field(default_factory=list); cost_risk_summary:str=''; warnings: list[str]=Field(default_factory=list)

class PolicyEngine:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def generate(self, classification: TaskClassification, backends: dict[str, Backend], profile: str, out_path: str|Path|None=None) -> tuple[ExecutionStrategy, LLMCallResult]:
        if profile not in DEFAULT_PROFILES: raise ValueError(f"Unknown policy profile '{profile}'")
        policy_backend=select_backend(backends,'policy'); coding=coding_backends(backends)
        if not coding: raise ValueError("No enabled coding backends configured.")
        allowed={b.name for b in coding}; max_total=DEFAULT_PROFILES[profile]['max_total_attempts']
        ctx={"classification":classification.model_dump(),"profile":profile,"profile_rules":DEFAULT_PROFILES[profile],"coding_backends":[b.redacted_dict() for b in coding]}
        result=self.client.complete_json(policy_backend, SYSTEM, USER.format(context=json.dumps(ctx, indent=2)), 'ExecutionStrategy')
        strat=ExecutionStrategy.model_validate(result.parsed_json)
        warnings=[]
        valid_by_name={b.name:b for b in coding}
        original=[a.backend for a in strat.attempts]
        kept=[]; total=0
        for a in strat.attempts:
            if a.backend not in valid_by_name:
                warnings.append(f"Removed invalid/non-coding/disabled backend {a.backend!r} from LLM strategy.")
                continue
            if a.runner!='villani_code':
                a.runner='villani_code'; warnings.append('LLM strategy runner was normalized to villani_code.')
            if total >= max_total:
                warnings.append(f'LLM strategy was truncated to max {max_total} total attempts for {profile}.')
                break
            a.max_attempts=max(1, min(a.max_attempts, max_total-total)); total += a.max_attempts; kept.append(a)
        # deterministic profile guardrails
        cheapest=sorted(coding, key=lambda b:(b.estimate_cost(1000,1000), b.output_cost_per_million, b.input_cost_per_million, -b.capability_score))[0]
        highest=sorted(coding, key=lambda b:(-b.capability_score, b.output_cost_per_million, b.name))[0]
        hard = classification.difficulty in {'hard','very_hard'} or classification.risk in {'high','critical'}
        easy = classification.difficulty in {'easy'} and classification.risk in {'low'}
        if profile=='cheap' and easy:
            kept=[StrategyAttempt(backend=cheapest.name, max_attempts=1, reason='Normalized cheap easy/low-risk profile to cheapest backend only.')]
            warnings.append('LLM strategy was normalized to enforce cheap easy/low-risk constraints.')
        elif profile=='cheap' and hard:
            # cheapest first plus at most one escalation
            esc=next((a for a in kept if a.backend!=cheapest.name), None)
            kept=[StrategyAttempt(backend=cheapest.name, max_attempts=1, reason='Normalized cheap hard/high-risk profile to start cheapest.')] + ([StrategyAttempt(backend=esc.backend, max_attempts=1, reason=esc.reason or 'One allowed escalation.')] if esc else [])
            warnings.append('LLM strategy was normalized to one cheap escalation path.')
        if profile=='cheap' and kept and kept[0].backend != cheapest.name:
            kept.insert(0, StrategyAttempt(backend=cheapest.name, max_attempts=1, reason='Normalized cheap profile to start with cheapest eligible backend.'))
            warnings.append('LLM strategy was normalized to enforce cheap policy constraints.')
        if profile=='balanced' and kept and easy and kept[0].backend != cheapest.name:
            kept.insert(0, StrategyAttempt(backend=cheapest.name, max_attempts=1, reason='Normalized balanced easy/low-risk profile to start cheaper.'))
            warnings.append('LLM strategy was normalized to enforce balanced easy start constraints.')
        if profile=='quality' and hard and kept and kept[0].backend != highest.name:
            kept.insert(0, StrategyAttempt(backend=highest.name, max_attempts=1, reason='Normalized quality hard/high-risk profile to start with highest-capability backend.'))
            warnings.append('LLM strategy was normalized to enforce quality policy constraints.')
        # enforce total again after insertions
        final=[]; total=0
        for a in kept:
            if a.backend not in valid_by_name: continue
            if total>=max_total: break
            a.max_attempts=max(1, min(a.max_attempts, max_total-total)); total+=a.max_attempts; final.append(a)
        strat.attempts=final; strat.profile=profile; strat.warnings.extend(warnings)
        if original != [a.backend for a in strat.attempts] and not warnings:
            strat.warnings.append('LLM strategy was normalized to enforce policy constraints.')
        if not strat.attempts: raise ValueError("Policy engine produced no valid attempts")
        if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
        return strat, result
