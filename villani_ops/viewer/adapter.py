from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json, re

SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|password|authorization)")
SECRET_VALUE_RE = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._-]+|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})")
EVENTS = {'run_started':'Run started','investigation_submitted':'Investigation submitted','classification_submitted':'Classification submitted','plan_submitted':'Plan submitted','decomposition_submitted':'Decomposition submitted','decomposition_validation_completed':'Decomposition validation completed','execution_path_selected':'Execution path selected','candidate_attempt_started':'Candidate attempt started','candidate_attempt_completed':'Candidate attempt completed','candidate_attempt_failed':'Candidate attempt failed','subtask_attempt_started':'Subtask attempt started','subtask_attempt_completed':'Subtask attempt completed','subtask_attempt_failed':'Subtask attempt failed','subtask_attempt_reviewed':'Subtask attempt reviewed','subtask_accepted':'Subtask accepted','subtask_failed':'Subtask failed','validation_started':'Validation started','validation_completed':'Validation completed','validation_failed':'Validation failed','candidate_attempt_reviewed':'Candidate attempt reviewed','selection_completed':'Selection completed','run_finalized':'Final decision','decomposition_deadlock_detected':'Decomposition deadlock detected','candidate_fallback_started':'Candidate fallback started','integration_started':'Integration started','integration_completed':'Integration completed','integration_failed':'Integration failed','recovery_injected':'Recovery injected','recovery_deterministic_action_executed':'Recovery action executed'}
STATUS_BY_EVENT = {'run_started':'running','candidate_attempt_started':'running','subtask_attempt_started':'running','validation_started':'running','integration_started':'running','candidate_fallback_started':'running','candidate_attempt_completed':'completed','subtask_attempt_completed':'completed','validation_completed':'completed','integration_completed':'completed','candidate_attempt_failed':'failed','subtask_attempt_failed':'failed','subtask_failed':'failed','validation_failed':'failed','integration_failed':'failed','subtask_accepted':'accepted','candidate_attempt_reviewed':'completed','subtask_attempt_reviewed':'completed','selection_completed':'selected','run_finalized':'completed','decomposition_deadlock_detected':'blocked'}

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

def _num(d,*keys):
    for k in keys:
        v=d.get(k) if isinstance(d,dict) else None
        if isinstance(v,(int,float)): return v
    return 0

def _meaningful(src: Any) -> bool:
    if not isinstance(src, dict): return False
    for k in ('total_tokens','input_tokens','output_tokens','total_cost','calls_count','unavailable_calls_count'):
        try:
            if float(src.get(k) or 0) > 0: return True
        except (TypeError, ValueError): pass
    return False

def aggregate_usage_jsonl(run_dir: Path) -> dict:
    rows=_read_jsonl(Path(run_dir)/'usage.jsonl')
    out={'input_tokens':0,'output_tokens':0,'total_tokens':0,'input_cost':0.0,'output_cost':0.0,'total_cost':0.0,'calls_count':0,'unavailable_calls_count':0,'by_role':{},'by_backend':{},'by_model':{}}
    def add_bucket(bucket, r):
        bucket['input_tokens']=bucket.get('input_tokens',0)+int(_num(r,'input_tokens','prompt_tokens'))
        bucket['output_tokens']=bucket.get('output_tokens',0)+int(_num(r,'output_tokens','completion_tokens'))
        tt=_num(r,'total_tokens') or int(_num(r,'input_tokens','prompt_tokens'))+int(_num(r,'output_tokens','completion_tokens'))
        bucket['total_tokens']=bucket.get('total_tokens',0)+int(tt)
        bucket['input_cost']=bucket.get('input_cost',0.0)+float(_num(r,'input_cost'))
        bucket['output_cost']=bucket.get('output_cost',0.0)+float(_num(r,'output_cost'))
        bucket['total_cost']=bucket.get('total_cost',0.0)+float(_num(r,'total_cost','cost','usd'))
        bucket['calls_count']=bucket.get('calls_count',0)+1
        if r.get('usage_source')=='unavailable' or r.get('cost_unavailable') or r.get('unavailable'):
            bucket['unavailable_calls_count']=bucket.get('unavailable_calls_count',0)+1
    for r in rows:
        add_bucket(out,r)
        for field, name in (('role','by_role'),('backend_name','by_backend'),('model','by_model')):
            val=r.get(field)
            if val:
                out[name].setdefault(str(val),{})
                add_bucket(out[name][str(val)],r)
    return out

def _usage(run_dir, state, digest):
    cost=_read_json(run_dir/'cost_summary.json', {}) or {}; uj=_read_json(run_dir/'usage.json', {}) or {}; summary=uj.get('summary') if isinstance(uj.get('summary'),dict) else uj
    candidates=[cost, summary, state.get('usage_summary') if isinstance(state,dict) else {}, digest.get('usage') if isinstance(digest,dict) else {}, aggregate_usage_jsonl(run_dir)]
    src=next((c for c in candidates if _meaningful(c)), {})
    inp=_num(src,'input_tokens','prompt_tokens'); out=_num(src,'output_tokens','completion_tokens'); total=_num(src,'total_tokens') or inp+out
    return {'input_tokens':inp,'output_tokens':out,'total_tokens':total,'input_cost':_num(src,'input_cost'),'output_cost':_num(src,'output_cost'),'total_cost':_num(src,'total_cost','cost','usd'),'calls_count':int(_num(src,'calls_count')),'unavailable_calls_count':int(_num(src,'unavailable_calls_count','unavailable_calls')),'by_role':src.get('by_role',{}) if isinstance(src,dict) else {},'by_backend':src.get('by_backend',{}) if isinstance(src,dict) else {},'by_model':src.get('by_model',{}) if isinstance(src,dict) else {}}

def _model(state, usage, events):
    for k in ('selected_model','orchestrator_model','backend_model','model'):
        if state.get(k): return str(state[k])
    b=state.get('backend')
    if isinstance(b,dict):
        for k in ('model','name'):
            if b.get(k): return str(b[k])
    us=state.get('usage_summary')
    if isinstance(us,dict) and isinstance(us.get('by_model'),dict) and us['by_model']: return next(iter(us['by_model']))
    for row in usage if isinstance(usage,list) else []:
        if row.get('model'): return str(row['model'])
    for ev in events:
        p=_payload(ev)
        if p.get('model'): return str(p['model'])
        if p.get('backend_model'): return str(p['backend_model'])
        if p.get('backend'): return str(p['backend'])
    return str(state.get('backend_name') or '')

def build_viewer_graph_layout(snapshot_or_state: dict[str,Any], events: list[dict[str,Any]]) -> dict[str,Any]:
    types=_event_types(events); decomposed=bool(types & {'decomposition_submitted','subtask_attempt_started','integration_started'}); fallback='decomposition_deadlock_detected' in types or 'candidate_fallback_started' in types
    nodes={}; edges=[]
    def node(id,label,type,row,col,status='pending',subtitle=''):
        old=nodes.get(id,{}); nodes[id]={**old,'id':id,'label':label,'type':type,'status':status or old.get('status','pending'),'row':row,'col':col,'subtitle':subtitle or old.get('subtitle',''),'metrics':old.get('metrics',{})}
    def edge(a,b,status='active'):
        if a in nodes and b in nodes and not any(e['source']==a and e['target']==b for e in edges): edges.append({'id':f'edge_{a}_{b}','source':a,'target':b,'status':status})
    base=[('investigate','Investigate','investigate'),('classify','Classify','classify'),('plan','Plan','plan')]
    if decomposed or fallback: base += [('decompose','Decompose','decompose')]
    base += [('select_path','Select Path','select_path')]
    completed={'investigate':'investigation_submitted','classify':'classification_submitted','plan':'plan_submitted','decompose':'decomposition_submitted','select_path':'execution_path_selected'}
    for i,(id,lab,typ) in enumerate(base,1): node(id,lab,typ,1,i,'completed' if completed.get(id) in types else ('running' if id=='investigate' and 'run_started' in types else 'pending'))
    for a,b in zip([x[0] for x in base],[x[0] for x in base][1:]): edge(a,b)
    mids=[]
    ids=sorted({_subtask_id(e) for e in events if _subtask_id(e)} or {_attempt_id(e) for e in events if _attempt_id(e) and 'candidate' in (e.get('type') or '')})
    kind='subtask' if any(_subtask_id(e) for e in events) else 'candidate'
    if kind=='subtask' and len(ids)>3: node('subtasks_group','Subtasks','group',2,1,'running' if 'subtask_attempt_started' in types else 'pending',f'{len(ids)} items'); edge('select_path','subtasks_group')
    for idx,idv in enumerate(ids):
        evs=[e for e in events if _subtask_id(e)==idv or _attempt_id(e)==idv]; last=evs[-1] if evs else {}; st=STATUS_BY_EVENT.get(last.get('type'),'pending')
        col=(idx%4)+ (2 if kind=='subtask' and len(ids)>3 else 1); row=2+(idx//4)
        node(idv,humanize_id(idv),kind,row,col,st,_subtitle(last)); mids.append(idv)
        edge('subtasks_group' if kind=='subtask' and len(ids)>3 else 'select_path', idv)
    final_row=4 if ids and len(ids)>4 else 3
    finals=[('integrate','Integrate','integration_completed'),('validate','Validate','validation_completed'),('review','Review','candidate_attempt_reviewed'),('winner','Winner','selection_completed'),('finalize','Finalize','run_finalized')]
    for i,(id,lab,typ) in enumerate(finals,1): node(id,lab,id,final_row,i,'completed' if typ in types else ('selected' if id=='winner' and typ in types else 'pending'))
    for m in (mids[-1:] or ['select_path']): edge(m,'integrate' if decomposed else 'validate')
    for a,b in [('integrate','validate'),('validate','review'),('review','winner'),('winner','finalize')]: edge(a,b)
    return {'nodes':list(nodes.values()),'edges':edges}

def build_viewer_snapshot(run_dir: Path) -> dict[str, Any]:
    run_dir=Path(run_dir); state=_read_json(run_dir/'state.json', {}) or {}; digest=_read_json(run_dir/'event_digest.json', {}) or {}; events=_read_jsonl(run_dir/'runtime_events.jsonl'); usage_rows=_read_jsonl(run_dir/'usage.jsonl')
    usage=_usage(run_dir,state,digest); rid=state.get('run_id') or digest.get('run_id') or run_dir.name; started=state.get('started_at') or (events[0].get('timestamp') if events else None); finalized=state.get('completed_at') or (events[-1].get('timestamp') if events and events[-1].get('type')=='run_finalized' else None); pct,label=_progress(state,events)
    status=state.get('status') or digest.get('status') or ('running' if events else 'unknown')
    return _redact({'run':{'run_id':rid,'run_id_short':rid[:18]+('…' if len(rid)>18 else ''),'task':state.get('task') or state.get('objective') or digest.get('task') or '', 'status':status, 'mode':state.get('mode') or digest.get('mode') or 'performance','runner':state.get('runner') or digest.get('runner') or 'villani-code','model':_model(state, usage_rows, events), 'started_at':started, 'completed_at':finalized, 'duration_seconds':_duration(started, finalized),'progress_percent':pct,'progress_label':label,'result':state.get('final_decision') or digest.get('final_decision'),'run_dir':str(run_dir),'run_dir_short':'…/'+run_dir.name}, 'usage':usage, 'timeline':_timeline(events), 'graph':build_viewer_graph_layout(state,events), 'artifacts':{'state':'state.json','events':'runtime_events.jsonl','graph':'orchestration_graph.json','usage':'usage.json'}})
