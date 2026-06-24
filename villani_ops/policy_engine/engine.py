from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field, ConfigDict
import json
from pathlib import Path
from villani_ops.core.backend import Backend, select_backend, coding_backends
from villani_ops.core.task import TaskClassification
from villani_ops.llm.client import LLMClient, LLMCallResult, LLMCallError
from .defaults import DEFAULT_PROFILES
from .prompts import SYSTEM, USER



def normalize_execution_strategy_payload(raw: dict, requested_profile: str) -> dict:
    payload=dict(raw or {})
    hint=payload.get("profile") or payload.get("strategy_name") or payload.get("policy") or payload.get("selected_profile") or payload.get("profile_name")
    if hint is not None:
        payload["_model_profile_hint"]=hint
    for alias in ("strategy_name", "policy", "selected_profile", "profile_name"):
        payload.pop(alias, None)
    payload["profile"]=requested_profile
    if "planned_attempts" in payload and "attempts" not in payload:
        payload["attempts"]=payload["planned_attempts"]
    if "planned_attempts" not in payload and "attempts" in payload:
        payload["planned_attempts"]=payload["attempts"]
    if "attempts" not in payload and payload.get("backend_order"):
        payload["attempts"]=[{"backend": b, "max_attempts": 1, "reason": "Created from backend_order."} for b in payload["backend_order"]]
    if "stop_conditions" not in payload:
        payload["stop_conditions"]={}
    if "stop_condition" not in payload:
        payload["stop_condition"]="first_accepted"
    payload.setdefault("warnings", [])
    if "strategy_summary" not in payload:
        payload["strategy_summary"]=payload.get("rationale") or payload.get("reasoning_summary") or ""
    return payload

def _write_controller_error(run_dir: Path|None, phase: str, backend: Backend|None, schema: str, result: LLMCallResult|None, parse_error=None, validation_error=None, normalized_payload=None):
    if not run_dir: return
    d=Path(run_dir)/"controller_calls"; d.mkdir(parents=True, exist_ok=True)
    data={"phase":phase,"backend":getattr(backend,"name",None),"schema":schema,"url":getattr(result,"url",None),"model":getattr(result,"model",getattr(backend,"model",None)),"max_tokens":getattr(result,"max_tokens",getattr(backend,"max_tokens",None)),"http_status":getattr(result,"http_status",None),"finish_reason":getattr(result,"finish_reason",None),"usage":getattr(result,"usage",{}) if result else {},"message_content":getattr(result,"raw_text",None),"reasoning_content":getattr(result,"reasoning_content",None),"raw_response":getattr(result,"raw_response",{}) if result else {},"parse_error":parse_error,"validation_error":validation_error,"normalized_payload":normalized_payload or {}}
    (d/f"{phase}_error.json").write_text(json.dumps(data, indent=2, default=str))

def build_deterministic_fallback_strategy(classification: TaskClassification, backends: dict[str, Backend], profile: str, reason: str) -> ExecutionStrategy:
    coding=coding_backends(backends)
    max_total=DEFAULT_PROFILES[profile]["max_total_attempts"]
    def cost(b): return b.estimate_cost(1000,1000)
    cheapest=sorted(coding, key=lambda b:(cost(b), b.output_cost_per_million, b.input_cost_per_million, -b.capability_score, b.name))[0]
    strongest=sorted(coding, key=lambda b:(-b.capability_score, cost(b), b.name))[0]
    attempts=[]
    if profile=="cheap":
        attempts=[StrategyAttempt(backend=cheapest.name, max_attempts=1, reason="Deterministic fallback: cheapest coding backend.")]
    elif profile=="balanced":
        if cheapest.name==strongest.name:
            attempts=[StrategyAttempt(backend=cheapest.name, max_attempts=min(2,max_total), reason="Deterministic fallback: only coding backend.")]
        else:
            attempts=[StrategyAttempt(backend=cheapest.name, max_attempts=1, reason="Deterministic fallback: cheapest viable backend first."), StrategyAttempt(backend=strongest.name, max_attempts=1, reason="Deterministic fallback: escalate to strongest backend.")]
    else:
        ordered=[]
        for b in [strongest]+sorted(coding, key=lambda b:(-b.capability_score, cost(b), b.name)):
            if b.name not in [x.name for x in ordered]: ordered.append(b)
        remaining=min(3,max_total)
        for b in ordered:
            if remaining<=0: break
            attempts.append(StrategyAttempt(backend=b.name, max_attempts=1, reason="Deterministic fallback: quality profile escalation path.")); remaining-=1
        if remaining and attempts: attempts[0].max_attempts+=remaining
    return ExecutionStrategy(profile=profile, strategy_summary="Policy generation failed validation, so Villani Ops used deterministic fallback policy.", attempts=attempts, warnings=[f"Policy generation failed validation, so Villani Ops used deterministic fallback policy. {reason}"], stop_conditions={"mode":"first_accepted"})

class StrategyAttempt(BaseModel):
    backend: str; runner: str='villani_code'; max_attempts:int=1; timeout_seconds:int=1200; reason:str=''
class ExecutionStrategy(BaseModel):
    model_config = ConfigDict(extra="ignore")
    profile: str; strategy_summary: str=''; attempts: list[StrategyAttempt]=Field(default_factory=list); stop_conditions: dict[str, Any]=Field(default_factory=dict); escalation_rules:list[dict[str,Any]]=Field(default_factory=list); cost_risk_summary:str=''; warnings: list[str]=Field(default_factory=list); backend_rankings: list[dict[str, Any]]=Field(default_factory=list)

class PolicyEngine:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def generate(self, classification: TaskClassification, backends: dict[str, Backend], profile: str, out_path: str|Path|None=None) -> tuple[ExecutionStrategy, LLMCallResult]:
        if profile not in DEFAULT_PROFILES: raise ValueError(f"Unknown policy profile '{profile}'")
        policy_backend=select_backend(backends,'policy'); coding=coding_backends(backends)
        if not coding: raise ValueError("No enabled coding backends configured.")
        allowed={b.name for b in coding}; max_total=DEFAULT_PROFILES[profile]['max_total_attempts']
        ctx={"classification":classification.model_dump(),"profile":profile,"profile_rules":DEFAULT_PROFILES[profile],"coding_backends":[b.redacted_dict() for b in coding]}
        try:
            result=self.client.complete_json(policy_backend, SYSTEM, USER.format(context=json.dumps(ctx, indent=2)), 'ExecutionStrategy')
        except LLMCallError as e:
            _write_controller_error(Path(out_path).parent if out_path else None, 'policy', policy_backend, 'ExecutionStrategy', e.result, parse_error=e.parse_error)
            strat=build_deterministic_fallback_strategy(classification, backends, profile, str(e))
            if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
            return strat, e.result or LLMCallResult(parsed_json={}, raw_text='', backend_name=policy_backend.name, model=policy_backend.model)
        normalized=normalize_execution_strategy_payload(result.parsed_json, profile)
        try:
            strat=ExecutionStrategy.model_validate(normalized)
        except Exception as e:
            run_dir=Path(out_path).parent if out_path else None
            _write_controller_error(run_dir, "policy", policy_backend, "ExecutionStrategy", result, validation_error=str(e), normalized_payload=normalized)
            strat=build_deterministic_fallback_strategy(classification, backends, profile, str(e))
            if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
            return strat, result
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

        def est(b):
            budget=b.max_tokens or 8000
            mult={'easy':0.5,'medium':1.0,'hard':1.5,'very_hard':2.0}.get(classification.difficulty,1.0)
            return b.estimate_cost(int(budget*mult), int(budget*0.35*mult))
        rankings=[]
        for b in coding:
            c=max(est(b), 0.000001); cap=max(b.capability_score,1)
            if profile=='cheap': key=(c, -cap)
            elif profile=='quality': key=(-cap, c)
            else: key=(-(cap/c), c)
            rankings.append((key, {'backend':b.name,'capability_score':b.capability_score,'estimated_attempt_cost':round(c,6),'expected_cost_rank':0,'rank_reason':f'{profile} policy ranking using capability {b.capability_score} and estimated attempt cost ${c:.6f}'}))
        rankings=[r for _,r in sorted(rankings, key=lambda x:x[0])]
        for i,r in enumerate(rankings,1): r['expected_cost_rank']=i
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
        strat.attempts=final; strat.profile=profile; strat.backend_rankings=rankings; strat.warnings.extend(warnings)
        if original != [a.backend for a in strat.attempts] and not warnings:
            strat.warnings.append('LLM strategy was normalized to enforce policy constraints.')
        if not strat.attempts:
            reason="Policy engine produced no valid attempts"
            _write_controller_error(Path(out_path).parent if out_path else None, "policy", policy_backend, "ExecutionStrategy", result, validation_error=reason, normalized_payload=normalized)
            strat=build_deterministic_fallback_strategy(classification, backends, profile, reason)
        if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
        return strat, result
