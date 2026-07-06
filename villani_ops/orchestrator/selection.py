from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from pathlib import Path
import json
import random
import re
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMClient

POLICY='binary_verifier_quality_tie'
LLM_COMPARE_POLICY='binary_verifier_llm_compare_tie'
LLM_COMPARE_FALLBACK_POLICY='binary_verifier_llm_compare_tie_fallback_quality'
VALID_ON_ALL_FAIL={'fail','random','best-confidence'}

_STRONG_SUCCESS_EVIDENCE_RE = re.compile(
    r"\b(test|tests|passed|validation|validated|verified|behavior|runtime|end-to-end|e2e|integration|executed|ran|imported|output|cleanup|cancellation|install|fresh|downstream|smoke)\b",
    re.IGNORECASE,
)

@dataclass
class SelectionResult:
    schemaVersion: str
    selectionPolicy: str
    seed: int
    onAllFail: str
    winnerCandidateId: str|None
    winnerResult: int|None
    tieBreak: bool
    candidatePool: list[str]
    allCandidates: list[dict[str,Any]]
    reason: str
    fallback: str|None=None
    qualityTieBreakApplied: bool=False
    winnerQualityKey: dict[str,Any]|None=None
    candidateQuality: list[dict[str,Any]]|None=None
    llmComparison: dict[str,Any]|None=None
    def to_dict(self): return self.__dict__.copy()

def _vr(c):
    if hasattr(c,'verifier_result'): return getattr(c,'verifier_result') or {}
    return c.get('verifier_result') or c.get('verifierResult') or c

def _cid(c): return getattr(c,'candidate_id',None) or c.get('candidateId') or c.get('candidate_id')

def _confidence(c):
    try: return float((_vr(c).get('confidence') if _vr(c).get('confidence') is not None else 0.0) or 0.0)
    except Exception: return 0.0

def _summary(c):
    v=_vr(c)
    return {'candidateId':_cid(c),'result':v.get('result'),'verdict':v.get('verdict'),'confidence':v.get('confidence'),'recommendedAction':v.get('recommendedAction'),'traceDir':v.get('traceDir') or v.get('trace_dir')}

def _recommended_action(c):
    return str((_vr(c).get('recommendedAction') or '')).strip().lower()

def _list_field(v: dict[str, Any], name: str) -> list[Any]:
    value = v.get(name)
    return value if isinstance(value, list) else []

def _uncertainty_level(v: dict[str, Any]) -> str:
    uncertainty = v.get('uncertainty')
    level = uncertainty.get('level') if isinstance(uncertainty, dict) else v.get('uncertaintyLevel')
    return str(level or 'medium').lower()

def verifier_quality_details(candidate: Any) -> dict[str, Any]:
    """Return generic verifier-quality diagnostics for candidate selection."""
    v = _vr(candidate)
    level = _uncertainty_level(v)
    uncertainty_rank = {'low': 2, 'medium': 1, 'high': 0}.get(level, 1)
    failure_count = len(_list_field(v, 'failureEvidence'))
    missing_count = len(_list_field(v, 'missingEvidence'))
    risk_count = len(_list_field(v, 'riskFlags'))
    reqs = _list_field(v, 'requirementResults')
    satisfied_count = sum(1 for r in reqs if isinstance(r, dict) and r.get('status') == 'satisfied')
    unsatisfied_count = sum(1 for r in reqs if isinstance(r, dict) and r.get('status') == 'unsatisfied')
    success = _list_field(v, 'successEvidence')
    behavioral_count = sum(1 for item in success if _STRONG_SUCCESS_EVIDENCE_RE.search(str(item)))
    tool_count = len(_list_field(v, 'toolsUsed'))
    confidence = _confidence(candidate)
    key = (
        uncertainty_rank,
        -failure_count,
        -missing_count,
        -risk_count,
        satisfied_count,
        -unsatisfied_count,
        behavioral_count,
        tool_count,
        confidence,
    )
    return {
        'candidateId': _cid(candidate),
        'result': v.get('result'),
        'confidence': confidence,
        'uncertaintyLevel': level if level in {'low','medium','high'} else 'medium',
        'failureEvidenceCount': failure_count,
        'missingEvidenceCount': missing_count,
        'riskFlagCount': risk_count,
        'satisfiedRequirementCount': satisfied_count,
        'unsatisfiedRequirementCount': unsatisfied_count,
        'behavioralSuccessEvidenceCount': behavioral_count,
        'toolCount': tool_count,
        'qualityKey': key,
    }

def verifier_quality_key(candidate: Any) -> tuple[Any, ...]:
    return verifier_quality_details(candidate)['qualityKey']

def _quality_rows(candidates: list[Any]) -> list[dict[str, Any]]:
    rows=[verifier_quality_details(c) for c in candidates]
    ranked=sorted({r['qualityKey'] for r in rows}, reverse=True)
    ranks={key:i+1 for i,key in enumerate(ranked)}
    for row in rows:
        row['qualityRank']=ranks[row['qualityKey']]
        row['qualityKey']={
            'uncertaintyRank': row['qualityKey'][0],
            'negativeFailureEvidenceCount': row['qualityKey'][1],
            'negativeMissingEvidenceCount': row['qualityKey'][2],
            'negativeRiskFlagCount': row['qualityKey'][3],
            'satisfiedRequirementCount': row['qualityKey'][4],
            'negativeUnsatisfiedRequirementCount': row['qualityKey'][5],
            'behavioralSuccessEvidenceCount': row['qualityKey'][6],
            'toolCount': row['qualityKey'][7],
            'confidence': row['qualityKey'][8],
        }
    return rows

def _pick_by_quality(bucket: list[Any], rng: random.Random) -> tuple[Any|None, list[str], bool]:
    if not bucket: return None, [], False
    best_key=max(verifier_quality_key(c) for c in bucket)
    best=[c for c in bucket if verifier_quality_key(c)==best_key]
    return rng.choice(best), [_cid(c) for c in best], len(best)>1

def select_winner(candidates:list[Any], seed:int, on_all_fail:str='fail')->SelectionResult:
    if on_all_fail not in VALID_ON_ALL_FAIL: raise ValueError('invalid on_all_fail')
    rng=random.Random(seed); allc=[_summary(c) for c in candidates]; candidate_quality=_quality_rows(candidates)
    successes=[c for c in candidates if _vr(c).get('result')==1]
    zeros=[c for c in candidates if _vr(c).get('result')==0]
    errors=[c for c in candidates if _vr(c).get('result') not in (0,1)]
    fallback=None; quality_applied=False
    if successes:
        accepted=[c for c in successes if _recommended_action(c)=='accept']
        bucket=accepted or successes
        win,pool,random_tie=_pick_by_quality(bucket,rng); quality_applied=len(bucket)>1
        if accepted:
            reason=(f'Selected {_cid(win)} by verifier quality tie-break among candidates with result = 1 and recommendedAction = accept.' if not random_tie else 'Selected randomly among accept-recommended candidates tied on verifier result and verifier quality key.')
        else:
            reason=(f'Selected {_cid(win)} by verifier quality tie-break among candidates with result = 1.' if not random_tie else 'Selected randomly among candidates tied on verifier result and verifier quality key.')
    elif on_all_fail=='fail':
        return SelectionResult('villani-ops-verifier-parallel-selection-v1',POLICY,seed,on_all_fail,None,None,False,[],allc,'No candidates had verifier result = 1; on-all-fail=fail skipped integration.',candidateQuality=candidate_quality)
    elif on_all_fail=='random':
        bucket=zeros or errors; pool=[_cid(c) for c in bucket]; win=rng.choice(bucket) if bucket else None; fallback='all-fail-random'; reason='All candidates failed; selected random fallback.'
    else:
        if zeros:
            maxc=max(_confidence(c) for c in zeros); bucket=[c for c in zeros if _confidence(c)==maxc]
            if len(bucket)>1:
                win,pool,random_tie=_pick_by_quality(bucket,rng); quality_applied=True
            else:
                win=bucket[0]; pool=[_cid(win)]; random_tie=False
            reason=('All candidates failed; selected result = 0 candidate with highest verifier confidence.' if not random_tie else 'All candidates failed; selected randomly among result = 0 candidates tied on verifier confidence and verifier quality key.')
        else:
            bucket=errors; pool=[_cid(c) for c in bucket]; win=rng.choice(bucket) if bucket else None; random_tie=len(bucket)>1; reason='All candidates had verifier errors; selected random error fallback.'
        fallback='all-fail-best-confidence'
    v=_vr(win) if win else {}
    winner_quality=next((r for r in candidate_quality if r['candidateId']==_cid(win)), None) if win else None
    return SelectionResult('villani-ops-verifier-parallel-selection-v1',POLICY,seed,on_all_fail,_cid(win) if win else None,v.get('result'),len(pool)>1,pool,allc,reason,fallback,quality_applied,winner_quality,candidate_quality)


def _truncate_text(value: Any, limit: int = 1200) -> str:
    text = str(value or '')
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated {len(text)-limit} chars]"

def _truncate_list(value: Any, max_items: int = 5, item_limit: int = 500) -> list[Any]:
    items = value if isinstance(value, list) else []
    out = [_truncate_text(item, item_limit) for item in items[:max_items]]
    if len(items) > max_items:
        out.append(f"…[truncated {len(items)-max_items} items]")
    return out

def build_llm_comparison_packet(candidates: list[Any], *, diff_limit: int = 2000, evidence_limit: int = 500) -> list[dict[str, Any]]:
    packets=[]
    for c in candidates:
        v=_vr(c)
        patch_path = getattr(c, 'patch_path', None) or (c.get('patchPath') if isinstance(c, dict) else None)
        diff=''
        if patch_path:
            try: diff=Path(patch_path).read_text(encoding='utf-8', errors='replace')
            except Exception: diff=''
        changed = getattr(c, 'changed_files', None) or (c.get('changedFiles') if isinstance(c, dict) else None) or []
        packets.append({
            'candidateId': _cid(c),
            'verifier': {
                'result': v.get('result'),
                'confidence': v.get('confidence'),
                'recommendedAction': v.get('recommendedAction'),
                'reason': _truncate_text(v.get('reason') or v.get('summary'), evidence_limit),
                'requirementResults': _truncate_list(v.get('requirementResults'), 5, evidence_limit),
                'successEvidence': _truncate_list(v.get('successEvidence'), 5, evidence_limit),
                'failureEvidence': _truncate_list(v.get('failureEvidence'), 5, evidence_limit),
                'missingEvidence': _truncate_list(v.get('missingEvidence'), 5, evidence_limit),
                'riskFlags': _truncate_list(v.get('riskFlags'), 5, evidence_limit),
            },
            'changedFiles': list(changed)[:30],
            'diffExcerpt': _truncate_text(diff, diff_limit),
        })
    return packets

def select_success_with_llm_comparison(*, task: str, success_criteria: str | None, candidates: list[Any], model: str | None, base_url: str | None, provider: str | None, api_key: str | None, timeout_s: int | None = None) -> dict[str, Any] | None:
    eligible={_cid(c) for c in candidates}
    if not eligible or not model or not base_url:
        return None
    backend=Backend(name='verifier-parallel-selector', provider=provider or 'openai-compatible', base_url=base_url, model=model, api_key=api_key)
    packet=build_llm_comparison_packet(candidates)
    system='You are a strict comparative selector. Return strict JSON only.'
    user=json.dumps({
        'instruction': 'Compare candidate patches against the task and success criteria. Prefer direct evidence that the riskiest or most specific requirements are satisfied. Prefer behavioural evidence over source-shape evidence when available. Penalize unresolved failure evidence, missing required outputs, or weak/indirect validation. Choose exactly one eligible candidate id.',
        'responseSchema': {'selectedCandidateId': 'candidate id from eligible list', 'reason': 'brief reason'},
        'task': task,
        'successCriteria': success_criteria,
        'eligibleCandidateIds': sorted(eligible),
        'candidates': packet,
    }, indent=2)[:60000]
    call=LLMClient().complete_json(backend, system, user, 'VerifierParallelSelection', timeout_seconds=timeout_s, estimate_cost=False)
    data=call.parsed_json or {}
    selected=data.get('selectedCandidateId') or data.get('selected_candidate_id')
    if selected not in eligible:
        raise ValueError(f'LLM comparative selector returned invalid candidate id: {selected}')
    return {'selectedCandidateId': selected, 'reason': str(data.get('reason') or data.get('reasoning') or ''), 'packet': packet}
