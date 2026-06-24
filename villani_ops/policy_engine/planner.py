from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from villani_ops.core.backend import Backend, coding_backends
from villani_ops.core.task import TaskClassification
from villani_ops.llm.client import LLMCallResult
from .defaults import DEFAULT_PROFILES
from .engine import ExecutionStrategy, StrategyAttempt

MIN_MAX_ATTEMPTS=1
MAX_MAX_ATTEMPTS=10

@dataclass(frozen=True)
class BackendPlanScore:
    backend: Backend
    required_capability: int
    estimated_solve_probability: float
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_attempt_cost: float
    capability_gap: int
    viable: bool
    utility: float = 0.0


def default_max_attempts_for_policy(policy: str) -> int:
    return int(DEFAULT_PROFILES[policy]["max_total_attempts"])


def validate_max_attempts(max_attempts: int) -> int:
    if isinstance(max_attempts, bool):
        raise ValueError("max_attempts must be an integer")
    value=int(max_attempts)
    if value < MIN_MAX_ATTEMPTS or value > MAX_MAX_ATTEMPTS:
        raise ValueError(f"max_attempts must be between {MIN_MAX_ATTEMPTS} and {MAX_MAX_ATTEMPTS}")
    return value


def estimate_required_capability(classification: TaskClassification) -> int:
    base={"easy":20,"medium":40,"hard":60}.get(classification.difficulty,40)
    risk={"low":0,"medium":5,"high":10}.get(classification.risk,5)
    cat={"documentation":-10,"test_fix":-5,"bug_fix":0,"refactor":5,"feature":5,"dependency":5,"security":10,"unknown":5}.get(classification.category or "unknown",5)
    return max(0, min(100, base+risk+cat))


def estimate_backend_solve_probability(backend: Backend, classification: TaskClassification, required_capability: int) -> float:
    gap=backend.capability_score-required_capability
    if gap >= 25: p=.85
    elif gap >= 10: p=.70
    elif gap >= 0: p=.55
    elif gap >= -10: p=.35
    elif gap >= -20: p=.18
    else: p=.05
    p += {"low":.05,"medium":0,"high":-.10}.get(classification.risk,0)
    p += {"easy":.05,"medium":0,"hard":-.05}.get(classification.difficulty,0)
    return max(.01, min(.95, round(p, 4)))


def estimate_backend_attempt_tokens(classification: TaskClassification) -> tuple[int,int]:
    inp,out={"easy":(20000,3000),"medium":(50000,6000),"hard":(90000,10000)}.get(classification.difficulty,(50000,6000))
    mult={"low":1.0,"medium":1.2,"high":1.5}.get(classification.risk,1.2)
    return int(inp*mult), int(out*mult)


def estimate_backend_attempt_cost(backend: Backend, classification: TaskClassification) -> float:
    inp,out=estimate_backend_attempt_tokens(classification)
    return backend.estimate_cost(inp,out)


def is_backend_viable_for_task(backend: Backend, classification: TaskClassification, required_capability: int, policy: str) -> bool:
    if policy == "quality": return True
    threshold={"cheap":.25,"balanced":.20}.get(policy,.20)
    return estimate_backend_solve_probability(backend, classification, required_capability) >= threshold


def _scores(backends: dict[str, Backend], classification: TaskClassification, policy: str) -> tuple[list[BackendPlanScore], list[str]]:
    coding=coding_backends(backends)
    if not coding: raise ValueError("No enabled coding backends available")
    req=estimate_required_capability(classification); inp,out=estimate_backend_attempt_tokens(classification)
    raw=[]
    for b in coding:
        p=estimate_backend_solve_probability(b, classification, req); cost=b.estimate_cost(inp,out); gap=b.capability_score-req
        raw.append(BackendPlanScore(b, req, p, inp, out, cost, gap, is_backend_viable_for_task(b, classification, req, policy)))
    max_cost=max((s.estimated_attempt_cost for s in raw), default=0)
    min_cost=min((s.estimated_attempt_cost for s in raw), default=0)
    out_scores=[]
    for s in raw:
        cost_eff=1.0 if max_cost <= min_cost else (max_cost-s.estimated_attempt_cost)/(max_cost-min_cost)
        cap_fit=max(0.0, min(1.0, s.backend.capability_score/max(s.required_capability,1)))
        util=.55*s.estimated_solve_probability+.25*cost_eff+.20*cap_fit
        out_scores.append(BackendPlanScore(**{**s.__dict__, "utility":round(util, 6)}))
    warnings=[]
    if policy in {"cheap","balanced"} and not any(s.viable for s in out_scores):
        warnings.append("No backend met the viability threshold; selected best available probability-per-dollar backend.")
    return out_scores, warnings


def _ranking_dicts(scores: list[BackendPlanScore], policy: str) -> list[dict[str, Any]]:
    ranked=rank_scores(scores, policy)
    rows=[]
    for i,s in enumerate(ranked,1):
        rows.append({"expected_cost_rank":i,"backend":s.backend.name,"capability_score":s.backend.capability_score,"required_capability":s.required_capability,"capability_gap":s.capability_gap,"estimated_solve_probability":s.estimated_solve_probability,"estimated_input_tokens":s.estimated_input_tokens,"estimated_output_tokens":s.estimated_output_tokens,"estimated_attempt_cost":round(s.estimated_attempt_cost,6),"viable":s.viable,"utility":s.utility,"rank_reason":f"{policy} deterministic ranking using capability, estimated solve probability, and estimated cost"})
    return rows


def rank_scores(scores: list[BackendPlanScore], policy: str) -> list[BackendPlanScore]:
    if policy == "quality": return sorted(scores, key=lambda s:(-s.backend.capability_score, s.estimated_attempt_cost, s.backend.name))
    if policy == "cheap": return sorted(scores, key=lambda s:(not s.viable, s.estimated_attempt_cost, s.backend.capability_score, -s.estimated_solve_probability, s.backend.name))
    return sorted(scores, key=lambda s:(not s.viable, s.backend.capability_score, s.estimated_attempt_cost, -s.utility, s.backend.name))


def _attempt(s: BackendPlanScore, reason: str) -> StrategyAttempt:
    return StrategyAttempt(backend=s.backend.name, max_attempts=1, reason=reason, estimated_solve_probability=s.estimated_solve_probability, estimated_attempt_cost=round(s.estimated_attempt_cost,6), estimated_input_tokens=s.estimated_input_tokens, estimated_output_tokens=s.estimated_output_tokens, required_capability=s.required_capability, capability_score=s.backend.capability_score, capability_gap=s.capability_gap)


def _pick_best_available(scores):
    return max(scores, key=lambda s:(s.estimated_solve_probability/max(s.estimated_attempt_cost, .000001), s.estimated_solve_probability, -s.estimated_attempt_cost, s.backend.capability_score))


def plan_execution_strategy(backends: dict[str, Backend], classification: TaskClassification, policy: str, max_attempts: int|None=None) -> ExecutionStrategy:
    if policy not in DEFAULT_PROFILES: raise ValueError(f"Unknown policy profile '{policy}'")
    max_attempts=validate_max_attempts(max_attempts if max_attempts is not None else default_max_attempts_for_policy(policy))
    scores,warnings=_scores(backends, classification, policy)
    if policy in {"cheap","balanced"} and not any(s.viable for s in scores):
        ordered=[_pick_best_available(scores)]
    else:
        ordered=rank_scores(scores, policy)
    attempts=[]
    if policy == "quality":
        strongest=ordered[0]
        attempts=[_attempt(strongest, "Quality policy: use the highest-capability enabled coding backend.") for _ in range(max_attempts)]
        objective="maximize capability"
    elif policy == "cheap":
        current=ordered[0]
        for i in range(max_attempts):
            attempts.append(_attempt(current, "Cheap policy: minimize cost while avoiding hopeless attempts."))
            retry_ok=current.estimated_solve_probability >= .45 or current.capability_gap >= 0 or (classification.difficulty=="easy" and classification.risk=="low")
            if not retry_ok:
                higher=[s for s in ordered if s.backend.capability_score > current.backend.capability_score and (s.viable or not any(x.viable for x in scores))]
                if higher: current=higher[0]
        objective="minimize estimated cost subject to viability"
    else:
        current=ordered[0]
        attempts.append(_attempt(current, "Balanced policy: start with the lowest-capability credible backend."))
        for i in range(1, max_attempts):
            stronger=[s for s in ordered if s.backend.capability_score >= current.backend.capability_score+10 and (s.viable or not any(x.viable for x in scores))]
            if stronger and (i == 1 or current.estimated_solve_probability < .55): current=stronger[0]
            attempts.append(_attempt(current, "Balanced policy: cost-aware escalation based on capability and solve probability."))
        objective="cost-aware balance of solve probability and cost"
    return ExecutionStrategy(profile=policy, strategy_summary=f"Deterministic policy planner used; planning objective: {objective}.", attempts=attempts[:max_attempts], stop_conditions={"mode":"first_accepted"}, warnings=warnings, backend_rankings=_ranking_dicts(scores, policy), max_attempts=max_attempts, required_capability=estimate_required_capability(classification), planning_objective=objective, deterministic_planner=True)


def deterministic_policy_call() -> LLMCallResult:
    return LLMCallResult(parsed_json={}, raw_text="deterministic policy planner", backend_name="deterministic", model="deterministic", estimated_cost=0.0)
