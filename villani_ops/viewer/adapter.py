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
    state=snapshot_or_state or {}; types=_event_types(events)
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
        add(cid,humanize_id(cid),'candidate',cand_row,cand_col+i,'selected' if cid==sel else 'completed','summarized in candidates group')
        edge(group_id,cid)
    final_row = 4 if sub_ids else 3
    add('validation_group','Validation','group',final_row,5,'completed' if 'validation_completed' in types else ('failed' if 'validation_failed' in types else 'pending'),f"{counts('validation_')} validation events")
    add('review_group','Review','group',final_row,6,'completed' if 'candidate_attempt_reviewed' in types or 'subtask_attempt_reviewed' in types else 'pending',f"{counts('review_')} retries")
    add('selection_group','Selection','group',final_row,7,'selected' if sel or 'selection_completed' in types else 'pending',humanize_id(sel or 'winner'),{'selected_attempt_id':sel})
    final_status='completed' if 'run_finalized' in types or state.get('status') in {'completed','failed'} else 'pending'
    add('finalization_group','Finalization','group',final_row,8,final_status,state.get('status') or '',{'final_decision':state.get('final_decision')})
    for a,b in [(group_id,'validation_group'),('validation_group','review_group'),('review_group','selection_group'),('selection_group','finalization_group')]: edge(a,b)
    return {'nodes':nodes,'edges':edges}

def _short_text(value: Any, limit: int=600) -> str:
    if value is None: return ''
    if not isinstance(value, str): value=json.dumps(value, ensure_ascii=False, default=str)
    return value[:limit] + ('…' if len(value)>limit else '')

def _artifact(run_dir: Path, artifact_id: str, label: str, path: str|None) -> dict[str, Any]:
    if not path: return {'id':artifact_id,'label':label,'path':None,'available':False,'status':'Unavailable'}
    p=Path(path)
    full=p if p.is_absolute() else run_dir/p
    return {'id':artifact_id,'label':label,'path':str(path),'available':full.exists(),'status':'Available' if full.exists() else 'Unavailable'}

def _find_candidate_file(run_dir: Path, cid: str, names: list[str]) -> str|None:
    candidates=[]
    for name in names:
        candidates += [run_dir/name, run_dir/cid/name, run_dir/'candidates'/cid/name, run_dir/'artifacts'/cid/name]
        candidates += list(run_dir.glob(f'**/{cid}*{name}'))[:5]
    for p in candidates:
        try:
            if p.exists() and p.is_file(): return str(p.relative_to(run_dir))
        except Exception: pass
    return None

def _read_excerpt(run_dir: Path, rel: str|None, limit: int=12000) -> str:
    if not rel: return ''
    p=Path(rel); fp=p if p.is_absolute() else run_dir/p
    try:
        if fp.exists() and fp.is_file():
            text=fp.read_text(encoding='utf-8', errors='replace')
            return text[:limit] + ('\n…' if len(text)>limit else '')
    except Exception: pass
    return ''

def _candidate_id_from(value: Any) -> str|None:
    if not value: return None
    return str(value)

def build_candidate_debug_summary(run_dir: Path, candidate_id: str) -> dict[str, Any]:
    run_dir=Path(run_dir); state=_read_json(run_dir/'state.json', {}) or {}; events=_read_jsonl(run_dir/'runtime_events.jsonl')
    cands=[c for c in state.get('candidates',[]) if isinstance(c,dict)]
    cand=next((c for c in cands if c.get('attempt_id')==candidate_id or c.get('candidate_id')==candidate_id or c.get('id')==candidate_id), {})
    related=[e for e in events if _attempt_id(e)==candidate_id][-100:]
    evidence_path=cand.get('evidence_path') or _find_candidate_file(run_dir,candidate_id,['evidence.json','candidate_evidence.json'])
    evidence=_read_json(run_dir/evidence_path, {}) if evidence_path and not Path(evidence_path).is_absolute() else _read_json(Path(evidence_path), {}) if evidence_path else {}
    telemetry=evidence.get('telemetry') if isinstance(evidence,dict) else {}
    commands=(cand.get('commands_executed') or evidence.get('commands_executed') or evidence.get('commands') or []) if isinstance(evidence,dict) else cand.get('commands_executed') or []
    failed=(cand.get('commands_failed') or evidence.get('commands_failed') or []) if isinstance(evidence,dict) else []
    files_read=(telemetry or {}).get('file_reads') or evidence.get('files_read',[]) if isinstance(evidence,dict) else []
    files_written=(telemetry or {}).get('file_writes') or evidence.get('files_written',[]) if isinstance(evidence,dict) else []
    arts=[]
    for key in ('patch_path','evidence_path','runner_summary_path'):
        val=cand.get(key) or (evidence_path if key=='evidence_path' else None)
        arts.append(_artifact(run_dir,key,key.replace('_',' '),val))
    return _redact({'candidate_id':candidate_id,'status':cand.get('status') or (related[-1].get('type') if related else 'unknown'),'latest_events':related,'commands':commands[:100],'commands_failed':failed[:100],'files_read':files_read[:100] if isinstance(files_read,list) else files_read,'files_written':files_written[:100] if isinstance(files_written,list) else files_written,'tool_calls':(telemetry or {}).get('tool_calls',[])[:100] if isinstance((telemetry or {}).get('tool_calls',[]),list) else [],'artifacts':arts,'updated_at':datetime.now(timezone.utc).isoformat(),'message':None if arts else 'debug artifacts unavailable'})

def _detail(id: str, type_: str, title: str, status=None, timestamp=None, summary=None, human=None, related=None, evidence=None, artifacts=None, logs=None, diff=None, raw=None):
    return {'id':id,'type':type_,'title':title,'status':status,'timestamp':timestamp,'summary':summary or title,'human':{'what_happened':'','why_it_matters':'','next_action':''}|(human or {}),'related_ids':{'candidate_id':None,'attempt_id':None,'review_id':None,'comparison_id':None,'subtask_id':None}|(related or {}),'evidence':evidence or {},'artifacts':artifacts or [],'logs':logs or [],'diff':diff,'raw':_redact(raw or {})}

def _build_details(run_dir: Path, state: dict[str,Any], events: list[dict[str,Any]], graph: dict[str,Any]) -> dict[str,Any]:
    details={'events':{},'nodes':{},'candidates':{},'reviews':{},'comparisons':{},'artifacts':{}}
    selection=state.get('selection') or {}; selected=selection.get('selected_attempt_id')
    cids=sorted({c.get('attempt_id') or c.get('candidate_id') or c.get('id') for c in state.get('candidates',[]) if isinstance(c,dict)} | {_attempt_id(e) for e in events if _attempt_id(e)})
    for cid in [c for c in cids if c]:
        debug=build_candidate_debug_summary(run_dir,cid); evs=[e for e in events if _attempt_id(e)==cid]
        evidence_path=next((a.get('path') for a in debug['artifacts'] if a['id']=='evidence_path'), None)
        evidence=_read_json(run_dir/evidence_path,{}) if evidence_path and not Path(evidence_path).is_absolute() else {}
        patch_path=_find_candidate_file(run_dir,cid,['patch.diff','.diff'])
        runner_path=_find_candidate_file(run_dir,cid,['runner_summary.json'])
        arts=debug['artifacts']+[_artifact(run_dir,'patch.diff','patch.diff',patch_path),_artifact(run_dir,'runner_summary.json','runner_summary.json',runner_path)]
        status='selected' if cid==selected else (debug.get('status') or 'unknown')
        cd={'id':cid,'type':'candidate','status':status,'summary':f'{humanize_id(cid)} {status}','worktree':next((c.get('worktree') for c in state.get('candidates',[]) if isinstance(c,dict) and (c.get('attempt_id')==cid or c.get('id')==cid)), None),'changed_files':evidence.get('changed_files',[]) if isinstance(evidence,dict) else [],'patch_path':patch_path,'evidence_path':evidence_path,'runner_summary_path':runner_path,'debug_artifact_paths':[],'trace_artifact_paths':[],'commands_executed':debug.get('commands',[]),'commands_failed':debug.get('commands_failed',[]),'latest_debug_events':debug.get('latest_events',[]),'implementation_signature':evidence.get('implementation_signature',{}) if isinstance(evidence,dict) else {},'telemetry':evidence.get('telemetry',{}) if isinstance(evidence,dict) else {},'review':evidence.get('review',{}) if isinstance(evidence,dict) else {},'selection':selection if cid==selected else {},'artifacts':arts,'diff':{'path':patch_path,'excerpt':_read_excerpt(run_dir,patch_path)} if patch_path else None,'raw':{'state':state,'events':evs,'evidence':evidence}}
        details['candidates'][cid]=cd
    for evd in _timeline(events):
        cid=_candidate_id_from(evd.get('attempt_id')); typ=evd.get('type')
        human={'what_happened':evd.get('title'),'why_it_matters':'This is an orchestrator step in the current run.','next_action':'Inspect related evidence or wait for the next runtime event.'}
        if typ=='plan_submitted': human.update({'why_it_matters':'The selected strategy determines which orchestrator stages run next.','next_action':'Launch planned candidates or subtasks.'})
        if cid: human.update({'what_happened':f'{humanize_id(cid)} {evd.get("title","event").lower()}','why_it_matters':'Candidate progress and artifacts determine whether it can win selection.'})
        details['events'][evd['id']]=_detail(evd['id'],'event',evd['title'],evd.get('status'),evd.get('timestamp'),evd.get('subtitle'),human,{'candidate_id':cid,'attempt_id':cid,'subtask_id':evd.get('subtask_id')},details['candidates'].get(cid,{}) if cid else {},details['candidates'].get(cid,{}).get('artifacts',[]) if cid else [],details['candidates'].get(cid,{}).get('latest_debug_events',[]) if cid else [],None,next((e for e in events if (e.get('event_id') or '')==evd['id']), evd))
    for n in graph.get('nodes',[]):
        cid=n['id'] if n.get('type')=='candidate' else None
        evidence=details['candidates'].get(cid,{}) if cid else n.get('details',{})
        details['nodes'][n['id']]=_detail(n['id'],'node',n.get('label') or n['id'],n.get('status'),None,n.get('summary') or n.get('subtitle'),{'what_happened':n.get('summary') or n.get('subtitle') or n.get('label'),'why_it_matters':'Graph nodes show the major stage currently being executed or audited.','next_action':'Select related timeline events or inspect evidence.'},{'candidate_id':cid,'attempt_id':cid},evidence,evidence.get('artifacts',[]) if isinstance(evidence,dict) else [],evidence.get('latest_debug_events',[]) if isinstance(evidence,dict) else [],evidence.get('diff') if isinstance(evidence,dict) else None,n)
    # best-effort review/comparison/selection details from common artifacts
    for path in run_dir.glob('**/*review*.json'):
        data=_read_json(path,{}) or {}; cid=data.get('candidate_id') or data.get('attempt_id') or path.stem
        details['reviews'][str(cid)]={'id':str(cid),'type':'review','recommendation':data.get('recommendation'),'correctness_score':data.get('correctness_score'),'hidden_test_risk':data.get('hidden_test_risk'),'confidence':data.get('confidence'),'evidence_gaps':data.get('evidence_gaps',[]),'risks':data.get('risks',[]),'raw':_redact(data)}
    for path in list(run_dir.glob('**/*comparison*.json'))+list(run_dir.glob('**/*pairwise*.json')):
        data=_read_json(path,{}) or {}; cid=data.get('comparison_id') or path.stem
        details['comparisons'][str(cid)]={'id':str(cid),'type':'comparison','candidate_a':data.get('candidate_a'),'candidate_b':data.get('candidate_b'),'winner':data.get('winner'),'material_differences':data.get('material_differences',[]),'comparison_quality':data.get('comparison_quality') or data.get('quality'),'confidence':data.get('confidence'),'rationale':data.get('rationale') or data.get('reason'),'raw':_redact(data)}
    details['selection']=selection
    return details


def build_viewer_snapshot(run_dir: Path) -> dict[str, Any]:
    run_dir=Path(run_dir); state=_read_json(run_dir/'state.json', {}) or {}; digest=_read_json(run_dir/'event_digest.json', {}) or {}; events=_read_jsonl(run_dir/'runtime_events.jsonl'); usage_rows=_read_jsonl(run_dir/'usage.jsonl')
    usage=_usage(run_dir,state,digest); rid=state.get('run_id') or digest.get('run_id') or run_dir.name; started=state.get('started_at') or (events[0].get('timestamp') if events else None); finalized=state.get('completed_at') or (events[-1].get('timestamp') if events and events[-1].get('type')=='run_finalized' else None); pct,label=_progress(state,events)
    status=state.get('status') or digest.get('status') or ('running' if events else 'unknown')
    timeline=_timeline(events); graph=build_viewer_graph_layout(state,events); details=_build_details(run_dir,state,events,graph)
    return _redact({'run':{'run_id':rid,'run_id_short':rid[:18]+('…' if len(rid)>18 else ''),'task':state.get('task') or state.get('objective') or digest.get('task') or '', 'status':status, 'mode':state.get('mode') or digest.get('mode') or 'performance','runner':state.get('runner') or digest.get('runner') or 'villani-code','model':_model(state, usage_rows, events), 'started_at':started, 'completed_at':finalized, 'duration_seconds':_duration(started, finalized),'progress_percent':pct,'progress_label':label,'result':state.get('final_decision') or digest.get('final_decision'),'run_dir':str(run_dir),'run_dir_short':'…/'+run_dir.name}, 'usage':usage, 'timeline':timeline, 'graph':graph, 'details':details, 'warnings':state.get('warnings') or digest.get('warnings') or [], 'errors':state.get('errors') or digest.get('errors') or [], 'artifacts':{'state':'state.json','events':'runtime_events.jsonl','graph':'orchestration_graph.json','usage':'usage.json'}})
