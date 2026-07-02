from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from dataclasses import dataclass
import json, re

SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|password|authorization)")
SECRET_VALUE_RE = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._-]+|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})")

@dataclass(frozen=True)
class NormalizedCandidateEvidence:
    patch: str
    changed_files: list[str]
    patch_status: str = 'unknown'

def _truthy_patch_value(v: Any) -> bool:
    if v is True: return True
    if isinstance(v, str): return bool(v.strip())
    if isinstance(v, (list, tuple, set, dict)): return bool(v)
    return False

def normalize_candidate_evidence(candidate: Mapping[str, Any]) -> NormalizedCandidateEvidence:
    changed=[]
    for key in ('changed_files','files_changed','modified_files'):
        vals=candidate.get(key)
        if isinstance(vals, list):
            changed += [str(x) for x in vals if str(x).strip()]
    meta=candidate.get('patch_metadata') or candidate.get('patch')
    if isinstance(meta, dict):
        for key in ('changed_files','files_changed','modified_files'):
            vals=meta.get(key)
            if isinstance(vals, list): changed += [str(x) for x in vals if str(x).strip()]
    changed=list(dict.fromkeys(changed))
    status=str(candidate.get('accepted_patch_application_status') or '').lower()
    explicit_failed=status in {'failed','rejected','error','errored'}
    has_patch=any(_truthy_patch_value(candidate.get(k)) for k in ('patch_produced','has_patch','patch','patch_path','diff_path')) or bool(changed)
    if status and not explicit_failed: has_patch=True
    artifacts=candidate.get('artifacts') or candidate.get('artifact_references') or candidate.get('artifact_paths')
    artifact_blob=json.dumps(artifacts).lower() if artifacts else ''
    if artifact_blob and any(x in artifact_blob for x in ('.patch','.diff','diff','patch')): has_patch=True
    patch='yes' if has_patch else ('no' if explicit_failed else 'unknown')
    return NormalizedCandidateEvidence(patch=patch, changed_files=changed, patch_status=status or 'unknown')

PROVIDER_FAILURE_KINDS = {'backend_connection_error','backend_timeout','backend_http_error','backend_response_error'}
EVENTS = {'run_started':'Run started','model_request_started':'Model request started','provider_failure':'Provider failure','backend_failure':'Backend failure','model_response_received':'Model response received','tool_call_started':'Tool call started','tool_call_completed':'Tool call completed','tool_call_failed':'Tool call failed','investigation_submitted':'Investigation submitted','classification_submitted':'Classification submitted','plan_submitted':'Plan submitted','decomposition_submitted':'Decomposition submitted','decomposition_validation_completed':'Decomposition validation completed','execution_path_selected':'Execution path selected','candidate_attempt_started':'Candidate attempt started','candidate_attempt_completed':'Candidate attempt completed','candidate_attempt_failed':'Candidate attempt failed','subtask_attempt_started':'Subtask attempt started','subtask_attempt_completed':'Subtask attempt completed','subtask_attempt_failed':'Subtask attempt failed','subtask_attempt_reviewed':'Subtask attempt reviewed','subtask_accepted':'Subtask accepted','subtask_failed':'Subtask failed','validation_started':'Validation started','validation_completed':'Validation completed','validation_failed':'Validation failed','candidate_attempt_reviewed':'Candidate attempt reviewed','selection_completed':'Selection completed','run_finalized':'Final decision','decomposition_deadlock_detected':'Decomposition deadlock detected','candidate_fallback_started':'Candidate fallback started','integration_started':'Integration started','integration_completed':'Integration completed','integration_failed':'Integration failed','recovery_injected':'Recovery injected','recovery_deterministic_action_executed':'Recovery action executed'}
STATUS_BY_EVENT = {'run_started':'running','candidate_attempt_started':'running','subtask_attempt_started':'running','validation_started':'running','integration_started':'running','model_request_started':'running','tool_call_started':'running','candidate_fallback_started':'running','candidate_attempt_completed':'completed','subtask_attempt_completed':'completed','validation_completed':'completed','integration_completed':'completed','candidate_attempt_failed':'failed','subtask_attempt_failed':'failed','subtask_failed':'failed','validation_failed':'failed','integration_failed':'failed','subtask_accepted':'accepted','candidate_attempt_reviewed':'completed','subtask_attempt_reviewed':'completed','selection_completed':'selected','run_finalized':'completed','provider_failure':'failed','backend_failure':'failed','tool_call_failed':'failed','model_response_received':'completed','tool_call_completed':'completed','decomposition_deadlock_detected':'blocked'}

def _read_json(path: Path, default: Any=None) -> Any:
    try:
        if not path.exists() or not path.read_text(encoding='utf-8').strip(): return default
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception: return default

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out=[]
    if not path.exists(): return out
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            if line.strip(): out.append(json.loads(line))
        except Exception: continue
    return out

def _redact(value: Any) -> Any:
    if isinstance(value, dict): return {k: ('***REDACTED***' if SECRET_KEY_RE.search(str(k)) else _redact(v)) for k,v in value.items()}
    if isinstance(value, list): return [_redact(v) for v in value]
    if isinstance(value, str): return SECRET_VALUE_RE.sub('***REDACTED***', value)[:5000]
    return value

def _duration(start: str|None, end: str|None=None) -> float|None:
    if not start: return None
    try:
        s=datetime.fromisoformat(start.replace('Z','+00:00')); e=datetime.fromisoformat(end.replace('Z','+00:00')) if end else datetime.now(timezone.utc)
        return max(0.0, round((e-s).total_seconds(), 2))
    except Exception: return None

def _payload(ev): return ev.get('payload') or {}
def _attempt_id(ev): return ev.get('attempt_id') or _payload(ev).get('attempt_id') or _payload(ev).get('candidate_id') or _payload(ev).get('selected_attempt_id')
def _subtask_id(ev): return ev.get('subtask_id') or _payload(ev).get('subtask_id')

DISPLAY_LABELS = {
    'best_effort_tournament_selection': 'Best effort selection',
    'adaptive_orchestrator_forced_tournament_execution_path': 'Forced tournament path',
    'validated_winner': 'Validated winner',
    'manual_acceptance': 'Manual acceptance',
}

def human_label(value: Any) -> str:
    s=str(value or '').strip()
    if not s: return 'Unavailable'
    if s in DISPLAY_LABELS: return DISPLAY_LABELS[s]
    return re.sub(r'[_-]+',' ',s).strip().capitalize()

def humanize_id(id: str) -> str:
    s=str(id or '')
    m=re.fullmatch(r'(?:candidate[_-]?)?(\d+)', s, re.I)
    if m: return f"Candidate {int(m.group(1)):03d}"
    m=re.fullmatch(r'(?:st|subtask)[_-]?(\d+)', s, re.I)
    if m: return f"Subtask {int(m.group(1))}"
    if s.startswith('candidate_'): return 'Candidate ' + s.split('_')[-1].zfill(3)
    if s.startswith('subtask_'): return 'Subtask ' + s.split('_')[-1]
    return re.sub(r'[_-]+',' ',s).strip().title() or 'Step'

def _subtitle(ev: dict[str,Any]) -> str:
    p=_payload(ev); t=ev.get('type') or ''; bits=[]
    if t=='classification_submitted': bits=[p.get('difficulty'), p.get('category')]
    elif t in {'plan_submitted','execution_path_selected'}: bits=[p.get('execution_path') or p.get('path') or p.get('plan_type')]
    elif t in {'provider_failure','backend_failure'}:
        bits=[human_label(p.get('failure_kind') or p.get('kind')), p.get('failure_message') or p.get('message'), p.get('backend_url') or p.get('backend_name') or p.get('backend')]
        if p.get('recoverable') is not None: bits.append('recoverable='+str(p.get('recoverable')).lower())
    elif t in {'model_request_started','model_response_received'}:
        bits=[p.get('backend_name') or p.get('backend') or p.get('backend_url'), p.get('model')]
    elif t.startswith('tool_call_'):
        bits=[p.get('tool') or p.get('name'), p.get('status')]
    elif 'candidate' in t or t.startswith('validation_') or t=='selection_completed': bits=[humanize_id(_attempt_id(ev) or '')]
    elif t.startswith('subtask_'): bits=[humanize_id(_subtask_id(ev) or ''), _attempt_id(ev) and 'attempt '+str(_attempt_id(ev)).split('_')[-1]]
    elif t.startswith('integration_'): bits=[p.get('status')]
    if p.get('exit_code') is not None: bits.append(f"exit_code={p.get('exit_code')}")
    if p.get('decision'): bits.append(str(p.get('decision')))
    return ' / '.join(str(b) for b in bits if b)

def _timeline(events):
    items=[]
    for i, ev in enumerate(sorted(events, key=lambda e: e.get('timestamp') or '')):
        typ=ev.get('type')
        if typ not in EVENTS: continue
        p=_redact(_payload(ev)); dur=p.get('duration_seconds') or p.get('duration')
        items.append({'id':ev.get('event_id') or f'event_{i}', 'timestamp':ev.get('timestamp'), 'type':typ, 'title':EVENTS[typ], 'subtitle':_subtitle(ev), 'status':STATUS_BY_EVENT.get(typ,'completed'), 'duration_seconds':dur, 'attempt_id':_attempt_id(ev), 'subtask_id':_subtask_id(ev)})
    return items

def _event_types(events): return {e.get('type') for e in events}

def _progress(state, events):
    status=str(state.get('status') or '').lower(); types=_event_types(events)
    if 'run_finalized' in types or status in {'failed','rejected','accepted','completed'}: return 100,'Finalized'
    decomposed=bool(types & {'decomposition_submitted','subtask_attempt_started','subtask_attempt_completed','integration_started'})
    stages=[('run_started',5,'Run started'),('investigation_submitted',10 if decomposed else 12,'Investigating'),('classification_submitted',15 if decomposed else 18,'Classifying'),('plan_submitted',20 if decomposed else 25,'Planning')]
    if decomposed:
        stages += [('decomposition_submitted',30,'Decomposing'),('decomposition_validation_completed',38,'Validating decomposition'),('execution_path_selected',45,'Selecting path')]
    else: stages += [('execution_path_selected',32,'Selecting path'),('candidate_attempt_started',40,'Running candidates'),('candidate_attempt_completed',55,'Candidates complete')]
    pct=0; label='Waiting for events'
    for typ,val,lab in stages:
        if typ in types: pct,label=max(pct,val),lab
    if decomposed:
        subs={_subtask_id(e) for e in events if _subtask_id(e)}; total=max(len(subs), int(state.get('total_subtasks') or 0), 0)
        done={_subtask_id(e) for e in events if e.get('type') in {'subtask_accepted','subtask_failed','subtask_attempt_completed','subtask_attempt_failed'} and _subtask_id(e)}
        if 'subtask_attempt_started' in types: pct,label=max(pct,55),'Running subtasks'
        if total: pct,label=max(pct, int(55+20*len(done)/total)),'Running subtasks'
        for typ,val,lab in [('integration_completed',82,'Integrating'),('validation_completed',88,'Validating'),('candidate_attempt_reviewed',92,'Reviewing'),('subtask_attempt_reviewed',92,'Reviewing'),('selection_completed',96,'Selecting winner')]:
            if typ in types: pct,label=max(pct,val),lab
    else:
        cands={_attempt_id(e) for e in events if _attempt_id(e) and 'candidate' in (e.get('type') or '')}; req=max(len(cands), int(state.get('requested_candidates') or 0), 0)
        done={_attempt_id(e) for e in events if e.get('type') in {'candidate_attempt_completed','candidate_attempt_failed'} and _attempt_id(e)}
        if req: pct,label=max(pct, int(40+15*len(done)/req)),'Running candidates'
        for typ,val,lab in [('validation_completed',70,'Validating'),('candidate_attempt_reviewed',82,'Reviewing'),('selection_completed',92,'Selecting winner')]:
            if typ in types: pct,label=max(pct,val),lab
    return min(pct,99),label

TOKEN_IN_KEYS=('input_tokens','prompt_tokens','tokens_in','total_input_tokens')
TOKEN_OUT_KEYS=('output_tokens','completion_tokens','tokens_out','total_output_tokens')
TOKEN_TOTAL_KEYS=('total_tokens','tokens_total')
COST_KEYS=('total_cost','cost','cost_usd','estimated_cost','amount','usd')

def _num(d,*keys):
    for k in keys:
        v=d.get(k) if isinstance(d,dict) else None
        if isinstance(v,(int,float)): return v
        if isinstance(v,str):
            try: return float(v)
            except ValueError: pass
    return 0

def _meaningful(src: Any) -> bool:
    if not isinstance(src, dict): return False
    for k in TOKEN_TOTAL_KEYS+TOKEN_IN_KEYS+TOKEN_OUT_KEYS+COST_KEYS+('calls_count','unavailable_calls_count'):
        try:
            if float(src.get(k) or 0) > 0: return True
        except (TypeError, ValueError): pass
    return False

def _normalize_usage_record(src: dict[str, Any]) -> dict[str, Any]:
    inp=_num(src,*TOKEN_IN_KEYS); out=_num(src,*TOKEN_OUT_KEYS); total=_num(src,*TOKEN_TOTAL_KEYS) or inp+out; amount=_num(src,*COST_KEYS)
    return {'input_tokens':inp,'output_tokens':out,'total_tokens':total,'total_cost':amount,'calls_count':int(_num(src,'calls_count','call_count') or (1 if (total or amount or src.get('usage_source')=='unavailable') else 0)),'unavailable_calls_count':int(_num(src,'unavailable_calls_count','unavailable_calls','missing_usage_calls') or (1 if src.get('usage_source')=='unavailable' or src.get('unavailable') else 0)),'estimated':bool(src.get('estimated') or src.get('cost_estimated') or src.get('estimated_cost')),'pricing_missing':bool(src.get('pricing_missing') or src.get('cost_unavailable') or src.get('unavailable_reason')=='pricing_missing'),'usage_missing':bool(src.get('usage_missing') or src.get('usage_source')=='unavailable' or src.get('unavailable_reason'))}

def _call_key(r: dict[str, Any], source: str, index: int) -> str:
    for k in ('call_id','request_id','model_call_id','event_id','id'):
        if r.get(k): return f'id:{r[k]}'
    bits=[r.get('candidate_id') or r.get('attempt_id'), r.get('role') or r.get('phase'), r.get('attempt'), r.get('started_at') or r.get('timestamp')]
    if all(bits): return 'candidate:' + ':'.join(map(str,bits))
    return f'{source}:{index}'

def aggregate_usage_jsonl(run_dir: Path) -> dict:
    rows=_read_jsonl(Path(run_dir)/'usage.jsonl')
    out={'input_tokens':0,'output_tokens':0,'total_tokens':0,'input_cost':0.0,'output_cost':0.0,'total_cost':0.0,'calls_count':0,'unavailable_calls_count':0,'by_role':{},'by_backend':{},'by_model':{},'source':'usage.jsonl','call_level':False,'diagnostics':[]}
    seen=set()
    def add_bucket(bucket, r):
        n=_normalize_usage_record(r)
        bucket['input_tokens']=bucket.get('input_tokens',0)+n['input_tokens']
        bucket['output_tokens']=bucket.get('output_tokens',0)+n['output_tokens']
        bucket['total_tokens']=bucket.get('total_tokens',0)+n['total_tokens']
        bucket['input_cost']=bucket.get('input_cost',0.0)+float(_num(r,'input_cost'))
        bucket['output_cost']=bucket.get('output_cost',0.0)+float(_num(r,'output_cost'))
        bucket['total_cost']=bucket.get('total_cost',0.0)+n['total_cost']
        bucket['calls_count']=bucket.get('calls_count',0)+max(1,n['calls_count'])
        bucket['unavailable_calls_count']=bucket.get('unavailable_calls_count',0)+n['unavailable_calls_count']
        bucket['estimated']=bucket.get('estimated',False) or n['estimated']
        bucket['pricing_missing']=bucket.get('pricing_missing',False) or n['pricing_missing']
        bucket['usage_missing']=bucket.get('usage_missing',False) or n['usage_missing']
    for i,r in enumerate(rows):
        if not isinstance(r,dict): continue
        key=_call_key(r,'usage.jsonl',i)
        out['call_level']=True
        if key in seen:
            out['diagnostics'].append('Duplicate call usage ignored.')
            continue
        seen.add(key)
        add_bucket(out,r)
        for field, name in (('role','by_role'),('backend_name','by_backend'),('model','by_model')):
            val=r.get(field)
            if val:
                out[name].setdefault(str(val),{})
                add_bucket(out[name][str(val)],r)
    return out

def _summary_from_json(path: Path, source: str) -> dict:
    data=_read_json(path, {}) or {}
    if not isinstance(data,dict): return {}
    summary=data.get('summary') if isinstance(data.get('summary'),dict) else data
    if not isinstance(summary,dict): return {}
    out={**summary,'source':source,'call_level':False}
    if isinstance(data.get('records'),list): out['has_records']=True
    return out

def _candidate_usage_sources(state: dict) -> list[dict]:
    sources=[]
    for c in state.get('candidates',[]) if isinstance(state.get('candidates'),list) else []:
        if isinstance(c,dict):
            for k in ('usage','usage_summary','telemetry','runner_telemetry'):
                if isinstance(c.get(k),dict): sources.append({**c[k], 'source':f'candidate:{c.get("attempt_id") or c.get("candidate_id")}'})
    return sources

def _canonical_usage_source(run_dir: Path, state: dict, digest: dict) -> dict:
    diagnostics=[]
    jsonl=aggregate_usage_jsonl(run_dir)
    usage_summary=_summary_from_json(run_dir/'usage.json','usage.json')
    cost_summary=_summary_from_json(run_dir/'cost_summary.json','cost_summary.json')
    summaries=[x for x in (usage_summary,cost_summary) if _meaningful(x) or x.get('pricing_missing') or x.get('usage_missing')]
    if jsonl.get('call_level') and (_meaningful(jsonl) or jsonl.get('calls_count')):
        if summaries: diagnostics.append('Usage summary used canonical source; duplicate summary ignored.')
        jsonl['diagnostics']=(jsonl.get('diagnostics') or [])+diagnostics
        return jsonl
    if _meaningful(usage_summary) or usage_summary.get('pricing_missing') or usage_summary.get('usage_missing'):
        if _meaningful(cost_summary): diagnostics.append('Usage summary used canonical source; duplicate summary ignored.')
        usage_summary['diagnostics']=diagnostics
        return usage_summary
    if _meaningful(cost_summary) or cost_summary.get('pricing_missing') or cost_summary.get('usage_missing'):
        cost_summary['diagnostics']=diagnostics
        return cost_summary
    for src in [state.get('usage_summary') if isinstance(state,dict) else {}, digest.get('usage') if isinstance(digest,dict) else {}, *_candidate_usage_sources(state), state.get('runner_telemetry') if isinstance(state.get('runner_telemetry'),dict) else {}, state.get('telemetry') if isinstance(state.get('telemetry'),dict) else {}]:
        if isinstance(src,dict) and (_meaningful(src) or src.get('pricing_missing') or src.get('usage_missing')):
            return src
    return {}

def _usage(run_dir, state, digest):
    src=_canonical_usage_source(Path(run_dir),state,digest)
    n=_normalize_usage_record(src) if src else {'input_tokens':0,'output_tokens':0,'total_tokens':0,'total_cost':0,'calls_count':0,'unavailable_calls_count':0,'estimated':False,'pricing_missing':False,'usage_missing':False}
    inp=n['input_tokens']; out=n['output_tokens']; total=n['total_tokens'] or inp+out; calls=n['calls_count']; unavailable=n['unavailable_calls_count']; amount=float(n['total_cost'] or 0)
    if unavailable and amount: cost_status='partial'; reason='Some calls were missing usage data'
    elif n.get('estimated') and amount: cost_status='estimated'; reason='Estimated from token usage and configured pricing'
    elif amount: cost_status='available'; reason=None
    elif calls and total and not unavailable and not n.get('pricing_missing'): cost_status='zero'; reason=None
    else:
        cost_status='unavailable'; reasons=[]
        if not total or n.get('usage_missing') or unavailable: reasons.append('Usage data missing')
        if total or n.get('pricing_missing') or unavailable: reasons.append('Backend pricing data missing')
        reason='; '.join(dict.fromkeys(reasons or ['Usage data missing']))
    return {'input_tokens':inp,'output_tokens':out,'total_tokens':total,'input_cost':0,'output_cost':0,'total_cost':amount,'calls_count':calls,'unavailable_calls_count':unavailable,'diagnostics':src.get('diagnostics',[]) if isinstance(src,dict) else [],'source':src.get('source') if isinstance(src,dict) else None,'tokens':{'status':'available' if total else 'unavailable','total':total or None,'input':inp or None,'output':out or None},'cost':{'status':cost_status,'amount':amount if (amount or calls) else None,'currency':'USD','reason':reason,'unavailable_calls_count':unavailable,'unavailable_calls_label':(f'{unavailable} unavailable call' + ('' if unavailable==1 else 's')) if unavailable else ''},'by_role':src.get('by_role',{}) if isinstance(src,dict) else {},'by_backend':src.get('by_backend',{}) if isinstance(src,dict) else {},'by_model':src.get('by_model',{}) if isinstance(src,dict) else {}}

def _validation_status(c):
    return (c.get('validation_status') or (c.get('validation') or {}).get('status') or ('passed' if (c.get('validation') or {}).get('passed') is True else 'failed' if (c.get('validation') or {}).get('passed') is False else 'not_run'))

def derive_decision_state(state: dict, digest: dict|None=None, candidates=None, usage=None, events=None) -> dict:
    digest=digest or {}
    status=str(state.get('status') or digest.get('status') or 'unknown').lower()
    failure_kind=state.get('failure_kind') or (state.get('final_decision') or {}).get('failure_kind')
    sel=(state.get('selection') or {}).get('selected_attempt_id') or (state.get('selection') or {}).get('selected_candidate_id') or digest.get('selected_attempt')
    cands=candidates if candidates is not None else [c for c in state.get('candidates',[]) if isinstance(c,dict)]
    winner=next((c for c in cands if c.get('attempt_id')==sel or c.get('candidate_id')==sel), {})
    warnings=[]
    if status=='failed' or failure_kind in PROVIDER_FAILURE_KINDS: kind='failed'
    elif status=='interrupted': kind='cancelled'
    elif not sel: kind='incomplete'
    else:
        val=(_validation_status(winner) or 'unknown').lower()
        rev=(winner.get('review_status') or ('passed' if winner.get('review') else 'missing')).lower()
        bad={'missing','not_run','skipped','unknown','absent','failed','unavailable',''}
        kind='accepted' if val=='passed' and rev not in bad else 'accepted_with_warnings'
        if val in bad: warnings.append('Validation did not run. Treat this result as unverified.' if val in {'missing','not_run','skipped','unknown','absent',''} else f'Validation status is {val}. Treat this result as unverified.')
        if rev in bad: warnings.append('Review did not run. Treat this result as unreviewed.' if rev in {'missing','not_run','skipped','unknown','absent','','unavailable'} else f'Review status is {rev}. Treat this result as unreviewed.')
    label={'accepted':'Accepted','accepted_with_warnings':'Accepted with warnings','failed':'Failed','incomplete':'Incomplete','cancelled':'Cancelled'}.get(kind,'Unknown')
    severity={'accepted':'success','accepted_with_warnings':'warning','failed':'error','incomplete':'warning','cancelled':'warning'}.get(kind,'info')
    failure=state.get('failure_message') or (state.get('final_decision') or {}).get('failure_message') or ((state.get('final_decision') or {}).get('summary') if kind=='failed' else None)
    return {'state':kind,'label':label,'severity':severity,'warnings':warnings,'failure_reason':failure}

def derive_decision_summary(state: dict, digest: dict) -> dict:
    base=derive_decision_state(state,digest)
    status=str(state.get('status') or digest.get('status') or 'unknown')
    sel=(state.get('selection') or {}).get('selected_attempt_id') or (state.get('selection') or {}).get('selected_candidate_id') or digest.get('selected_attempt')
    cands=[c for c in state.get('candidates',[]) if isinstance(c,dict)]
    winner=next((c for c in cands if c.get('attempt_id')==sel), {})
    kind=base['state']; warnings=list(base.get('warnings') or [])
    if (state.get('usage_summary') or {}).get('cost_unavailable') or _num(state.get('usage_summary') or {}, 'unavailable_calls_count'):
        warnings.append('Cost is unavailable because pricing data is missing.')
    label=base['label']; failure=base.get('failure_reason')
    changed=winner.get('changed_files') or (state.get('integration') or {}).get('changed_files') or []
    return {'state':kind,'label':label,'severity':base.get('severity'),'winner':sel,'selection_basis':human_label(state.get('selection_basis') or (state.get('selection') or {}).get('selection_basis') or (state.get('selection') or {}).get('basis') or 'Unavailable'),'validation_status':human_label(_validation_status(winner)) if winner else 'Unknown','review_status':human_label(winner.get('review_status') or ('passed' if winner.get('review') else 'Unknown')),'runner_status':human_label(winner.get('runner_status') or winner.get('status') or status),'changed_files_count':len(changed),'changed_files':changed,'confidence':(state.get('tournament_ranking') or {}).get('selection_confidence') or (state.get('selection') or {}).get('confidence'),'failure_reason':human_label(failure) if failure in PROVIDER_FAILURE_KINDS else failure,'warnings':warnings,'next_step':'Start the backend server or update backend configuration.' if kind=='failed' else ('Run validation/review before trusting this result.' if kind in {'accepted_with_warnings','incomplete'} else '')}

def candidate_evidence(state: dict) -> list[dict]:
    sel=(state.get('selection') or {}).get('selected_attempt_id') or (state.get('selection') or {}).get('selected_candidate_id')
    out=[]
    for c in [x for x in state.get('candidates',[]) if isinstance(x,dict)]:
        val=_validation_status(c); rev=c.get('review_status') or ('passed' if c.get('review') else 'Unknown')
        warns=[]
        if val!='passed': warns.append('Validation did not run' if val in {'not_run','missing','unknown','',None} else f'Validation {val}')
        if rev in {'not_run','Unknown','missing','unavailable',None}: warns.append('Review did not run')
        norm=normalize_candidate_evidence(c)
        out.append({'candidate_id':c.get('attempt_id') or 'Unknown','status':human_label(c.get('status') or 'Unknown'),'patch':norm.patch,'changed_files':norm.changed_files,'runner_status':human_label(c.get('runner_status') or (f"exit {c.get('exit_code')}" if c.get('exit_code') is not None else 'Unknown')),'review_status':human_label(rev),'validation_status':human_label(val or 'Unknown'),'eligible':bool(c.get('acceptance_eligible')),'blockers':(c.get('acceptance_blockers') or [])+warns,'selected':c.get('attempt_id')==sel})
    return out

def _backend_url_from_state_events(state, events):
    for k in ('backend_url','base_url'):
        if state.get(k): return str(state[k])
    b=state.get('backend')
    if isinstance(b,dict) and b.get('base_url'): return str(b.get('base_url'))
    for ev in events:
        p=_payload(ev)
        if p.get('backend_url') or p.get('base_url'): return str(p.get('backend_url') or p.get('base_url'))
    return ''

def _backend_name_from_state_events(state, events):
    if state.get('backend_name'): return str(state.get('backend_name'))
    b=state.get('backend')
    if isinstance(b,dict) and b.get('name'): return str(b.get('name'))
    for ev in events:
        p=_payload(ev)
        if p.get('backend_name') or (p.get('backend') and not str(p.get('backend')).startswith(('http://','https://'))): return str(p.get('backend_name') or p.get('backend'))
    return ''

def _model(state, usage, events):
    for k in ('selected_model','orchestrator_model','backend_model','model'):
        if state.get(k): return str(state[k])
    b=state.get('backend')
    if isinstance(b,dict):
        if b.get('model'): return str(b['model'])
    us=state.get('usage_summary')
    if isinstance(us,dict) and isinstance(us.get('by_model'),dict) and us['by_model']: return next(iter(us['by_model']))
    for row in usage if isinstance(usage,list) else []:
        if row.get('model'): return str(row['model'])
    for ev in events:
        p=_payload(ev)
        if p.get('model'): return str(p['model'])
        if p.get('backend_model'): return str(p['backend_model'])
    return 'Unknown model'

def _provider_failure_kind(state: dict[str,Any], events: list[dict[str,Any]]) -> str|None:
    kind=state.get('failure_kind') or (state.get('final_decision') or {}).get('failure_kind')
    if kind in PROVIDER_FAILURE_KINDS: return kind
    for ev in events:
        if ev.get('type') in {'provider_failure','backend_failure'}:
            k=_payload(ev).get('failure_kind') or _payload(ev).get('kind')
            if k in PROVIDER_FAILURE_KINDS: return k
    return None

def _candidate_execution_happened(state: dict[str,Any], events: list[dict[str,Any]]) -> bool:
    if any(isinstance(c,dict) and c.get('attempt_id') for c in state.get('candidates',[]) or []): return True
    return any('candidate_attempt_' in (e.get('type') or '') or (e.get('type') or '').startswith('validation_') or e.get('type')=='selection_completed' for e in events)

def build_viewer_graph_layout(snapshot_or_state: dict[str,Any], events: list[dict[str,Any]]) -> dict[str,Any]:
    state=snapshot_or_state or {}; types=_event_types(events)
    pf_kind=_provider_failure_kind(state, events)
    if pf_kind and not _candidate_execution_happened(state, events):
        pf_event=next((e for e in events if e.get('type') in {'provider_failure','backend_failure'}), {})
        p=_payload(pf_event)
        msg=state.get('failure_message') or p.get('failure_message') or p.get('message') or ''
        backend_name=_backend_name_from_state_events(state, events); backend_url=_backend_url_from_state_events(state, events); backend=backend_name or backend_url
        nodes=[
            {'id':'run_started','label':'Run started','type':'start','row':1,'col':1,'status':'completed' if 'run_started' in types else 'pending','subtitle':'Run initialized','summary':'Run initialized','details':{}},
            {'id':'model_request','label':'Model request','type':'request','row':1,'col':2,'status':'completed' if 'model_request_started' in types else 'pending','subtitle':backend or 'Backend request','summary':backend or 'Backend request','details':{'backend':backend}},
            {'id':'provider_failure','label':'Provider failure','type':'failure','row':1,'col':3,'status':'failed','subtitle':human_label(pf_kind),'summary':human_label(pf_kind),'details':{'failure_kind':pf_kind,'failure_label':human_label(pf_kind),'failure_message':msg,'backend_name':backend_name,'backend_url':backend_url,'recoverable':state.get('recoverable', p.get('recoverable'))}},
            {'id':'failed_finalization','label':'Failed finalization','type':'finalization','row':1,'col':4,'status':'failed','subtitle':'Failed','summary':'Failed','details':{'status':state.get('status'),'failure_kind':pf_kind,'failure_label':human_label(pf_kind),'failure_message':msg}},
        ]
        edges=[{'id':'edge_run_started_model_request','source':'run_started','target':'model_request','status':'active'},{'id':'edge_model_request_provider_failure','source':'model_request','target':'provider_failure','status':'failed'},{'id':'edge_provider_failure_failed_finalization','source':'provider_failure','target':'failed_finalization','status':'failed'}]
        return {'kind':'provider_failure','nodes':nodes,'edges':edges}

    candidates=[c for c in state.get('candidates',[]) if isinstance(c,dict)]
    if candidates:
        sel=(state.get('selection') or {}).get('selected_attempt_id') or (state.get('selection') or {}).get('selected_candidate_id')
        nodes=[]; edges=[]
        def add(id,label,type,row,col,status='pending',summary='',details=None,badge=None):
            nodes.append({'id':id,'label':label,'type':type,'kind':type,'row':row,'col':col,'status':status,'subtitle':summary,'summary':summary,'details':details or {},'badge':badge,'candidate_id':details.get('candidate_id') if isinstance(details,dict) else None})
        def edge(a,b,status='active'):
            edges.append({'id':f'edge_{a}_{b}','source':a,'target':b,'status':status})
        final_label=derive_decision_state(state, {}, candidates=candidates, events=events)['label']
        final_row=len(candidates)+1
        for i,c in enumerate(candidates,1):
            cid=c.get('attempt_id') or c.get('candidate_id') or f'candidate_{i:03d}'
            hcid=humanize_id(cid)
            norm=normalize_candidate_evidence(c)
            changed=norm.changed_files
            detail={'candidate_id':cid,'stage':'candidate','status':c.get('status'),'changed_files':changed,'patch_evidence':norm.patch,'raw':c}
            add(f'{cid}_candidate',(hcid + (f' · {len(changed)} changed file{"s" if len(changed)!=1 else ""} · Patch {norm.patch}' if cid==sel else '')),'candidate',i,1,'selected' if cid==sel else human_label(c.get('status') or 'completed').lower().replace(' ','_'),('Selected winner' if cid==sel else f"{len(changed)} changed file{'s' if len(changed)!=1 else ''} · Patch {norm.patch}"),detail,'Winner' if cid==sel else None)
            rstat=c.get('runner_status') or c.get('status') or ('completed' if c.get('exit_code')==0 else 'failed' if c.get('exit_code') else 'unknown')
            add(f'{cid}_runner','Runner '+human_label(rstat).lower(),'runner',i,2,'failed' if str(rstat).lower() in {'failed','error'} else 'completed',f"exit {c.get('exit_code')}" if c.get('exit_code') is not None else 'Runner status',{'candidate_id':cid,'stage':'runner','status':rstat,'raw':c.get('runner') or c})
            rev=c.get('review_status') or (c.get('review') or {}).get('decision') or ('passed' if c.get('review') else 'unknown')
            rev_label='Review passed' if str(rev).lower() in {'pass','passed','accepted'} else ('Review failed' if str(rev).lower() in {'fail','failed','rejected'} else 'Review '+human_label(rev).lower())
            add(f'{cid}_review',rev_label,'review',i,3,'failed' if 'failed' in rev_label.lower() else 'completed',human_label(rev),{'candidate_id':cid,'stage':'review','status':rev,'raw':c.get('review') or rev},'Failed' if 'failed' in rev_label.lower() else None)
            val=_validation_status(c); val_l=str(val).lower()
            if val_l in {'not_run','missing','unknown','skipped','absent',''}: vlabel='Validation not run' if val_l!='skipped' else 'Validation skipped'; vst='missing'; badge='Warning'
            elif val_l in {'passed','pass'}: vlabel='Validation passed'; vst='completed'; badge=None
            else: vlabel='Validation failed'; vst='failed'; badge='Failed'
            add(f'{cid}_validation',vlabel,'validation',i,4,vst,('Warning: validation not run' if vst=='missing' else human_label(val)),{'candidate_id':cid,'stage':'validation','status':val,'warnings':['Validation did not run'] if vst=='missing' else [],'raw':c.get('validation') or val},badge)
            edge(f'{cid}_candidate',f'{cid}_runner'); edge(f'{cid}_runner',f'{cid}_review'); edge(f'{cid}_review',f'{cid}_validation','failed' if vst=='failed' else 'active')
            if cid==sel: edge(f'{cid}_validation','final_decision','active')
        add('final_decision','Final decision: '+final_label,'final_decision',final_row,4,'failed' if final_label=='Failed' else 'selected',final_label,{'status':state.get('status'),'final_decision':state.get('final_decision'),'selection':state.get('selection')},None)
        return {'kind':'candidate_lanes','nodes':nodes,'edges':edges,'responsive_class':'candidate-lanes'}
    decomposed=bool(state.get('decomposition') or state.get('decomposition_requested') or types & {'decomposition_submitted','subtask_attempt_started','integration_started'})
    fallback=bool(state.get('fallback_used') or 'candidate_fallback_started' in types)
    dead=bool(state.get('decomposed_execution_status') in {'blocked','failed'} or 'decomposition_deadlock_detected' in types)
    nodes=[]; edges=[]
    def counts(prefix):
        return sum(1 for e in events if (e.get('type') or '').startswith(prefix))
    def add(id,label,type,row,col,status='pending',summary='',details=None,children=None):
        nodes.append({'id':id,'label':label,'type':type,'row':row,'col':col,'status':status,'subtitle':summary,'summary':summary,'details':details or {},'children':children or []})
    def edge(a,b,status='active'):
        edges.append({'id':f'edge_{a}_{b}','source':a,'target':b,'status':status})
    add('investigation_group','Investigation','group',1,1,'completed' if 'investigation_submitted' in types else ('running' if 'run_started' in types else 'pending'),'Inspect and classify')
    add('planning_group','Planning','group',1,2,'completed' if 'plan_submitted' in types else 'pending','Plan orchestration')
    prev='planning_group'; edge('investigation_group','planning_group')
    if decomposed:
        subs=state.get('subtasks') or []
        child=[{'id':x.get('subtask_id'),'status':x.get('status'),'attempts':[{'id':a.get('attempt_id'),'status':a.get('status')} for a in x.get('attempts',[])]} for x in subs if isinstance(x,dict)]
        status='failed' if dead else ('completed' if state.get('decomposition_accepted') is True or 'decomposition_validation_completed' in types else 'pending')
        add('decomposition_group','Decomposition','group',1,3,status,f"{len(child)} subtasks",{'blockers':state.get('decomposed_execution_blockers') or []},child)
        edge(prev,'decomposition_group'); prev='decomposition_group'
        if dead:
            add('deadlock','Deadlock','deadlock',2,3,'blocked','Required subtask failed',{'failed_subtasks':state.get('decomposed_execution_failed_subtasks') or [],'blocked_subtasks':state.get('decomposed_execution_blocked_subtasks') or []})
            edge('decomposition_group','deadlock','failed'); prev='deadlock'
    sub_ids=sorted({_subtask_id(e) for e in events if _subtask_id(e)})
    if sub_ids:
        add('subtasks_group','Subtasks','group',2,4,'completed',f'{len(sub_ids)} subtasks',{},[{'id':sid,'attempts':[]} for sid in sub_ids])
        for i,sid in enumerate(sub_ids[:3],1):
            add(sid,humanize_id(sid),'subtask',2,4+i,'completed','summarized in subtasks group')
            edge('subtasks_group',sid)
    cand_ids=sorted({_attempt_id(e) for e in events if _attempt_id(e) and ('candidate' in (e.get('type') or '') or e.get('type')=='selection_completed')})
    candidates=[c for c in state.get('candidates',[]) if isinstance(c,dict)]
    if candidates:
        cand_ids=sorted(set(cand_ids)|{c.get('attempt_id') for c in candidates if c.get('attempt_id')})
    group_id='fallback_group' if fallback else 'candidate_group'
    sel=(state.get('selection') or {}).get('selected_attempt_id')
    cand_row = 3 if sub_ids else (1 if not dead else 2)
    cand_col = 4 if not sub_ids else 4
    add(group_id,'Fallback candidates' if fallback else 'Candidates','group',cand_row,cand_col,'completed' if cand_ids else 'pending',f"{len(cand_ids)} candidates",{},[{'id':cid,'status':next((c.get('status') for c in candidates if c.get('attempt_id')==cid),None),'validations':sum(1 for e in events if _attempt_id(e)==cid and (e.get('type') or '').startswith('validation_')),'reviews':sum(1 for e in events if _attempt_id(e)==cid and e.get('type')=='candidate_attempt_reviewed')} for cid in cand_ids])
    edge(prev,group_id)
    for i,cid in enumerate(cand_ids[:3],1):
        add(cid,humanize_id(cid),'candidate',cand_row,cand_col+i,'selected' if cid==sel else 'completed',('Selected winner' if cid==sel else 'Candidate lane'))
        edge(group_id,cid)
    final_row = 4 if sub_ids else 3
    add('validation_group','Validation','group',final_row,5,'completed' if 'validation_completed' in types else ('failed' if 'validation_failed' in types else 'missing'),('Warning: validation not run' if counts('validation_')==0 else f"{counts('validation_')} validation events"))
    add('review_group','Review','group',final_row,6,'completed' if 'candidate_attempt_reviewed' in types or 'subtask_attempt_reviewed' in types else 'pending',f"{counts('review_')} retries")
    add('selection_group','Selection','group',final_row,7,'selected' if sel or 'selection_completed' in types else 'pending',humanize_id(sel or 'winner'),{'selected_attempt_id':sel})
    final_status='failed' if state.get('status')=='failed' else ('completed' if 'run_finalized' in types or state.get('status') in {'completed','failed'} else 'pending')
    add('finalization_group','Finalization','group',final_row,8,final_status,state.get('status') or '',{'final_decision':state.get('final_decision')})
    for a,b in [(group_id,'validation_group'),('validation_group','review_group'),('review_group','selection_group'),('selection_group','finalization_group')]: edge(a,b)
    return {'nodes':nodes,'edges':edges}

def build_viewer_snapshot(run_dir: Path) -> dict[str, Any]:
    run_dir=Path(run_dir); state=_read_json(run_dir/'state.json', {}) or {}; digest=_read_json(run_dir/'event_digest.json', {}) or {}; events=_read_jsonl(run_dir/'runtime_events.jsonl'); usage_rows=_read_jsonl(run_dir/'usage.jsonl')
    usage=_usage(run_dir,state,digest); rid=state.get('run_id') or digest.get('run_id') or run_dir.name; started=state.get('started_at') or (events[0].get('timestamp') if events else None); finalized=state.get('completed_at') or (events[-1].get('timestamp') if events and events[-1].get('type')=='run_finalized' else None); pct,label=_progress(state,events)
    status=state.get('status') or digest.get('status') or ('running' if events else 'unknown')
    decision=derive_decision_summary(state,digest)
    evidence=candidate_evidence(state)
    return _redact({'run':{'run_id':rid,'run_id_short':rid[:18]+('…' if len(rid)>18 else ''),'task':state.get('task') or state.get('objective') or digest.get('task') or '', 'status':status, 'mode':state.get('mode') or digest.get('mode') or 'performance','runner':state.get('runner') or digest.get('runner') or 'villani-code','model':_model(state, usage_rows, events), 'backend_name': _backend_name_from_state_events(state, events), 'backend_url': _backend_url_from_state_events(state, events), 'started_at':started, 'completed_at':finalized, 'duration_seconds':_duration(started, finalized),'progress_percent':pct,'progress_label':label,'result':state.get('final_decision') or digest.get('final_decision'),'run_dir':str(run_dir),'run_dir_short':'…/'+run_dir.name}, 'usage':usage, 'decision':decision, 'candidate_evidence':evidence, 'timeline':_timeline(events), 'graph':build_viewer_graph_layout(state,events), 'warnings':state.get('warnings') or digest.get('warnings') or [], 'errors':state.get('errors') or digest.get('errors') or [], 'artifacts':{'state':'state.json','events':'runtime_events.jsonl','graph':'orchestration_graph.json','usage':'usage.json'}})
