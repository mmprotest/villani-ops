from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json, re

SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|password|authorization)")
SECRET_VALUE_RE = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._-]+|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})")
EVENTS = {
    'run_started':'Run started','investigation_submitted':'Investigation submitted','classification_submitted':'Classification submitted','plan_submitted':'Plan submitted','decomposition_submitted':'Decomposition submitted','decomposition_validation_completed':'Decomposition validation completed','execution_path_selected':'Execution path selected','candidate_attempt_started':'Candidate attempt started','candidate_attempt_completed':'Candidate attempt completed','candidate_attempt_failed':'Candidate attempt failed','subtask_attempt_started':'Subtask attempt started','subtask_attempt_completed':'Subtask attempt completed','subtask_attempt_failed':'Subtask attempt failed','subtask_attempt_reviewed':'Subtask attempt reviewed','subtask_accepted':'Subtask accepted','subtask_failed':'Subtask failed','validation_started':'Validation started','validation_completed':'Validation completed','validation_failed':'Validation failed','candidate_attempt_reviewed':'Candidate attempt reviewed','selection_completed':'Selection completed','run_finalized':'Final decision','decomposition_deadlock_detected':'Decomposition deadlock detected','candidate_fallback_started':'Candidate fallback started','integration_started':'Integration started','integration_completed':'Integration completed','integration_failed':'Integration failed','recovery_injected':'Recovery injected','recovery_deterministic_action_executed':'Recovery deterministic action executed'
}
STATUS_BY_EVENT = {
    'run_started':'running','candidate_attempt_started':'running','subtask_attempt_started':'running','validation_started':'running','integration_started':'running','candidate_fallback_started':'running','candidate_attempt_completed':'completed','subtask_attempt_completed':'completed','validation_completed':'completed','integration_completed':'completed','candidate_attempt_failed':'failed','subtask_attempt_failed':'failed','subtask_failed':'failed','validation_failed':'failed','integration_failed':'failed','subtask_accepted':'accepted','candidate_attempt_reviewed':'completed','subtask_attempt_reviewed':'completed','selection_completed':'selected','run_finalized':'completed','decomposition_deadlock_detected':'blocked'
}

def _read_json(path: Path, default: Any=None) -> Any:
    try:
        if not path.exists() or not path.read_text(encoding='utf-8').strip(): return default
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out=[]
    if not path.exists(): return out
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            if line.strip(): out.append(json.loads(line))
        except Exception:
            continue
    return out

def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ('***REDACTED***' if SECRET_KEY_RE.search(str(k)) else _redact(v)) for k,v in value.items()}
    if isinstance(value, list): return [_redact(v) for v in value]
    if isinstance(value, str):
        if SECRET_VALUE_RE.search(value): return SECRET_VALUE_RE.sub('***REDACTED***', value)
        return value[:5000]
    return value

def _duration(start: str|None, end: str|None=None) -> float|None:
    if not start: return None
    try:
        s=datetime.fromisoformat(start.replace('Z','+00:00'))
        e=datetime.fromisoformat(end.replace('Z','+00:00')) if end else datetime.now(timezone.utc)
        return max(0.0, round((e-s).total_seconds(), 2))
    except Exception: return None

def _payload(ev): return ev.get('payload') or {}
def _attempt_id(ev): return ev.get('attempt_id') or _payload(ev).get('attempt_id') or _payload(ev).get('candidate_id') or _payload(ev).get('selected_attempt_id')
def _subtask_id(ev): return ev.get('subtask_id') or _payload(ev).get('subtask_id')

def _subtitle(ev: dict[str,Any]) -> str:
    p=_payload(ev); t=ev.get('type')
    bits=[]
    if t=='classification_submitted': bits=[p.get('difficulty'), p.get('category')]
    elif t in {'plan_submitted','execution_path_selected'}: bits=[p.get('execution_path') or p.get('path') or p.get('plan_type')]
    elif t in {'candidate_attempt_started','candidate_attempt_completed','candidate_attempt_failed','candidate_attempt_reviewed','validation_started','validation_completed','validation_failed','selection_completed'}: bits=[_attempt_id(ev)]
    elif t.startswith('subtask_') or t=='subtask_attempt_reviewed': bits=[_subtask_id(ev), _attempt_id(ev)]
    elif t.startswith('integration_'): bits=[p.get('status')]
    if p.get('exit_code') is not None: bits.append(f"exit_code={p.get('exit_code')}")
    if p.get('decision'): bits.append(str(p.get('decision')))
    return ' / '.join(str(b) for b in bits if b)

def _timeline(events):
    items=[]
    for i, ev in enumerate(events):
        typ=ev.get('type')
        if typ not in EVENTS: continue
        p=_redact(_payload(ev))
        dur=p.get('duration_seconds') or p.get('duration')
        items.append({'id':ev.get('event_id') or f'event_{i}', 'timestamp':ev.get('timestamp'), 'type':typ, 'title':EVENTS[typ], 'subtitle':_subtitle(ev), 'status':STATUS_BY_EVENT.get(typ,'completed'), 'duration_seconds':dur, 'attempt_id':_attempt_id(ev), 'subtask_id':_subtask_id(ev)})
    return items

def _graph_from_events(events):
    nodes={}; edges=[]
    def node(id,label,type,status='pending',subtitle=''):
        if id in nodes:
            nodes[id].update({k:v for k,v in {'status':status,'subtitle':subtitle}.items() if v})
        else: nodes[id]={'id':id,'label':label,'type':type,'status':status,'subtitle':subtitle,'metrics':{}}
    def edge(a,b,status='active'):
        eid=f'edge_{a}_{b}'
        if not any(e['id']==eid for e in edges): edges.append({'id':eid,'source':a,'target':b,'status':status})
    for id,label,type in [('investigate','Investigate','investigate'),('classify','Classify','classify'),('plan','Plan','plan'),('select_path','Select Path','select_path'),('validate','Validate','validate'),('review','Review','review'),('winner','Winner','winner'),('finalize','Finalize','finalize')]: node(id,label,type)
    for a,b in [('investigate','classify'),('classify','plan'),('plan','select_path'),('select_path','validate'),('validate','review'),('review','winner'),('winner','finalize')]: edge(a,b)
    last_mid=None
    for ev in events:
        typ=ev.get('type'); aid=_attempt_id(ev); sid=_subtask_id(ev); st=STATUS_BY_EVENT.get(typ,'completed')
        if aid and 'candidate_attempt' in typ:
            node(aid, aid.replace('_',' ').title(), 'candidate', st, _subtitle(ev)); edge('select_path', aid); edge(aid,'validate'); last_mid=aid
        if sid:
            node(sid, sid.replace('_',' ').title(), 'subtask', st, _subtitle(ev)); edge(last_mid or 'select_path', sid); edge(sid,'validate')
        if typ and typ.startswith('validation_'): node('validate','Validate','validate',st,_subtitle(ev))
        if typ and typ.endswith('_reviewed'): node('review','Review','review',st,_subtitle(ev))
        if typ=='selection_completed': node('winner','Winner','winner','selected',_subtitle(ev))
        if typ=='run_finalized': node('finalize','Finalize','finalize',st,_subtitle(ev))
    return {'nodes':list(nodes.values()), 'edges':edges}

def _usage(run_dir, state):
    data=_read_json(run_dir/'usage.json', {}) or _read_json(run_dir/'cost_summary.json', {}) or (state.get('usage_summary') if isinstance(state,dict) else {}) or {}
    return {'input_tokens':data.get('input_tokens',0) or 0,'output_tokens':data.get('output_tokens',0) or 0,'total_tokens':data.get('total_tokens',0) or 0,'total_cost':data.get('total_cost',0.0) or 0.0,'unavailable_calls_count':data.get('unavailable_calls_count',0) or 0}

def build_viewer_snapshot(run_dir: Path) -> dict[str, Any]:
    run_dir=Path(run_dir)
    state=_read_json(run_dir/'state.json', {}) or {}
    digest=_read_json(run_dir/'event_digest.json', {}) or {}
    events=_read_jsonl(run_dir/'runtime_events.jsonl')
    graph_raw=_read_json(run_dir/'orchestration_graph.json', None)
    usage=_usage(run_dir,state)
    rid=state.get('run_id') or digest.get('run_id') or run_dir.name
    started=state.get('started_at') or (events[0].get('timestamp') if events else None)
    finalized=state.get('completed_at') or (events[-1].get('timestamp') if events and events[-1].get('type')=='run_finalized' else None)
    graph=graph_raw if isinstance(graph_raw,dict) and 'nodes' in graph_raw and 'edges' in graph_raw else _graph_from_events(events)
    return _redact({'run':{'run_id':rid,'task':state.get('task') or state.get('objective') or digest.get('task') or '', 'status':state.get('status') or digest.get('status') or ('running' if events else 'unknown'), 'mode':state.get('mode') or digest.get('mode') or 'performance','runner':state.get('runner') or 'villani-code','model':state.get('model') or state.get('backend_model') or '', 'started_at':started, 'duration_seconds':_duration(started, finalized),'result':state.get('final_decision') or digest.get('final_decision'),'run_dir':str(run_dir)}, 'usage':usage, 'timeline':_timeline(events), 'graph':graph, 'artifacts':{'state':'state.json','events':'runtime_events.jsonl','graph':'orchestration_graph.json','usage':'usage.json'}})
