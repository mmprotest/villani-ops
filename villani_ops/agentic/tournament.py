from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from typing import Literal, Any
from pydantic import BaseModel, Field, ConfigDict

class CandidateRiskReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate_id: str
    summary: str
    changed_files: list[str]=Field(default_factory=list)
    likely_correct: bool
    confidence: float
    strengths: list[str]=Field(default_factory=list)
    risks: list[str]=Field(default_factory=list)
    likely_hidden_failures: list[str]=Field(default_factory=list)
    edge_cases_considered: list[str]=Field(default_factory=list)
    edge_cases_missed: list[str]=Field(default_factory=list)
    minimality_score: float
    correctness_score: float
    hidden_test_risk_score: float
    recommendation: Literal['strong_accept','accept','weak_accept','reject','uncertain']
    rationale: str

class PairwiseCandidateComparison(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate_a: str
    candidate_b: str
    material_differences: list[str]=Field(default_factory=list)
    a_likely_failures: list[str]=Field(default_factory=list)
    b_likely_failures: list[str]=Field(default_factory=list)
    winner: Literal['candidate_a','candidate_b','tie','neither']
    confidence: float
    rationale: str

class RankedCandidate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate_id: str
    rank: int
    correctness_score: float
    hidden_test_risk_score: float
    pairwise_wins: int
    pairwise_losses: int
    validation_status: str | None=None
    materiality_notes: str

class TournamentRanking(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ranked_candidates: list[RankedCandidate]=Field(default_factory=list)
    selected_candidate_id: str | None=None
    selection_confidence: float
    unresolved_risks: list[str]=Field(default_factory=list)
    rationale: str

FORBIDDEN_PROMPT_PHRASES=(
    'Candidate 1 failed','previous attempt','oracle','behavioural oracle','review said',
    'hidden test','comparison','try differently from','another candidate',
    'attempt learning','validation strategy','behavioural checklist',
)

def build_tournament_candidate_prompt(task: str, success_criteria: str | None=None, *, repo_context: str | None=None) -> str:
    sections=[
        f'TASK\n{task}',
        f'SUCCESS CRITERIA\n{success_criteria or "Complete the task with a minimal correct patch."}',
    ]
    if repo_context:
        sections.append(f'RUNNER CONTEXT\n{repo_context}')
    sections.append('INSTRUCTIONS\nProduce one independent, minimal, correct solution for the task. Do not create internal scratch artifacts in the final patch.')
    return '\n\n'.join(sections)

def prompt_is_clean(prompt: str) -> bool:
    low=prompt.lower()
    return not any(p.lower() in low for p in FORBIDDEN_PROMPT_PHRASES)

def build_adversarial_review_prompt(payload: dict[str, Any]) -> str:
    return ('Assume this plausible solution is wrong. What hidden test would break it? '
            'What edge case did it likely miss? Does the patch actually implement the requested behaviour, '
            'or only a structural pattern? What behaviour is unproven? Return CandidateRiskReview JSON.\n'
            + str(payload))

def build_pairwise_comparison_prompt(a: dict[str, Any], b: dict[str, Any]) -> str:
    return ('Where do these candidates differ in actual behaviour? Which candidate handles edge cases better? '
            'Which candidate is more likely to pass hidden tests? Which candidate has the simpler and more robust patch? '
            'Do not compare only review scores. Return PairwiseCandidateComparison JSON.\n'
            + str({'candidate_a':a,'candidate_b':b}))

def summarize_candidate_agreement(candidates: list[Any]) -> dict[str, Any]:
    sigs=defaultdict(list)
    for c in candidates:
        files=tuple(sorted(getattr(c,'changed_files',[]) or (c.get('changed_files',[]) if isinstance(c,dict) else [])))
        sigs[files].append(getattr(c,'attempt_id',None) or (c.get('attempt_id') if isinstance(c,dict) else None))
    largest=max((len(v) for v in sigs.values()), default=0)
    return {'same_patch': largest>1, 'same_answer': largest>1, 'same_behaviour': largest>1,
            'material_differences':[{'changed_files':list(k),'candidates':v} for k,v in sigs.items()],
            'consensus_strength': (largest / max(1, len(candidates)))}

def rank_candidates(reviews: list[CandidateRiskReview], comparisons: list[PairwiseCandidateComparison], validation: dict[str,str] | None=None, generic_scores: dict[str,float] | None=None) -> TournamentRanking:
    validation=validation or {}; generic_scores=generic_scores or {}
    by={r.candidate_id:r for r in reviews}; wins=defaultdict(int); losses=defaultdict(int)
    for c in comparisons:
        if c.winner=='candidate_a': wins[c.candidate_a]+=1; losses[c.candidate_b]+=1
        elif c.winner=='candidate_b': wins[c.candidate_b]+=1; losses[c.candidate_a]+=1
    def key(cid):
        r=by[cid]; auth=1 if validation.get(cid)=='passed' else 0
        return (auth, wins[cid], -r.hidden_test_risk_score, r.correctness_score, r.minimality_score, generic_scores.get(cid,0.0))
    ordered=sorted(by, key=key, reverse=True)
    ranked=[RankedCandidate(candidate_id=cid,rank=i+1,correctness_score=by[cid].correctness_score,hidden_test_risk_score=by[cid].hidden_test_risk_score,pairwise_wins=wins[cid],pairwise_losses=losses[cid],validation_status=validation.get(cid),materiality_notes='ranked by validation, pairwise wins, hidden-test risk, adversarial review, minimality, then generic score') for i,cid in enumerate(ordered)]
    selected=ordered[0] if ordered else None
    risks=[] if selected and validation.get(selected)=='passed' else ['selection is evidence-based, not authoritatively validated']
    return TournamentRanking(ranked_candidates=ranked,selected_candidate_id=selected,selection_confidence=0.85 if selected and validation.get(selected)=='passed' else 0.55,unresolved_risks=risks,rationale='Selection prioritised authoritative validation, pairwise wins, lower hidden-test risk, stronger adversarial review, patch minimality, generic review score, then cost.')

def decide_launch_count(requested:int, max_parallel:int, timeout_seconds:int|None, estimated_time_per_candidate:float=120.0, review_budget:float=120.0, finalization_budget:float=60.0)->dict[str,Any]:
    requested=max(1,int(requested)); max_parallel=max(1,int(max_parallel or 1))
    if timeout_seconds is None:
        return {'candidate_attempts_requested':requested,'candidate_attempts_launched':requested,'reason':'no timeout configured','max_parallel':max_parallel}
    available=max(0.0,float(timeout_seconds)-review_budget-finalization_budget)
    waves=max(1, int(available // max(1.0, estimated_time_per_candidate)))
    launch=min(requested, waves*max_parallel)
    launch=max(1, launch) if available>0 else 0
    reason='reserved time for review and finalization' if launch<requested else 'budget sufficient'
    return {'candidate_attempts_requested':requested,'candidate_attempts_launched':launch,'reason':reason,'max_parallel':max_parallel}
