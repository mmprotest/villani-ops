from __future__ import annotations
import hashlib,re
from .types import *
VALIDATION=['test','pytest','npm test','pnpm test','yarn test','vitest','jest','go test','cargo test','mvn test','gradle test','tsc','typecheck','build','lint','curl','urllib','requests','wget','git clone','git push','nginx -t','openssl x509','grep','pass','verification','cat ']
FAIL_RE=re.compile(r'\b(FAIL|FAILED|error|exception|traceback|not found|refused|timeout|permission denied|connection refused|syntax error|missing file)\b',re.I)
PASS_RE=re.compile(r'\b(PASS|test[s]? passed|all tests passed|successful|succeeded)\b',re.I)
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
def extract_evidence(run:DebugRun):
    success=[]; failures=[]; risks=[]; missing=[]; mutations=[]; validations=[]
    for m in run.missingArtifacts:
        txt='No validations.jsonl artifact was present.' if m=='validations.jsonl' else f'Missing artifact: {m}'
        missing.append(EvidenceItem('missing','derived','medium',txt))
    for p in run.patches:
        if p.ok: mutations.append(EvidenceItem('mutation','patches','medium',f'Patch applied successfully to {p.filePath}',order=p.index))
    for c in run.commands:
        if c.event and not c.command: continue
        blob=((c.stdout or '')+'\n'+(c.stderr or ''))[:2000]; cmd=c.command or ''
        text=f'command[{c.index}] exit={c.exitCode}: {cmd} :: {blob[:300].strip()}'
        if c.exitCode not in (None,0) or FAIL_RE.search(blob): failures.append(EvidenceItem('failure_signal','commands','high' if is_validation_command(cmd) else 'medium',text,c.toolCallId,timestamp=c.ts,order=c.index))
        if (c.exitCode==0 and is_validation_command(cmd)) or PASS_RE.search(blob) or ('git clone' in cmd and c.exitCode==0) or ('git push' in cmd and c.exitCode==0):
            ev=EvidenceItem('validation' if is_validation_command(cmd) else 'success_signal','commands','high' if is_validation_command(cmd) else 'medium',text,c.toolCallId,timestamp=c.ts,order=c.index)
            success.append(ev); validations.append(ev)
    for t in run.toolCalls:
        if (t.status or '').lower() in {'failed','error'} or t.error:
            failures.append(EvidenceItem('failure_signal','tool_calls','medium',f'tool[{t.toolCallId}] {t.toolName} failed: {t.error or t.resultSummary}',t.toolCallId,t.turnIndex,t.startedAt,(t.turnIndex if t.turnIndex is not None else t.index)))
    return success,failures,risks,missing,mutations,validations
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
