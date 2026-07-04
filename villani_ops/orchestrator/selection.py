from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import random

POLICY='binary_verifier_random_tie'
VALID_ON_ALL_FAIL={'fail','random','best-confidence'}

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
    return {'candidateId':_cid(c),'result':v.get('result'),'verdict':v.get('verdict'),'confidence':v.get('confidence'),'traceDir':v.get('traceDir') or v.get('trace_dir')}

def select_winner(candidates:list[Any], seed:int, on_all_fail:str='fail')->SelectionResult:
    if on_all_fail not in VALID_ON_ALL_FAIL: raise ValueError('invalid on_all_fail')
    rng=random.Random(seed); allc=[_summary(c) for c in candidates]
    successes=[c for c in candidates if _vr(c).get('result')==1]
    zeros=[c for c in candidates if _vr(c).get('result')==0]
    errors=[c for c in candidates if _vr(c).get('result') not in (0,1)]
    fallback=None
    if successes:
        pool=[_cid(c) for c in successes]; win=rng.choice(successes)
        reason='Selected randomly among candidates with verifier result = 1.'
    elif on_all_fail=='fail':
        return SelectionResult('villani-ops-verifier-parallel-selection-v1',POLICY,seed,on_all_fail,None,None,False,[],allc,'No candidates had verifier result = 1; on-all-fail=fail skipped integration.')
    elif on_all_fail=='random':
        bucket=zeros or errors; pool=[_cid(c) for c in bucket]; win=rng.choice(bucket) if bucket else None; fallback='all-fail-random'; reason='All candidates failed; selected random fallback.'
    else:
        if zeros:
            maxc=max(_confidence(c) for c in zeros); bucket=[c for c in zeros if _confidence(c)==maxc]; pool=[_cid(c) for c in bucket]; win=rng.choice(bucket); reason='All candidates failed; selected result = 0 candidate with highest verifier confidence.'
        else:
            bucket=errors; pool=[_cid(c) for c in bucket]; win=rng.choice(bucket) if bucket else None; reason='All candidates had verifier errors; selected random error fallback.'
        fallback='all-fail-best-confidence'
    v=_vr(win) if win else {}; return SelectionResult('villani-ops-verifier-parallel-selection-v1',POLICY,seed,on_all_fail,_cid(win) if win else None,v.get('result'),len(pool)>1,pool,allc,reason,fallback)
