from __future__ import annotations
import hashlib,re,json,os
from .types import *
VALIDATION=['test','pytest','npm test','pnpm test','yarn test','vitest','jest','go test','cargo test','mvn test','gradle test','tsc','typecheck','build','lint','curl','urllib','requests','wget','git clone','git push','nginx -t','openssl x509','grep','pass','verification','cat ','eval.py','rscript']
FAIL_RE=re.compile(r'\b(FAIL|FAILED|error|exception|traceback|not found|refused|timeout|permission denied|connection refused|syntax error|missing file|segmentation fault|core dumped|sigsegv|abort(?:ed)?|fatal error|uncaught exception|unhandled exception|exit code 139|returncode -11|process crashed)\b',re.I)
PASS_RE=re.compile(r'\b(PASS|test[s]? passed|all tests passed|all correctness tests passed|successful|succeeded|checks? passed|assertions? passed|verification complete)\b',re.I)
URL_RE=re.compile(r'https?://[^\s"\'<>]+')
KNOWN_EXT={'.py','.r','.R','.stan','.ics','.tex','.txt','.csv','.json','.html','.js','.ts','.md','.yml','.yaml','.sh','.c','.h','.sql','.db','.sqlite','.xml'}
OUTPUT_EXT={'.json','.txt','.csv','.ics','.html','.xml','.md'}
PATH_CAND_RE=re.compile(r'(?:https?://[^\s"\'<>]+|(?:\.?\.?/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|[A-Za-z0-9_.-]+\.(?:py|R|r|stan|ics|tex|txt|csv|json|html|js|ts|md|yml|yaml|sh|c|h|sql|db|sqlite|xml))')
def is_validation_command(cmd:str|None):
    c=(cmd or '').lower(); return any(v in c for v in VALIDATION)
def extract_requirements(objective:str|None):
    text=(objective or '').strip(); req=[]
    if text:
        parts=[]
        for line in text.splitlines():
            s=re.sub(r'^\s*(?:[-*]|\d+[.)])\s*','',line).strip()
            if s: parts.append(s)
        if len(parts)<=1: parts=[p.strip() for p in re.split(r';|\n|(?<=\.)\s+',text) if len(p.strip())>12]
        for p in parts:
            if len(p)>8: req.append(RequirementCheck(hashlib.sha1(p.lower().encode()).hexdigest()[:10],p))
    if len(req)<3: return [RequirementCheck('task_objective_addressed','Task objective addressed'),RequirementCheck('final_validation_present','Final validation present'),RequirementCheck('no_blocking_failures','No blocking failures remain')]
    return req
def _as_text(x):
    if x is None: return ''
    if isinstance(x,str): return x
    try: return json.dumps(x,default=str)
    except Exception: return str(x)
def _path_name(p): return (p or '').strip().strip('"\'`').rstrip('.,);]')
def _basename(p): return _path_name(p).rstrip('/').split('/')[-1]
def _ext(p):
    b=_basename(p); return os.path.splitext(b)[1]
def _accept_path(p, known=None):
    p=_path_name(p)
    if not p or re.fullmatch(r'/?\d+\)?',p) or re.search(r'\(-?\d+/\d+\)?$',p): return False
    if re.fullmatch(r'/\d+',p): return False
    if URL_RE.fullmatch(p): return True
    if _ext(p) in KNOWN_EXT or _ext(p).lower() in {e.lower() for e in KNOWN_EXT}: return True
    if p.startswith('/app/'): return True
    if p.startswith(('./','../')):
        norm=os.path.normpath(p)
        return not norm.startswith('../') and norm not in {'.','..'}
    comps=[c for c in p.split('/') if c]
    if len(comps)>=2 and any(re.search(r'[A-Za-z]',c) for c in comps): return True
    if known and p in known: return True
    return False
def _tool_name(t): return (t.toolName or (t.raw or {}).get('tool_name') or (t.raw or {}).get('toolName') or (t.raw or {}).get('name') or '').lower()
def _tool_args(t):
    a=t.args
    if isinstance(a,str):
        try: return json.loads(a)
        except Exception: return {}
    return a if isinstance(a,dict) else {}
def _tool_success(t):
    st=(t.status or '').lower(); return not t.error and st not in {'failed','error','failure','denied','refused'}
def _extract_paths_from_obj(obj, known=None, scan_text=True):
    paths=[]
    if isinstance(obj,dict):
        for k in ['path','file_path','filePath','filename','file','target','target_path']:
            if obj.get(k): paths.append(_path_name(str(obj.get(k))))
    if scan_text:
        for m in PATH_CAND_RE.findall(_as_text(obj)): paths.append(_path_name(m))
    out=[]
    for p in paths:
        if _accept_path(p, known) and p not in out: out.append(p)
    return out
def _preview_content(t,n=500):
    a=_tool_args(t)
    for k in ['content','text','data']:
        if k in a and a[k] is not None: return _as_text(a[k])[:n]
    return _as_text(t.resultSummary or t.args)[:n]
def extract_deliverables(objective, run=None):
    spec=DeliverableSpec(); text=objective or ''
    for u in URL_RE.findall(text): spec.required_endpoints.append(u.rstrip('.,)'))
    files=_extract_paths_from_obj(text)
    for p in files:
        ext=_ext(p); low=p.lower()
        if ext.lower() in OUTPUT_EXT or re.search(r'(must|create|write|generate|output)\s+[^.]{0,80}'+re.escape(_basename(p).lower()), text.lower()): spec.required_output_files.append(p)
        else: spec.required_edited_files.append(p)
        spec.required_files.append(p)
    for pat,dst in [(r'\b(?:must|should)\s+install\s+([A-Za-z0-9_.+-]+)',spec.required_binaries),(r'\b(?:command|run)\s+`?([^`\n]+)`?',spec.required_commands)]:
        for m in re.findall(pat,text,re.I): dst.append(m.strip())
    for fn in re.findall(r'\bdef\s+([A-Za-z_]\w*)|\bfunction\s+([A-Za-z_]\w*)|`([A-Za-z_]\w*)\(`', text):
        name=next((x for x in fn if x),None)
        if name: spec.required_functions.append(name)
    for m in re.findall(r'(?i)(do not edit\s+[^.\n]+|must not (?:change|modify|edit)\s+[^.\n]+|do not modify\s+[^.\n]+)',text): spec.negative_constraints.append(m.strip())
    for m in re.findall(r'(?i)(only edit\s+[^.\n]+|only replace\s+[^.\n]+|using\s+synonyms\.txt|allowed\s+synonyms[^.\n]*)',text): spec.allowed_edit_constraints.append(m.strip())
    if run:
        for t in run.toolCalls:
            # Required deliverables come from objective text and explicit artifact/path fields,
            # not from arbitrary tool output, source bodies, diffs, logs, or changed-file summaries.
            for p in _extract_paths_from_obj(_tool_args(t), spec.required_files, scan_text=False):
                if p not in spec.required_files and any(_basename(p)==_basename(x) for x in spec.required_files): spec.required_files.append(p)
    for attr in spec.__dataclass_fields__:
        vals=[]; [vals.append(x) for x in getattr(spec,attr) if x and x not in vals]; setattr(spec,attr,vals)
    return spec
def _looks_deliverable(path,spec):
    b=_basename(path).lower()
    if any(b==_basename(x).lower() or path==x for x in spec.required_files+spec.required_output_files+spec.required_edited_files+spec.required_generated_artifacts): return True
    return bool(re.search(r'(output|answer|solution|result|meeting_scheduled|posterior_.*|.*_mean)\.(json|txt|csv|ics|html)$',b))
def _ics_struct(content):
    s=(content or '').upper(); return {'kind':'ics_deliverable_structure','hasVcalendar':'BEGIN:VCALENDAR' in s,'hasVevent':'BEGIN:VEVENT' in s,'hasSummary':'SUMMARY' in s,'hasDtstart':'DTSTART' in s,'hasDtend':'DTEND' in s,'attendeeCount':s.count('ATTENDEE'),'hasEndVevent':'END:VEVENT' in s,'hasEndVcalendar':'END:VCALENDAR' in s}
def _ics_ok(preview):
    st=_ics_struct(preview); return st['hasVcalendar'] and st['hasVevent'] and st['hasSummary'] and st['hasDtstart'] and st['hasDtend'] and st['hasEndVevent'] and st['hasEndVcalendar']
def tool_call_evidence(run,spec):
    mutations=[]; inspections=[]; deliverables=[]; failures=[]
    for t in run.toolCalls:
        name=_tool_name(t); ok=_tool_success(t); paths=_extract_paths_from_obj(_tool_args(t),spec.required_files,scan_text=False) or _extract_paths_from_obj(t.raw,spec.required_files,scan_text=False)
        order=(t.turnIndex if t.turnIndex is not None else t.index); tid=t.toolCallId
        if not ok:
            failures.append(EvidenceItem('failure_signal','tool_calls','medium',f'tool[{tid}] {t.toolName} failed: {t.error or t.resultSummary}',tid,t.turnIndex,t.startedAt,order,toolCallId=tid)); continue
        if any(x in name for x in ['write','edit','patch','replace','create']):
            for p in paths or ['unknown file']:
                prev=_preview_content(t); mutations.append(EvidenceItem('file_write' if 'write' in name or 'create' in name else 'file_edit','tool_calls','high',f'{(t.toolName or name)} tool created or updated {p}'+(f' :: {prev[:120]}' if prev else ''),tid,t.turnIndex,t.startedAt,order,path=p,toolCallId=tid))
                if _looks_deliverable(p,spec):
                    strength='medium'; extra=''
                    if p.lower().endswith('.ics'):
                        struct=_ics_struct(prev); strength='strong' if _ics_ok(prev) else 'medium'; extra=' :: '+json.dumps({'path':p,**struct})
                    deliverables.append(EvidenceItem('deliverable_write','tool_calls','high',f'Successful tool call created or updated required deliverable {p}{extra}',tid,t.turnIndex,t.startedAt,order,path=p,toolCallId=tid,deliverableLinked=True,deliverableLinks=[p],validationStrength=strength))
        if 'read' in name:
            for p in paths or ['unknown file']: inspections.append(EvidenceItem('file_read','tool_calls','medium',f'Read tool inspected {p}',tid,t.turnIndex,t.startedAt,order,path=p,toolCallId=tid,deliverableLinked=_looks_deliverable(p,spec),deliverableLinks=[p] if _looks_deliverable(p,spec) else []))
    return mutations,inspections,deliverables,failures
def extract_evidence(run):
    success=[]; failures=[]; risks=[]; missing=[]; mutations=[]; validations=[]; spec=extract_deliverables(run.objective,run)
    for m in run.missingArtifacts:
        txt='No validations.jsonl artifact was present.' if m=='validations.jsonl' else f'Missing artifact: {m}'; missing.append(EvidenceItem('missing','derived','medium',txt))
    for p in run.patches:
        if p.ok: mutations.append(EvidenceItem('mutation','patches','medium',f'Patch applied successfully to {p.filePath}',order=p.index,path=p.filePath))
    for obj,src in [(run.finalSummary,'final_summary'),(run.summary,'summary')]:
        if isinstance(obj,dict):
            for p in obj.get('changed_files') or obj.get('changedFiles') or []: mutations.append(EvidenceItem('file_changed',src,'medium',f'{src}.changed_files reports changed file {p}',path=p))
    for c in run.commands:
        if c.event and not c.command: continue
        blob=((c.stdout or '')+'\n'+(c.stderr or ''))[:2000]; cmd=c.command or ''; text=f'command[{c.index}] exit={c.exitCode}: {cmd} :: {blob[:300].strip()}'
        if c.exitCode not in (None,0) or FAIL_RE.search(blob): failures.append(EvidenceItem('failure_signal','commands','high' if is_validation_command(cmd) else 'medium',text,c.toolCallId,timestamp=c.ts,order=c.index))
        if (c.exitCode==0 and is_validation_command(cmd)) or PASS_RE.search(blob) or ('git clone' in cmd and c.exitCode==0) or ('git push' in cmd and c.exitCode==0):
            ev=EvidenceItem('validation' if is_validation_command(cmd) else 'success_signal','commands','high' if is_validation_command(cmd) else 'medium',text,c.toolCallId,timestamp=c.ts,order=c.index); success.append(ev); validations.append(ev)
    tm,ti,td,tf=tool_call_evidence(run,spec); mutations+=tm; success+=td; failures+=tf
    return success,failures,risks,missing,mutations,validations,ti,td,spec
def classify_recovered(failures, successes):
    active=[]; recovered=[]
    for f in failures:
        later=[s for s in successes if s.order>f.order]
        if later: recovered.append(EvidenceItem('recovered_failure',f.source,f.confidence,'Recovered: '+f.text,f.commandId,f.turnIndex,f.timestamp,f.order))
        else: active.append(f)
    return active,recovered
