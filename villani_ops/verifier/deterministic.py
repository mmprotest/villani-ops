from __future__ import annotations
from datetime import datetime, timezone
from .types import *
from .extract import extract_requirements, extract_evidence, classify_recovered, is_validation_command
PROMPT_VERSION='villani-ops-verifier-tool-loop-v1'
CATS=['finalEndToEndValidation','testValidation','serviceValidation','repoMutation','fileMutation','setupEvidence','inspectionEvidence','cleanupEvidence','agentClaims','activeFailures','recoveredFailures','missingEvidence','riskFlags']
def re_words(s):
    import re; return re.findall(r'[a-zA-Z0-9_./:-]+',(s or '').lower())
def _cat(run, success, active, recovered, risks, missing, mutations):
    ev={k:[] for k in CATS}
    for e in missing: ev['missingEvidence'].append(e)
    for e in risks: ev['riskFlags'].append(e)
    for e in active: ev['activeFailures'].append(e)
    for e in recovered: ev['recoveredFailures'].append(e)
    for m in mutations: ev['fileMutation'].append(m)
    for c in run.commands:
        cmd=(c.command or '').lower(); blob=((c.stdout or '')+'\n'+(c.stderr or ''))[:300]
        item=EvidenceItem('command','commands','medium',f'command[{c.index}] exit={c.exitCode}: {c.command} :: {blob.strip()}',c.toolCallId,timestamp=c.ts,order=c.index)
        if any(x in cmd for x in ['git clone','git push','curl','wget','https://','http://']) and c.index>=max(0,len(run.commands)-4): ev['finalEndToEndValidation'].append(item)
        elif any(x in cmd for x in ['pytest','npm test','pnpm test','go test','cargo test','tsc','typecheck','build']) or 'pass:' in blob.lower() or 'fail:' in blob.lower(): ev['testValidation'].append(item)
        elif any(x in cmd for x in ['nginx -t','openssl x509','ssh ','sshd','systemctl','service ']): ev['serviceValidation'].append(item)
        elif any(x in cmd for x in ['which ','id ','cat /etc/os-release','pgrep','env']): ev['inspectionEvidence'].append(item)
        elif any(x in cmd for x in ['rm ','cleanup','delete','kill ']): ev['cleanupEvidence'].append(item)
        elif any(x in cmd for x in ['chmod','chown','mkdir','tee ','cat >','echo ','openssl req','useradd','adduser','nginx','hook']): ev['setupEvidence'].append(item)
    if run.modelResponses:
        ev['agentClaims'].append(EvidenceItem('agent_claim','model_responses','low',(run.modelResponses[-1].text or '')[:1000],order=run.modelResponses[-1].index))
    return ev
def build_packet(run:DebugRun, repo_dir=None):
    reqs=extract_requirements(run.objective); success,failures,risks,missing,mutations,validations=extract_evidence(run); active,recovered=classify_recovered(failures,success)
    cats=_cat(run,success,active,recovered,risks,missing,mutations); corpus='\n'.join([e.text for xs in cats.values() for e in xs]).lower()
    for r in reqs:
        words=[w for w in re_words(r.requirement) if len(w)>3]; hits=sum(1 for w in words[:8] if w in corpus)
        ok=(r.id=='final_validation_present' and bool(validations)) or (r.id=='no_blocking_failures' and not active) or hits>=max(1,min(3,len(words)//3)) or (bool(validations) and bool(mutations))
        r.status='satisfied' if ok else 'unclear'; r.evidence=success[:3] if ok else []; r.risks=missing[:2] if not ok else []
    return {'schemaVersion':'villani-ops-verifier-packet-v2','objective':run.objective,'run':{'debugDir':run.debugDir,'repoDir':repo_dir,'runId':run.runId,'model':run.model,'provider':run.provider,'status':run.status,'durationMs':run.durationMs},'requirements':to_jsonable(reqs),'evidence':to_jsonable(cats),'artifactIndex':{'debugFiles':[],'commandCount':len(run.commands),'toolCallCount':len(run.toolCalls),'patchCount':len(run.patches),'modelResponseCount':len(run.modelResponses)}}
def deterministic_result(run:DebugRun, repo_dir=None, mode='deterministic', model=None, base_url=None):
    pkt=build_packet(run,repo_dir); reqs=[RequirementCheck(**{k:v for k,v in r.items() if k in {'id','requirement','status'}}) for r in pkt['requirements']]
    cats=pkt['evidence']; active=cats['activeFailures']; validations=cats['finalEndToEndValidation']+cats['testValidation']+cats['serviceValidation']; status=(run.status or '').lower(); sat=sum(1 for r in pkt['requirements'] if r['status']=='satisfied'); coverage=sat/max(1,len(pkt['requirements']))
    if status in {'failed','crashed','timed_out','timeout'} and not validations: verdict='failure'; conf=.78; action='retry_same_model'; reason='Run status indicates failure and no later validation evidence was found.'
    elif active and not validations and any(a.get('source')=='commands' and a.get('confidence')=='high' for a in active): verdict='failure'; conf=.8; action='retry_same_model'; reason='Active blocking failure evidence remains unresolved.'
    elif validations and not active and coverage>=.7 and status in {'completed','success',''}: verdict='success'; conf=.84; action='accept'; reason='Final validation evidence supports the task and earlier failures appear recovered.'
    elif active and validations and any(a.get('source')=='commands' and a.get('confidence')=='high' for a in active): verdict='failure'; conf=.8; action='retry_same_model'; reason='Active blocking failure evidence remains unresolved.'
    elif not validations: verdict='unclear'; conf=.45; action='run_more_tests'; reason='No validation evidence was found; final answer alone is insufficient.'
    else: verdict='unclear'; conf=.55; action='inspect_manually'; reason='Evidence is incomplete or contradictory.'
    risks=cats['riskFlags']
    if mode=='deterministic': risks.append({'kind':'risk','source':'derived','confidence':'high','text':'LLM verifier was explicitly disabled; deterministic result is not authoritative.'})
    return {'schemaVersion':'villani-ops-verifier-result-v2','verdict':verdict,'confidence':conf,'recommendedAction':action,'reason':reason,'requirementResults':pkt['requirements'],'successEvidence':validations[:20],'failureEvidence':active[:20],'recoveredFailures':cats['recoveredFailures'][:20],'missingEvidence':cats['missingEvidence'][:20],'riskFlags':risks,'evidenceByCategory':cats,'toolsUsed':[],'llmRawVerdict':{},'artifactsUsed':pkt['artifactIndex'],'deterministicChecks':{'validationEvidenceCount':len(validations),'activeFailureCount':len(active),'recoveredFailureCount':len(cats['recoveredFailures']),'requirementCoverage':coverage},'debugDir':run.debugDir,'repoDir':repo_dir,'createdAt':datetime.now(timezone.utc).isoformat(),'verifier':{'mode':mode,'model':model,'baseUrl':base_url,'promptVersion':PROMPT_VERSION}}
