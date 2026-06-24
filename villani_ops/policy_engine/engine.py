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




def _first_string(mapping: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value=mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _coerce_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


_BACKEND_ALIASES=("backend", "assigned_backend", "backend_name", "model_backend", "model", "model_name", "backend_id", "name")
_RUNNER_ALIASES=("runner", "runner_name", "runner_type", "agent", "tool", "executor")
_MAX_ATTEMPT_ALIASES=("max_attempts", "attempts", "num_attempts", "n_attempts", "retries", "max_retries", "tries")
_TIMEOUT_ALIASES=("timeout_seconds", "timeout", "max_seconds", "time_limit_seconds", "agent_time_limit_seconds")
_REASON_ALIASES=("reason", "rationale", "instructions", "description", "summary", "why", "phase", "name")
_ATTEMPT_SOURCES=("attempts", "planned_attempts", "plan", "steps", "execution_steps", "execution_plan", "phases", "execution_phases", "backend_order", "backend_sequence", "model_sequence", "backend_plan")
_SEQUENCE_SOURCES=("backend_order", "backend_sequence", "model_sequence", "backend_plan")
_PLANNING_FIELDS=("backend_sequence", "backend_order", "execution_phases", "phases", "steps", "execution_plan", "planned_attempts", "plan", "execution_steps", "model_sequence", "backend_plan")


def normalize_attempt_payload(raw_attempt: dict | str, default_reason: str = "") -> dict:
    """Normalize one local-model attempt/phase/sequence item into StrategyAttempt shape."""
    if isinstance(raw_attempt, str):
        backend=raw_attempt.strip()
        return {"backend": backend, "runner": "villani_code", "max_attempts": 1, "timeout_seconds": 1200, "reason": default_reason} if backend else {}
    if not isinstance(raw_attempt, dict):
        return {}
    backend=_first_string(raw_attempt, _BACKEND_ALIASES)
    if not backend:
        return {}
    runner=_first_string(raw_attempt, _RUNNER_ALIASES) or "villani_code"
    max_attempts=1
    for key in _MAX_ATTEMPT_ALIASES:
        if key in raw_attempt:
            max_attempts=_coerce_int(raw_attempt.get(key), 1)
            break
    timeout=1200
    for key in _TIMEOUT_ALIASES:
        if key in raw_attempt:
            timeout=_coerce_int(raw_attempt.get(key), 1200)
            break
    reason=_first_string(raw_attempt, _REASON_ALIASES) or default_reason or "Normalized local-model policy attempt."
    return {"backend": backend.strip(), "runner": runner.strip() or "villani_code", "max_attempts": _clamp(max_attempts, 1, 3), "timeout_seconds": max(1, timeout), "reason": reason}


def _normalize_attempt_list(value: Any, default_reason: str) -> list[dict]:
    if isinstance(value, list):
        return [a for item in value if (a:=normalize_attempt_payload(item, default_reason))]
    if isinstance(value, dict):
        if isinstance(value.get("attempts"), list):
            return _normalize_attempt_list(value.get("attempts"), default_reason)
        return [a for a in [normalize_attempt_payload(value, default_reason)] if a]
    if isinstance(value, str):
        return [a for a in [normalize_attempt_payload(value, default_reason)] if a]
    return []


def _trim_attempt_budget(attempts: list[dict], requested_profile: str) -> list[dict]:
    max_total=DEFAULT_PROFILES.get(requested_profile, {}).get("max_total_attempts", 3)
    trimmed=[]; total=0
    for attempt in attempts:
        backend=str(attempt.get("backend", "")).strip()
        if not backend or total >= max_total:
            continue
        attempt=dict(attempt); attempt["backend"]=backend
        allowed=max_total-total
        attempt["max_attempts"]=_clamp(_coerce_int(attempt.get("max_attempts"), 1), 1, min(3, allowed))
        total += attempt["max_attempts"]
        trimmed.append(attempt)
    return trimmed


def normalize_execution_strategy_payload(raw: dict, requested_profile: str) -> dict:
    payload=dict(raw or {})
    hint=payload.get("profile") or payload.get("strategy_name") or payload.get("strategy_id") or payload.get("policy") or payload.get("selected_profile") or payload.get("profile_name") or payload.get("mode")
    for alias in ("strategy_name", "strategy_id", "policy", "selected_profile", "profile_name", "mode"):
        payload.pop(alias, None)
    payload["profile"]=requested_profile

    attempts=[]
    canonical_field="attempts"
    # Prefer the schema-native field when present, then planned_attempts, then rich phases, then sequences.
    if isinstance(payload.get("attempts"), list):
        attempts=_normalize_attempt_list(payload.get("attempts"), "Created from attempts.")
    elif isinstance(payload.get("planned_attempts"), list):
        attempts=_normalize_attempt_list(payload.get("planned_attempts"), "Created from planned_attempts.")
    if not attempts:
        for source in ("execution_phases", "phases", "steps", "execution_steps", "execution_plan", "plan"):
            if source in payload:
                attempts=_normalize_attempt_list(payload.get(source), f"Created from {source}.")
                if attempts:
                    break
    if not attempts:
        for source in _SEQUENCE_SOURCES:
            if source in payload:
                attempts=_normalize_attempt_list(payload.get(source), f"Created from {source}.")
                if attempts:
                    break
    attempts=_trim_attempt_budget(attempts, requested_profile)
    payload[canonical_field]=attempts
    payload.pop("planned_attempts", None)

    if any(field in (raw or {}) for field in _PLANNING_FIELDS) and not attempts:
        payload["_normalization_error"]="Policy response contained planning fields but no attempts could be normalized."

    stop=None
    for key in ("stop_conditions", "stop_condition", "termination_conditions", "termination", "exit_conditions", "success_criteria"):
        if key in payload:
            stop=payload.get(key); break
    if isinstance(stop, dict):
        mode=stop.get("mode") or ("first_accepted" if stop.get("stop_on_success", True) else None) or "first_accepted"
        payload["stop_conditions"]={"mode": mode}
    elif isinstance(stop, str) and stop.strip():
        payload["stop_conditions"]={"mode": stop.strip()}
    else:
        payload["stop_conditions"]={"mode":"first_accepted"}
    for key in ("stop_condition", "termination_conditions", "termination", "exit_conditions", "success_criteria"):
        payload.pop(key, None)

    warnings=payload.get("warnings", [])
    if isinstance(warnings, str): warnings=[warnings]
    if not isinstance(warnings, list): warnings=[]
    for key in ("risks", "caveats", "notes", "concerns"):
        val=payload.get(key)
        if isinstance(val, str) and val.strip(): warnings.append(val.strip())
        elif isinstance(val, list): warnings.extend(str(x).strip() for x in val if str(x).strip())
    payload["warnings"]=warnings

    if not payload.get("strategy_summary"):
        for key in ("summary", "reasoning_summary", "rationale"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                payload["strategy_summary"]=payload[key].strip(); break
        else:
            payload["strategy_summary"]=str(hint).strip() if hint else "Normalized local-model policy strategy."

    if "backend_rankings" not in payload:
        for key in ("ranked_backends", "backend_scores", "ranking", "rankings"):
            if key in payload:
                payload["backend_rankings"]=payload.get(key); break
    payload.setdefault("backend_rankings", [])
    return payload

def _write_controller_error(run_dir: Path|None, phase: str, backend: Backend|None, schema: str, result: LLMCallResult|None, parse_error=None, validation_error=None, normalized_payload=None, raw_payload=None, fallback_used=False, fallback_payload=None, fallback_reason=None):
    if not run_dir: return
    d=Path(run_dir)/"controller_calls"; d.mkdir(parents=True, exist_ok=True)
    data={"phase":phase,"backend":getattr(backend,"name",None),"schema":schema,"url":getattr(result,"url",None),"model":getattr(result,"model",getattr(backend,"model",None)),"max_tokens":getattr(result,"max_tokens",getattr(backend,"max_tokens",None)),"http_status":getattr(result,"http_status",None),"finish_reason":getattr(result,"finish_reason",None),"usage":getattr(result,"usage",{}) if result else {},"message_content":getattr(result,"raw_text",None),"reasoning_content":getattr(result,"reasoning_content",None),"raw_response":getattr(result,"raw_response",{}) if result else {},"parse_error":parse_error,"validation_error":validation_error,"raw_payload":raw_payload if raw_payload is not None else (getattr(result,"parsed_json",{}) if result else {}),"normalized_payload":normalized_payload or {},"fallback_used":fallback_used,"fallback_reason":fallback_reason or (validation_error or parse_error if fallback_used else None),"fallback_payload":fallback_payload or {}}
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
    estimated_solve_probability: float | None = None
    estimated_attempt_cost: float | None = None
    estimated_input_tokens: int | None = None
    estimated_output_tokens: int | None = None
    required_capability: int | None = None
    capability_score: int | None = None
    capability_gap: int | None = None
class ExecutionStrategy(BaseModel):
    model_config = ConfigDict(extra="ignore")
    profile: str; strategy_summary: str=''; attempts: list[StrategyAttempt]=Field(default_factory=list); stop_conditions: dict[str, Any]=Field(default_factory=dict); escalation_rules:list[dict[str,Any]]=Field(default_factory=list); cost_risk_summary:str=''; warnings: list[str]=Field(default_factory=list); backend_rankings: list[dict[str, Any]]=Field(default_factory=list)
    max_attempts: int | None = None
    required_capability: int | None = None
    planning_objective: str | None = None
    deterministic_planner: bool = False

class PolicyEngine:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def generate(self, classification: TaskClassification, backends: dict[str, Backend], profile: str, out_path: str|Path|None=None, max_attempts: int|None=None) -> tuple[ExecutionStrategy, LLMCallResult]:
        from villani_ops.policy_engine.planner import plan_execution_strategy, deterministic_policy_call
        if type(self.client) is LLMClient:
            strat=plan_execution_strategy(backends, classification, profile, max_attempts=max_attempts)
            if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
            return strat, deterministic_policy_call()
        if profile not in DEFAULT_PROFILES: raise ValueError(f"Unknown policy profile '{profile}'")
        policy_backend=select_backend(backends,'policy'); coding=coding_backends(backends)
        if not coding: raise ValueError("No enabled coding backends configured.")
        allowed={b.name for b in coding}; max_total=DEFAULT_PROFILES[profile]['max_total_attempts']
        ctx={"classification":classification.model_dump(),"profile":profile,"profile_rules":DEFAULT_PROFILES[profile],"coding_backends":[b.redacted_dict() for b in coding]}
        try:
            result=self.client.complete_json(policy_backend, SYSTEM, USER.format(context=json.dumps(ctx, indent=2)), 'ExecutionStrategy')
        except LLMCallError as e:
            strat=build_deterministic_fallback_strategy(classification, backends, profile, str(e))
            _write_controller_error(Path(out_path).parent if out_path else None, 'policy', policy_backend, 'ExecutionStrategy', e.result, parse_error=e.parse_error, fallback_used=True, fallback_payload=strat.model_dump())
            if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
            return strat, e.result or LLMCallResult(parsed_json={}, raw_text='', backend_name=policy_backend.name, model=policy_backend.model)
        normalized=normalize_execution_strategy_payload(result.parsed_json, profile)
        if normalized.get("_normalization_error"):
            reason=normalized["_normalization_error"]
            strat=build_deterministic_fallback_strategy(classification, backends, profile, reason)
            _write_controller_error(Path(out_path).parent if out_path else None, "policy", policy_backend, "ExecutionStrategy", result, validation_error=reason, normalized_payload=normalized, fallback_used=True, fallback_payload=strat.model_dump())
            if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
            return strat, result
        try:
            strat=ExecutionStrategy.model_validate(normalized)
        except Exception as e:
            run_dir=Path(out_path).parent if out_path else None
            strat=build_deterministic_fallback_strategy(classification, backends, profile, str(e))
            _write_controller_error(run_dir, "policy", policy_backend, "ExecutionStrategy", result, validation_error=str(e), normalized_payload=normalized, fallback_used=True, fallback_payload=strat.model_dump())
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
            mult={'easy':0.5,'medium':1.0,'hard':1.5}.get(classification.difficulty,1.0)
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
        hard = classification.difficulty == 'hard' or classification.risk == 'high'
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
            strat=build_deterministic_fallback_strategy(classification, backends, profile, reason)
            _write_controller_error(Path(out_path).parent if out_path else None, "policy", policy_backend, "ExecutionStrategy", result, validation_error=reason, normalized_payload=normalized, fallback_used=True, fallback_payload=strat.model_dump())
        if out_path: Path(out_path).write_text(strat.model_dump_json(indent=2))
        return strat, result
