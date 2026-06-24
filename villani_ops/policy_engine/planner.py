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
    base_solve_probability: float
    shape_adjustment: float
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
    base={"easy":15,"medium":30,"hard":55}.get(classification.difficulty,30)
    risk={"low":0,"medium":3,"high":8}.get(classification.risk,3)
    return max(0, min(100, base+risk))


def estimate_backend_base_solve_probability(backend: Backend, classification: TaskClassification, required_capability: int) -> float:
    gap=backend.capability_score-required_capability
    if gap >= 30: p=.90
    elif gap >= 20: p=.80
    elif gap >= 10: p=.70
    elif gap >= 0: p=.60
    elif gap >= -10: p=.45
    elif gap >= -20: p=.30
    elif gap >= -30: p=.18
    else: p=.08
    p += {"low":.04,"medium":0,"high":-.08}.get(classification.risk,0)
    p += {"easy":.04,"medium":0,"hard":-.04}.get(classification.difficulty,0)
    return max(.01, min(.95, round(p, 4)))

def estimate_task_shape_bonus(classification: TaskClassification, task: str='', relevant_files: list|None=None) -> float:
    signals=dict(getattr(classification, 'task_shape_signals', {}) or {})
    relevant_count=signals.get('relevant_file_count', len(relevant_files or getattr(classification, 'relevant_file_paths', []) or []))
    bonus=0.0
    if relevant_count and relevant_count <= 2: bonus += .05
    if signals.get('target_files_found'): bonus += .04
    if signals.get('failing_tests_mentioned') or signals.get('explicit_tests_mentioned'): bonus += .04
    if signals.get('do_not_change_tests'): bonus += .02
    if getattr(classification, 'confidence', 0.0) >= .85: bonus += .02
    if relevant_count >= 8: bonus -= .05
    if signals and getattr(classification, 'confidence', 0.0) < .50: bonus -= .05
    if signals and relevant_count == 0: bonus -= .03
    if signals.get('broad_change'): bonus -= .05
    return round(max(-.12, min(.17, bonus)), 4)

def estimate_backend_solve_probability(backend: Backend, classification: TaskClassification, required_capability: int, task: str='', relevant_files: list|None=None) -> float:
    base=estimate_backend_base_solve_probability(backend, classification, required_capability)
    p=base+estimate_task_shape_bonus(classification, task, relevant_files)
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
        base=estimate_backend_base_solve_probability(b, classification, req); shape=estimate_task_shape_bonus(classification); p=max(.01, min(.95, round(base+shape, 4))); cost=b.estimate_cost(inp,out); gap=b.capability_score-req
        raw.append(BackendPlanScore(b, req, p, base, shape, inp, out, cost, gap, is_backend_viable_for_task(b, classification, req, policy)))
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
        rows.append({"expected_cost_rank":i,"backend":s.backend.name,"capability_score":s.backend.capability_score,"required_capability":s.required_capability,"capability_gap":s.capability_gap,"estimated_solve_probability":s.estimated_solve_probability,"base_solve_probability":s.base_solve_probability,"shape_adjustment":s.shape_adjustment,"final_solve_probability":s.estimated_solve_probability,"estimated_input_tokens":s.estimated_input_tokens,"estimated_output_tokens":s.estimated_output_tokens,"estimated_attempt_cost":round(s.estimated_attempt_cost,6),"viable":s.viable,"utility":s.utility,"rank_reason":f"{policy} deterministic ranking using capability, estimated solve probability, and estimated cost"})
    return rows


def rank_scores(scores: list[BackendPlanScore], policy: str) -> list[BackendPlanScore]:
    if policy == "quality": return sorted(scores, key=lambda s:(-s.backend.capability_score, s.estimated_attempt_cost, s.backend.name))
    if policy == "cheap": return sorted(scores, key=lambda s:(not s.viable, s.estimated_attempt_cost, s.backend.capability_score, -s.estimated_solve_probability, s.backend.name))
    return sorted(scores, key=lambda s:(not s.viable, s.backend.capability_score, s.estimated_attempt_cost, -s.utility, s.backend.name))



def _next_viable_or_strongest(scores: list[BackendPlanScore], candidate: BackendPlanScore) -> BackendPlanScore:
    higher_cost_viable=[s for s in scores if s.backend.name != candidate.backend.name and s.viable and s.estimated_attempt_cost >= candidate.estimated_attempt_cost]
    if higher_cost_viable:
        return min(higher_cost_viable, key=lambda s:s.estimated_attempt_cost)
    return max(scores, key=lambda s:(s.backend.capability_score, -s.estimated_attempt_cost))

def _cheap_plausible(s: BackendPlanScore, scores: list[BackendPlanScore]) -> bool:
    if s.viable:
        return True
    ref=_next_viable_or_strongest(scores, s)
    return s.capability_gap >= -30 and s.estimated_solve_probability >= .15 and s.estimated_attempt_cost <= .75 * max(ref.estimated_attempt_cost, .000001)

def _cheapest_plausible(scores: list[BackendPlanScore]) -> BackendPlanScore|None:
    plausible=[s for s in scores if _cheap_plausible(s, scores)]
    if not plausible: return None
    return min(plausible, key=lambda s:(s.estimated_attempt_cost, -s.estimated_solve_probability, -s.backend.capability_score, s.backend.name))

def _next_stronger(current: BackendPlanScore, scores: list[BackendPlanScore]) -> BackendPlanScore|None:
    stronger=[s for s in scores if s.backend.capability_score > current.backend.capability_score]
    if not stronger: return None
    viable=[s for s in stronger if s.viable or _cheap_plausible(s, scores)] or stronger
    return max(viable, key=lambda s:(s.backend.capability_score, s.estimated_solve_probability, -s.estimated_attempt_cost))

def _attempt(s: BackendPlanScore, reason: str) -> StrategyAttempt:
    return StrategyAttempt(backend=s.backend.name, max_attempts=1, reason=reason, estimated_solve_probability=s.estimated_solve_probability, base_solve_probability=s.base_solve_probability, shape_adjustment=s.shape_adjustment, estimated_attempt_cost=round(s.estimated_attempt_cost,6), estimated_input_tokens=s.estimated_input_tokens, estimated_output_tokens=s.estimated_output_tokens, required_capability=s.required_capability, capability_score=s.backend.capability_score, capability_gap=s.capability_gap)


def _pick_best_available(scores):
    return max(scores, key=lambda s:(s.estimated_solve_probability, s.backend.capability_score, -s.estimated_attempt_cost))


def plan_execution_strategy(backends: dict[str, Backend], classification: TaskClassification, policy: str, max_attempts: int|None=None) -> ExecutionStrategy:
    if policy not in DEFAULT_PROFILES: raise ValueError(f"Unknown policy profile '{policy}'")
    max_attempts=validate_max_attempts(max_attempts if max_attempts is not None else default_max_attempts_for_policy(policy))
    scores,warnings=_scores(backends, classification, policy)
    ordered=rank_scores(scores, policy)
    plausible=_cheapest_plausible(scores) if policy in {"cheap","balanced"} else None
    if policy in {"cheap","balanced"} and not any(s.viable for s in scores) and not plausible:
        fallback=_pick_best_available(scores)
        ordered=[fallback]+[s for s in ordered if s.backend.name != fallback.backend.name]
    attempts=[]
    if policy == "quality":
        strongest=ordered[0]
        attempts=[_attempt(strongest, "Quality policy: use the highest-capability enabled coding backend.") for _ in range(max_attempts)]
        objective="maximize capability"
    elif policy == "cheap":
        current=plausible or ordered[0]
        first_reason="Selected as cheap exploratory attempt: lower cost and non-hopeless capability gap." if not current.viable else "Cheap policy: selected cheapest viable backend."
        for i in range(max_attempts):
            attempts.append(_attempt(current, first_reason if i == 0 else "Cheap policy: retry cheap backend or escalate when solve probability is low."))
            retry_ok=current.estimated_solve_probability >= .35 or current.capability_gap >= 0
            if not retry_ok:
                higher=_next_stronger(current, scores)
                if higher: current=higher
        objective="minimize estimated cost with cheap exploratory attempts for plausible backends"
    else:
        current=plausible or ordered[0]
        attempts.append(_attempt(current, "Balanced policy: start with the least-cost plausible backend."))
        for i in range(1, max_attempts):
            stronger=_next_stronger(current, scores)
            if stronger: current=stronger
            attempts.append(_attempt(current, "Balanced policy: escalate sooner to higher capability after a plausible low-cost first attempt."))
        objective="cost-aware balance of solve probability and cost"
    return ExecutionStrategy(profile=policy, strategy_summary=f"Deterministic policy planner used; planning objective: {objective}.", attempts=attempts[:max_attempts], stop_conditions={"mode":"first_accepted"}, warnings=warnings, backend_rankings=_ranking_dicts(scores, policy), max_attempts=max_attempts, required_capability=estimate_required_capability(classification), planning_objective=objective, deterministic_planner=True)


def deterministic_policy_call() -> LLMCallResult:
    return LLMCallResult(parsed_json={}, raw_text="deterministic policy planner", backend_name="deterministic", model="deterministic", estimated_cost=0.0)
