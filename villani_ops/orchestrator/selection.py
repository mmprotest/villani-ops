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

_CLEANUP_REQUIREMENT_RE = re.compile(r"\b(sigint|interrupt|interruption|cancel|cancellation|cleanup|clean up|shutdown|shut down|resource|rollback)\b", re.IGNORECASE)
_SEVERE_RISK_RE = re.compile(r"\b(missing cleanup|missing cancellation|broad exception|swallow|fake test|hardcoded|ignored failure|unrelated|untested async|resource leak)\b", re.IGNORECASE)
_TEST_RE = re.compile(r"\b(pytest|cargo test|npm test|go test|test|validation|verified|passed|failed)\b", re.IGNORECASE)


def _result_label(v: dict[str, Any]) -> str:
    r = v.get('result')
    if r == 1 or str(v.get('verdict') or '').lower() == 'success':
        return 'pass'
    if r == 0 or str(v.get('verdict') or '').lower() in {'failure', 'failed', 'fail'}:
        return 'fail'
    return 'unknown'


def _strength(text: Any) -> str:
    t = str(text or '')
    if _STRONG_SUCCESS_EVIDENCE_RE.search(t):
        return 'strong'
    if t.strip():
        return 'medium'
    return 'weak'


def _evidence_item(requirement: str, evidence: Any) -> dict[str, str]:
    return {'requirement': str(requirement or 'unspecified requirement'), 'evidence': str(evidence or ''), 'strength': _strength(evidence)}


def _candidate_tests(c: Any, v: dict[str, Any]) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    for item in _list_field(v, 'successEvidence') + _list_field(v, 'failureEvidence'):
        text = str(item)
        if _TEST_RE.search(text):
            tests.append({'command': text[:300], 'passed': item in _list_field(v, 'successEvidence'), 'source': 'repo'})
    return tests


def build_candidate_evidence_matrix(candidates: list[Any], selected_candidate_id: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in candidates:
        v = _vr(c); cid = str(_cid(c) or '')
        reqs = [r for r in _list_field(v, 'requirementResults') if isinstance(r, dict)]
        success = _list_field(v, 'successEvidence')
        missing = [str(x) for x in _list_field(v, 'missingEvidence')]
        risks = [str(x) for x in _list_field(v, 'riskFlags')]
        unsatisfied = [str(r.get('requirement') or r.get('id') or 'unsatisfied requirement') for r in reqs if r.get('status') == 'unsatisfied']
        missing_flags = missing + unsatisfied
        cleanup_reqs = [str(r.get('requirement') or r.get('id') or '') for r in reqs if _CLEANUP_REQUIREMENT_RE.search(str(r.get('requirement') or r.get('id') or ''))]
        evidence_text = ' '.join(map(str, success + _list_field(v, 'recoveredFailures') + [v.get('directEvidenceForCriticalRequirement') or '']))
        if cleanup_reqs and not _CLEANUP_REQUIREMENT_RE.search(evidence_text):
            missing_flags.append('missing cleanup/cancellation/interruption/shutdown evidence')
        if v.get('criticalRequirementCovered') is False:
            missing_flags.append('critical requirement was not covered')
        elif v.get('criticalRequirementCovered') is True and v.get('criticalRequirementCoverageProven') is not True:
            missing_flags.append('critical requirement coverage was not proven by same-condition evidence')
        direct = [_evidence_item((reqs[0].get('requirement') if reqs else v.get('criticalRequirement')) or 'task behavior', x) for x in success]
        if v.get('directEvidenceForCriticalRequirement'):
            direct.append(_evidence_item(v.get('criticalRequirement') or 'critical requirement', v.get('directEvidenceForCriticalRequirement')))
        source = []
        changed = getattr(c, 'changed_files', None) or (c.get('changedFiles') if isinstance(c, dict) else None) or []
        if changed:
            source.append(_evidence_item('changed files', ', '.join(map(str, changed))))
        satisfied = sum(1 for r in reqs if r.get('status') == 'satisfied')
        total = max(1, len(reqs))
        if not reqs and v.get('criticalRequirementCovered') is True:
            satisfied = 1
            total = 1
        if v.get('criticalRequirementCoverageProven') is True:
            satisfied = max(satisfied, total)
        tests = _candidate_tests(c, v)
        repo_tests = sum(1 for t in tests if t['source'] == 'repo' and t['passed'] is True)
        candidate_tests = sum(1 for t in tests if t['source'] == 'candidate' and t['passed'] is True)
        severe = sum(1 for r in risks + missing_flags if _SEVERE_RISK_RE.search(r))
        risk_penalty = len(risks) * 1.5 + len(missing_flags) * 2 + severe * 5
        score = {
            'direct_behavioral': float(sum({'strong': 3, 'medium': 2, 'weak': 1}[e['strength']] for e in direct)),
            'repo_tests': float(repo_tests * 4),
            'candidate_tests': float(candidate_tests),
            'source_inference': float(len(source)),
            'requirement_coverage': float(10 * satisfied / total),
            'risk_penalty': float(risk_penalty),
            'final': 0.0,
        }
        score['final'] = score['direct_behavioral'] + score['repo_tests'] + score['candidate_tests'] + score['source_inference'] + score['requirement_coverage'] - score['risk_penalty']
        row = {'candidate_id': cid, 'verifier_result': _result_label(v), 'verifier_confidence': v.get('confidence'), 'commands_run': [str(x) for x in _list_field(v, 'toolsUsed')], 'tests_run': tests, 'files_changed': [str(x) for x in changed], 'direct_behavioral_evidence': direct, 'source_level_inference_evidence': source, 'missing_requirement_flags': missing_flags, 'risk_flags': risks, 'evidence_score': score, 'selection_status': 'selected' if cid == selected_candidate_id else 'rejected', 'final_selection_reason': ''}
        rows.append(row)
    return rows


def _evidence_rank_components(row: dict[str, Any]) -> tuple[Any, ...]:
    s = row['evidence_score']
    severe = sum(1 for r in row['risk_flags'] + row['missing_requirement_flags'] if _SEVERE_RISK_RE.search(str(r)))
    return (-severe, -len(row['missing_requirement_flags']), -len(row['risk_flags']), s['direct_behavioral'], s['repo_tests'], s['candidate_tests'], s['source_inference'], s['requirement_coverage'], -s['risk_penalty'], float(row['verifier_confidence'] or 0))

def _evidence_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return _evidence_rank_components(row) + (-sum(ord(ch) for ch in row['candidate_id']),)


def rank_candidates_by_evidence(candidates: list[Any]) -> list[dict[str, Any]]:
    rows = build_candidate_evidence_matrix(candidates)
    return sorted(rows, key=_evidence_rank_key, reverse=True)


def _finalize_evidence_reasons(rows: list[dict[str, Any]], winner_id: str | None) -> list[dict[str, Any]]:
    winner = next((r for r in rows if r['candidate_id'] == winner_id), None)
    for r in rows:
        if r['candidate_id'] == winner_id:
            r['selection_status'] = 'selected'
            r['final_selection_reason'] = f"Selected because it had the strongest evidence-ranked coverage: coverage={r['evidence_score']['requirement_coverage']:.2f}, direct_behavioral={r['evidence_score']['direct_behavioral']:.2f}, risk_penalty={r['evidence_score']['risk_penalty']:.2f}."
        else:
            r['selection_status'] = 'rejected'
            gaps = '; '.join(r['missing_requirement_flags'][:3]) or 'lower evidence-ranked score'
            if winner:
                r['final_selection_reason'] = f"Rejected because {gaps}; {winner_id} had stronger requirement coverage or lower risk."
            else:
                r['final_selection_reason'] = f"Rejected because {gaps}."
    return rows


def write_candidate_evidence_matrix(path: str | Path, matrix: list[dict[str, Any]]) -> Path:
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(matrix, indent=2, default=str), encoding='utf-8'); return p


def write_selection_report(path: str | Path, matrix: list[dict[str, Any]], winner_id: str | None) -> Path:
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    winner = next((r for r in matrix if r['candidate_id'] == winner_id), None)
    lines = ['# Selection Report', '', '## Winner', f"- Candidate: {winner_id or ''}", f"- Reason: {(winner or {}).get('final_selection_reason','No winner selected.')}"]
    llm_recommended = next((r for r in matrix if r.get('llm_comparison_recommended')), None)
    if llm_recommended:
        note = llm_recommended.get('llm_comparison_advisory_note')
        if not note:
            note = 'LLM comparison matched the evidence-ranked winner.' if llm_recommended['candidate_id'] == winner_id else f"LLM comparison recommended {llm_recommended['candidate_id']}, but evidence-ranked selector selected {winner_id} because {(winner or {}).get('final_selection_reason','it had stronger evidence.')}"
        lines += ['', '## LLM Comparison Advisory', f"- Recommended candidate: {llm_recommended['candidate_id']}", f"- Evidence-ranked winner: {winner_id or ''}", '- Used for final decision: no', f"- Notes: {note}"]
    lines += ['', '## Candidate Ranking', 'candidate_id | verifier_result | coverage | direct_behavioral | repo_tests | source_inference | risk_flags | status | reason', '--- | --- | ---: | ---: | ---: | ---: | --- | --- | ---']
    for r in sorted(matrix, key=_evidence_rank_key, reverse=True):
        s=r['evidence_score']; lines.append(f"{r['candidate_id']} | {r['verifier_result']} | {s['requirement_coverage']:.2f} | {s['direct_behavioral']:.2f} | {s['repo_tests']:.2f} | {s['source_inference']:.2f} | {'; '.join(r['risk_flags'])} | {r['selection_status']} | {r['final_selection_reason']}")
    lines += ['', '## Why the winner won', (winner or {}).get('final_selection_reason','No winner selected.'), '', '## Why other candidates lost']
    for r in matrix:
        if r['candidate_id'] != winner_id:
            lines += [f"### {r['candidate_id']}", r['final_selection_reason']]
    gaps=[f"{r['candidate_id']}: {g}" for r in matrix for g in r['missing_requirement_flags']]
    lines += ['', '## Evidence gaps', *(f"- {g}" for g in (gaps or ['No missing requirement flags recorded.']))]
    p.write_text('\n'.join(lines)+'\n', encoding='utf-8'); return p

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
    return {'candidateId':_cid(c),'result':v.get('result'),'verdict':v.get('verdict'),'confidence':v.get('confidence'),'recommendedAction':v.get('recommendedAction'),'criticalRequirement':v.get('criticalRequirement'),'directEvidenceForCriticalRequirement':v.get('directEvidenceForCriticalRequirement'),'criticalRequirementCovered':v.get('criticalRequirementCovered'),'criticalRequirementCoverageProven':v.get('criticalRequirementCoverageProven'),'criticalRequirementEvidenceMatch':v.get('criticalRequirementEvidenceMatch'),'warnings':v.get('warnings'),'traceDir':v.get('traceDir') or v.get('trace_dir')}

def _recommended_action(c):
    return str((_vr(c).get('recommendedAction') or '')).strip().lower()

def _critical_covered(c) -> bool:
    return _vr(c).get('criticalRequirementCovered') is True

def _critical_proven(c) -> bool:
    return _vr(c).get('criticalRequirementCoverageProven') is True

def _strong_accept(c) -> bool:
    return _recommended_action(c)=='accept' and _critical_covered(c) and _critical_proven(c)

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
    critical_covered = 1 if _critical_covered(candidate) else 0
    critical_proven = 1 if _critical_proven(candidate) else 0
    strong_accept = 1 if _strong_accept(candidate) else 0
    key = (
        critical_proven,
        critical_covered,
        strong_accept,
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
        'criticalRequirementCoverageProven': bool(critical_proven),
        'criticalRequirementCovered': bool(critical_covered),
        'strongAccept': bool(strong_accept),
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
            'criticalRequirementCoverageProven': row['qualityKey'][0],
            'criticalRequirementCovered': row['qualityKey'][1],
            'strongAccept': row['qualityKey'][2],
            'uncertaintyRank': row['qualityKey'][3],
            'negativeFailureEvidenceCount': row['qualityKey'][4],
            'negativeMissingEvidenceCount': row['qualityKey'][5],
            'negativeRiskFlagCount': row['qualityKey'][6],
            'satisfiedRequirementCount': row['qualityKey'][7],
            'negativeUnsatisfiedRequirementCount': row['qualityKey'][8],
            'behavioralSuccessEvidenceCount': row['qualityKey'][9],
            'toolCount': row['qualityKey'][10],
            'confidence': row['qualityKey'][11],
        }
    return rows

def _pick_by_quality(bucket: list[Any], rng: random.Random) -> tuple[Any|None, list[str], bool]:
    if not bucket: return None, [], False
    ranked_rows = rank_candidates_by_evidence(bucket)
    best_id = ranked_rows[0]['candidate_id']
    best_key = _evidence_rank_components(ranked_rows[0])
    best = [r['candidate_id'] for r in ranked_rows if _evidence_rank_components(r) == best_key]
    return next(c for c in bucket if _cid(c) == best_id), best, len(best) > 1

def select_winner(candidates:list[Any], seed:int, on_all_fail:str='fail')->SelectionResult:
    if on_all_fail not in VALID_ON_ALL_FAIL: raise ValueError('invalid on_all_fail')
    rng=random.Random(seed); allc=[_summary(c) for c in candidates]; candidate_quality=_quality_rows(candidates)
    successes=[c for c in candidates if _vr(c).get('result')==1]
    zeros=[c for c in candidates if _vr(c).get('result')==0]
    errors=[c for c in candidates if _vr(c).get('result') not in (0,1)]
    fallback=None; quality_applied=False
    if successes:
        accepted=[c for c in successes if _strong_accept(c)]
        bucket=accepted or successes
        win,pool,random_tie=_pick_by_quality(bucket,rng); quality_applied=len(bucket)>1
        if accepted:
            reason=(f'Selected {_cid(win)} by verifier quality tie-break among candidates with result = 1 and recommendedAction = accept with evidence-proven critical-requirement coverage.' if not random_tie else 'Selected randomly among strong accept-recommended candidates tied on verifier result and verifier quality key.')
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
                'criticalRequirement': v.get('criticalRequirement'),
                'directEvidenceForCriticalRequirement': v.get('directEvidenceForCriticalRequirement'),
                'criticalRequirementCovered': v.get('criticalRequirementCovered'),
                'criticalRequirementEvidenceRefs': _truncate_list(v.get('criticalRequirementEvidenceRefs'), 10, evidence_limit),
                'criticalRequirementCoverageProven': v.get('criticalRequirementCoverageProven'),
                'criticalRequirementEvidenceMatch': v.get('criticalRequirementEvidenceMatch') if isinstance(v.get('criticalRequirementEvidenceMatch'), dict) else {},
                'warnings': _truncate_list(v.get('warnings'), 5, evidence_limit),
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
