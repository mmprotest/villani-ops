from __future__ import annotations
import hashlib,re,json
from .types import *
VALIDATION=['test','pytest','npm test','pnpm test','yarn test','vitest','jest','go test','cargo test','mvn test','gradle test','tsc','typecheck','build','lint','curl','urllib','requests','wget','git clone','git push','nginx -t','openssl x509','grep','pass','verification','cat ']
FAIL_RE=re.compile(r'\b(FAIL|FAILED|error|exception|traceback|not found|refused|timeout|permission denied|connection refused|syntax error|missing file)\b',re.I)
PASS_RE=re.compile(r'\b(PASS|test[s]? passed|all tests passed|successful|succeeded)\b',re.I)
PATH_RE=re.compile(r'(?:(?:/[\w.-]+)+|[\w.-]+\.(?:py|json|txt|csv|ics|html|js|ts|md|yml|yaml|sh))')
URL_RE=re.compile(r'https?://[^\s"\'<>]+')
OUTPUT_EXT={'.json','.txt','.csv','.ics','.html','.xml','.md'}
def is_validation_command(cmd:str|None):
    c=(cmd or '').lower(); return any(v in c for v in VALIDATION)
def extract_requirements(objective:str|None):
    text=(objective or '').strip(); req=[]
    if text:
        parts=[]
        for line in text.splitlines():
            s=re.sub(r'^\s*(?:[-*]|\d+[.)])\s*','',line).strip()
            if s: parts.append(s)
        if len(parts)<=1:
            parts=[p.strip() for p in re.split(r';|\n|(?<=\.)\s+',text) if len(p.strip())>12]
        for p in parts:
            if len(p)>8:
                rid=hashlib.sha1(p.lower().encode()).hexdigest()[:10]
                req.append(RequirementCheck(rid,p))
    if len(req)<3:
        return [RequirementCheck('task_objective_addressed','Task objective addressed'),RequirementCheck('final_validation_present','Final validation present'),RequirementCheck('no_blocking_failures','No blocking failures remain')]
    return req
def _as_text(x):
    if x is None: return ''
    if isinstance(x,str): return x
    try: return json.dumps(x,default=str)
    except Exception: return str(x)
def _path_name(p): return (p or '').strip().strip('"\'`')
def _basename(p): return _path_name(p).rstrip('/').split('/')[-1]
def _tool_name(t): return (t.toolName or (t.raw or {}).get('tool_name') or (t.raw or {}).get('toolName') or (t.raw or {}).get('name') or '').lower()
def _tool_args(t):
    a=t.args
    if isinstance(a,str):
        try: return json.loads(a)
        except Exception: return {}
    return a if isinstance(a,dict) else {}
def _tool_blob(t): return ' '.join(_as_text(x) for x in [t.args,t.resultSummary,t.raw])
def _tool_success(t):
    st=(t.status or '').lower()
    return not t.error and st not in {'failed','error','failure','denied','refused'}
def _extract_paths_from_obj(obj):
    paths=[]
    if isinstance(obj,dict):
        for k in ['path','file_path','filePath','filename','file','target']:
            if obj.get(k): paths.append(_path_name(str(obj.get(k))))
    for m in PATH_RE.findall(_as_text(obj)):
        paths.append(_path_name(m))
    out=[]
    for p in paths:
        if p and p not in out: out.append(p)
    return out
def _preview_content(t,n=220):
    a=_tool_args(t)
    for k in ['content','text','data']:
        if k in a and a[k] is not None: return _as_text(a[k])[:n]
    return _as_text(t.resultSummary or t.args)[:n]
def extract_deliverables(objective:str|None, run:DebugRun|None=None):
    spec=DeliverableSpec()
    text=objective or ''
    for u in URL_RE.findall(text):
        spec.required_endpoints.append(u.rstrip('.,)'))
    files=[_path_name(p) for p in PATH_RE.findall(text)]
    for p in files:
        ext='.'+_basename(p).split('.')[-1].lower() if '.' in _basename(p) else ''
        if ext in OUTPUT_EXT: spec.required_output_files.append(p)
        else: spec.required_edited_files.append(p)
        spec.required_files.append(p)
    for fn in re.findall(r'\bdef\s+([A-Za-z_]\w*)|\bfunction\s+([A-Za-z_]\w*)|`([A-Za-z_]\w*)\(`', text):
        name=next((x for x in fn if x),None)
        if name and name not in spec.required_functions: spec.required_functions.append(name)
    if run:
        for obj in [run.finalSummary, run.summary]:
            if isinstance(obj,dict):
                for p in obj.get('changed_files') or obj.get('changedFiles') or []:
                    if p not in spec.required_files: spec.required_files.append(p)
        for t in run.toolCalls:
            for p in _extract_paths_from_obj(_tool_args(t)) + _extract_paths_from_obj(t.resultSummary):
                if p not in spec.required_files and any(_basename(p)==_basename(x) for x in spec.required_files):
                    spec.required_files.append(p)
    for attr in ['required_files','required_output_files','required_edited_files','required_endpoints']:
        vals=[]; [vals.append(x) for x in getattr(spec,attr) if x and x not in vals]; setattr(spec,attr,vals)
    return spec
def _looks_deliverable(path,spec):
    b=_basename(path).lower()
    if any(b==_basename(x).lower() or path==x for x in spec.required_files+spec.required_output_files+spec.required_edited_files): return True
    return bool(re.search(r'(output|answer|solution|result|meeting_scheduled)\.(json|txt|csv|ics|html)$',b))
def _ics_ok(preview):
    return sum(1 for m in ['BEGIN:VCALENDAR','BEGIN:VEVENT','DTSTART','DTEND','ATTENDEE','SUMMARY','END:VEVENT','END:VCALENDAR'] if m.lower() in (preview or '').lower())>=3
def tool_call_evidence(run:DebugRun, spec:DeliverableSpec):
    mutations=[]; inspections=[]; deliverables=[]; failures=[]
    for t in run.toolCalls:
        name=_tool_name(t); ok=_tool_success(t); paths=_extract_paths_from_obj(_tool_args(t)) or _extract_paths_from_obj(t.resultSummary) or _extract_paths_from_obj(t.raw)
        order=(t.turnIndex if t.turnIndex is not None else t.index); tid=t.toolCallId
        if not ok:
            failures.append(EvidenceItem('failure_signal','tool_calls','medium',f'tool[{tid}] {t.toolName} failed: {t.error or t.resultSummary}',tid,t.turnIndex,t.startedAt,order,toolCallId=tid))
            continue
        if any(x in name for x in ['write','edit','patch','replace','create']):
            for p in paths or ['unknown file']:
                prev=_preview_content(t)
                mutations.append(EvidenceItem('file_write' if 'write' in name or 'create' in name else 'file_edit','tool_calls','high',f'{(t.toolName or name)} tool created or updated {p}'+(f' :: {prev[:120]}' if prev else ''),tid,t.turnIndex,t.startedAt,order,path=p,toolCallId=tid))
                if _looks_deliverable(p,spec):
                    strength='medium'
                    if p.lower().endswith('.ics') and _ics_ok(prev): strength='strong'
                    deliverables.append(EvidenceItem('deliverable_write','tool_calls','high',f'Successful tool call created or updated required deliverable {p}',tid,t.turnIndex,t.startedAt,order,path=p,toolCallId=tid,deliverableLinked=True,deliverableLinks=[p],validationStrength=strength))
        if 'read' in name:
            for p in paths or ['unknown file']:
                inspections.append(EvidenceItem('file_read','tool_calls','medium',f'Read tool inspected {p}',tid,t.turnIndex,t.startedAt,order,path=p,toolCallId=tid,deliverableLinked=_looks_deliverable(p,spec),deliverableLinks=[p] if _looks_deliverable(p,spec) else []))
    return mutations,inspections,deliverables,failures
def extract_evidence(run:DebugRun):
    success=[]; failures=[]; risks=[]; missing=[]; mutations=[]; validations=[]
    spec=extract_deliverables(run.objective,run)
    for m in run.missingArtifacts:
        txt='No validations.jsonl artifact was present.' if m=='validations.jsonl' else f'Missing artifact: {m}'
        missing.append(EvidenceItem('missing','derived','medium',txt))
    for p in run.patches:
        if p.ok: mutations.append(EvidenceItem('mutation','patches','medium',f'Patch applied successfully to {p.filePath}',order=p.index))
    for obj,src in [(run.finalSummary,'final_summary'),(run.summary,'summary')]:
        if isinstance(obj,dict):
            for p in obj.get('changed_files') or obj.get('changedFiles') or []:
                mutations.append(EvidenceItem('file_changed',src,'medium',f'{src}.changed_files reports changed file {p}',path=p))
    for c in run.commands:
        if c.event and not c.command: continue
        blob=((c.stdout or '')+'\n'+(c.stderr or ''))[:2000]; cmd=c.command or ''
        text=f'command[{c.index}] exit={c.exitCode}: {cmd} :: {blob[:300].strip()}'
        if c.exitCode not in (None,0) or FAIL_RE.search(blob): failures.append(EvidenceItem('failure_signal','commands','high' if is_validation_command(cmd) else 'medium',text,c.toolCallId,timestamp=c.ts,order=c.index))
        if (c.exitCode==0 and is_validation_command(cmd)) or PASS_RE.search(blob) or ('git clone' in cmd and c.exitCode==0) or ('git push' in cmd and c.exitCode==0):
            ev=EvidenceItem('validation' if is_validation_command(cmd) else 'success_signal','commands','high' if is_validation_command(cmd) else 'medium',text,c.toolCallId,timestamp=c.ts,order=c.index)
            success.append(ev); validations.append(ev)
    tm,ti,td,tf=tool_call_evidence(run,spec); mutations+=tm; success+=td; failures+=tf
    return success,failures,risks,missing,mutations,validations,ti,td,spec
def classify_recovered(failures, successes):
    active=[]; recovered=[]
    if successes:
        last_success=max(s.order for s in successes)
    else: last_success=-1
    for f in failures:
        ft=f.text.lower(); later=[s for s in successes if s.order>f.order]
        if later and (any(any(v in ss.text.lower() for v in ['pass','git clone','git push','curl','verification','nginx']) for ss in later) or any(k in ft for k in ['not exist','refused','syntax error','already exists','rm -rf'])):
            recovered.append(EvidenceItem('recovered_failure',f.source,f.confidence,'Recovered: '+f.text,f.commandId,f.turnIndex,f.timestamp,f.order))
        else: active.append(f)
    return active,recovered
