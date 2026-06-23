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
    profile: str; strategy_summary: str=''; attempts: list[StrategyAttempt]=Field(default_factory=list); stop_conditions: dict[str, Any]=Field(default_factory=dict); escalation_rules:list[dict[str,Any]]=Field(default_factory=list); cost_risk_summary:str=''

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
        kept=[]; total=0
        for a in strat.attempts:
            if a.backend not in allowed: raise ValueError(f"Policy selected invalid/non-coding/disabled backend '{a.backend}'")
            if a.runner!='villani_code': a.runner='villani_code'
            if total >= max_total: break
            a.max_attempts=max(1, min(a.max_attempts, max_total-total)); total += a.max_attempts; kept.append(a)
        strat.attempts=kept; strat.profile=profile
        if not strat.attempts: raise ValueError("Policy engine produced no valid attempts")
        if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
        return strat, result
