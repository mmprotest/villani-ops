from __future__ import annotations
from datetime import datetime, timezone
import re, shlex
from .types import *
from .extract import extract_requirements, extract_evidence, is_validation_command
PROMPT_VERSION='villani-ops-verifier-tool-loop-v2'
CATS=['finalEndToEndValidation','testValidation','serviceValidation','repoMutation','fileMutation','setupEvidence','inspectionEvidence','cleanupEvidence','agentClaims','activeFailures','recoveredFailures','missingEvidence','riskFlags']

def re_words(s): return re.findall(r'[a-zA-Z0-9_./:-]+',(s or '').lower())
def command_text(c): return c.command or ''
def command_output_text(c): return ((c.stdout or '')+'\n'+(c.stderr or '')).strip()
def _all(c): return (command_text(c)+'\n'+command_output_text(c)).lower()
def _cmd0(c):
    try: return shlex.split(command_text(c) or '')[0].lower()
    except Exception: return (command_text(c) or '').strip().split(' ')[0].lower()

def is_inspection_command(c):
    cmd=command_text(c).strip().lower()
    first=_cmd0(c)
    exact_prefix=('which ','id ','cat /etc/os-release','cat /etc/ssh/sshd_config','pgrep','ss ','ls','find ','stat ','pwd','whoami','python --version','python3 --version','node --version','npm --version','env')
    return cmd in {'pwd','whoami','ls','id git'} or first in {'which','id','pgrep','ss','ls','stat','whoami','pwd'} or any(cmd.startswith(x) for x in exact_prefix)

def is_cleanup_command(c):
    cmd=command_text(c).lower()
    return any(x in cmd for x in ['rm -rf','rm -r ','cleanup',' -delete','find /tmp','delete','kill '])

def is_setup_or_mutation_command(c):
    cmd=command_text(c).lower()
    if is_cleanup_command(c) or is_inspection_command(c): return False
    pats=['apt install','apt-get install','apk add','pip install','npm install','useradd','adduser','chpasswd','mkdir','chmod','chown','ln -s','tee ','cat >','cat <<','echo ','openssl req','service start','service restart','systemctl start','nginx start','sshd start','write','post-receive','hook','python setup.py']
    return any(p in cmd for p in pats)

def is_service_validation_command(c):
    txt=_all(c); cmd=command_text(c).lower()
    if is_inspection_command(c) or is_setup_or_mutation_command(c): return False
    return ('nginx -t' in cmd and c.exitCode==0) or any(x in cmd for x in ['curl ','wget ','https://','http://','openssl x509','ssh ']) or 'test is successful' in txt or 'serves correct content' in txt

def is_test_validation_command(c):
    txt=_all(c); cmd=command_text(c).lower()
    if is_inspection_command(c) or is_setup_or_mutation_command(c): return False
    return any(x in cmd for x in ['pytest','npm test','pnpm test','yarn test','vitest','jest','go test','cargo test','mvn test','gradle test','tsc','typecheck','integration test']) or re.search(r'\b(pass|tests? passed|all tests passed|fail:)\b',txt,re.I)

def _strong_final_signal(c):
    txt=_all(c); cmd=command_text(c).lower()
    if is_inspection_command(c) or is_setup_or_mutation_command(c) or is_cleanup_command(c): return False
    if c.exitCode==0 and ('git clone' in cmd or 'git push' in cmd): return True
    signals=['pass:','serves correct content','clone exit: 0','push exit: 0','verification complete','deployment completed','fresh temp','/tmp/final-test','/tmp/clone']
    return c.exitCode==0 and any(s in txt for s in signals)

def detect_final_validation_window(run):
    cmds=run.commands; strong=[c.index for c in cmds if _strong_final_signal(c)]
    if not strong: return None
    start=strong[0]; end=strong[-1]
    # pull window back over adjacent temp/clone/setup-in-temp commands, not broad service setup
    for c in reversed([x for x in cmds if x.index < start]):
        if is_cleanup_command(c) or is_inspection_command(c): break
        t=_all(c)
        if any(s in t for s in ['/tmp/final','/tmp/clone','git clone','git checkout','git push','curl','https://','http://']): start=c.index
        else: break
    for c in [x for x in cmds if x.index > end]:
        if is_cleanup_command(c) or is_inspection_command(c) or (is_setup_or_mutation_command(c) and not _strong_final_signal(c)): break
        if _strong_final_signal(c) or is_service_validation_command(c) or is_test_validation_command(c): end=c.index
        else: break
    texts=' '.join(_all(c) for c in cmds if start<=c.index<=end)
    reason='git clone/push followed by PASS endpoint validation' if ('git clone' in texts and 'git push' in texts and 'pass:' in texts) else 'strong end-to-end validation signals'
    return {'startIndex':start,'endIndex':end,'reason':reason}

def is_final_end_to_end_validation_command(c, run_context=None):
    win=(run_context or {}).get('window') if isinstance(run_context,dict) else None
    if win and win['startIndex'] <= c.index <= win['endIndex'] and not (is_cleanup_command(c) or is_inspection_command(c) or is_setup_or_mutation_command(c)):
        return _strong_final_signal(c) or is_service_validation_command(c) or is_test_validation_command(c)
    return _strong_final_signal(c)

def _item(c, confidence='medium'):
    blob=command_output_text(c)[:300]
    return EvidenceItem('command','commands',confidence,f'command[{c.index}] exit={c.exitCode}: {c.command} :: {blob.strip()}',c.toolCallId,timestamp=c.ts,order=c.index)

def _classify_failures(run, failures, window):
    active=[]; recovered=[]; post=0; strong_final=window is not None
    win_start=window['startIndex'] if window else 10**9; win_end=window['endIndex'] if window else -1
    for f in failures:
        if strong_final and f.order < win_start:
            recovered.append(EvidenceItem('recovered_failure',f.source,f.confidence,'Recovered: '+f.text,f.commandId,f.turnIndex,f.timestamp,f.order))
        elif strong_final and f.order > win_end:
            c=next((x for x in run.commands if x.index==f.order),None)
            if c and (is_cleanup_command(c) or is_inspection_command(c)):
                post+=1; recovered.append(EvidenceItem('post_validation_risk',f.source,'medium','Post-validation non-blocking risk: '+f.text,f.commandId,f.turnIndex,f.timestamp,f.order))
            else: active.append(f)
        elif strong_final and win_start <= f.order <= win_end:
            active.append(f)
        else:
            active.append(f)
    return active,recovered,post

def _cat(run, success, active, recovered, risks, missing, mutations, window):
    ev={k:[] for k in CATS}
    for e in missing: ev['missingEvidence'].append(e)
    for e in risks: ev['riskFlags'].append(e)
    for e in active: ev['activeFailures'].append(e)
    for e in recovered: ev['recoveredFailures'].append(e)
    for m in mutations: ev['fileMutation'].append(m)
    ctx={'window':window}
    for c in run.commands:
        if c.event and not c.command: continue
        item=_item(c,'high' if c.exitCode==0 else 'medium')
        if is_final_end_to_end_validation_command(c,ctx): ev['finalEndToEndValidation'].append(item)
        if is_test_validation_command(c): ev['testValidation'].append(item)
        if is_service_validation_command(c): ev['serviceValidation'].append(item)
        if is_inspection_command(c): ev['inspectionEvidence'].append(item)
        if is_cleanup_command(c): ev['cleanupEvidence'].append(item)
        if is_setup_or_mutation_command(c):
            ev['setupEvidence'].append(item)
            if any(x in (c.command or '').lower() for x in ['git commit','git config','git init','git remote']): ev['repoMutation'].append(item)
    if run.modelResponses:
        ev['agentClaims'].append(EvidenceItem('agent_claim','model_responses','low',(run.modelResponses[-1].text or '')[:1000],order=run.modelResponses[-1].index))
    return ev

def _top_success(cats):
    out=[]
    for k in ['finalEndToEndValidation','testValidation','serviceValidation','repoMutation','fileMutation','setupEvidence','inspectionEvidence','agentClaims']:
        out.extend(cats.get(k,[]))
    return out[:20]

def build_packet(run:DebugRun, repo_dir=None):
    reqs=extract_requirements(run.objective); success,failures,risks,missing,mutations,validations=extract_evidence(run)
    window=detect_final_validation_window(run); active,recovered,post=_classify_failures(run,failures,window)
    cats=_cat(run,success,active,recovered,risks,missing,mutations,window); corpus='\n'.join([e.text for xs in cats.values() for e in xs]).lower()
    validations2=cats['finalEndToEndValidation']+cats['testValidation']+cats['serviceValidation']
    for r in reqs:
        words=[w for w in re_words(r.requirement) if len(w)>3]; hits=sum(1 for w in words[:8] if w in corpus)
        ok=(r.id=='final_validation_present' and bool(validations2)) or (r.id=='no_blocking_failures' and not active) or hits>=max(1,min(3,len(words)//3)) or (bool(validations2) and bool(mutations))
        r.status='satisfied' if ok else 'unclear'; r.evidence=validations2[:3] if ok else []; r.risks=missing[:2] if not ok else []
    return {'schemaVersion':'villani-ops-verifier-packet-v2','objective':run.objective,'run':{'debugDir':run.debugDir,'repoDir':repo_dir,'runId':run.runId,'model':run.model,'provider':run.provider,'status':run.status,'durationMs':run.durationMs},'requirements':to_jsonable(reqs),'evidence':to_jsonable(cats),'artifactIndex':{'debugFiles':[],'commandCount':len(run.commands),'toolCallCount':len(run.toolCalls),'patchCount':len(run.patches),'modelResponseCount':len(run.modelResponses)},'deterministicChecks':{'finalValidationWindow':window,'activeFailureCount':len(cats['activeFailures']),'recoveredFailureCount':len(cats['recoveredFailures']),'postValidationRiskCount':post}}

def deterministic_result(run:DebugRun, repo_dir=None, mode='deterministic', model=None, base_url=None):
    pkt=build_packet(run,repo_dir); cats=pkt['evidence']; active=cats['activeFailures']; validations=cats['finalEndToEndValidation']+cats['testValidation']+cats['serviceValidation']; status=(run.status or '').lower(); sat=sum(1 for r in pkt['requirements'] if r['status']=='satisfied'); coverage=sat/max(1,len(pkt['requirements']))
    if status in {'failed','crashed','timed_out','timeout'} and not validations: verdict='failure'; conf=.78; action='retry_same_model'; reason='Run status indicates failure and no later validation evidence was found.'
    elif active and any(a.get('source')=='commands' and a.get('confidence')=='high' for a in active): verdict='failure'; conf=.8; action='retry_same_model'; reason='Active blocking failure evidence remains unresolved.'
    elif validations and not active and (coverage>=.7 or cats['finalEndToEndValidation']) and status in {'completed','success',''}: verdict='success'; conf=.84; action='accept'; reason='Final validation evidence supports the task and earlier failures appear recovered.'
    elif not validations: verdict='unclear'; conf=.45; action='run_more_tests'; reason='No validation evidence was found; final answer alone is insufficient.'
    else: verdict='unclear'; conf=.55; action='inspect_manually'; reason='Evidence is incomplete or contradictory.'
    risks=cats['riskFlags']
    if mode=='deterministic': risks.append({'kind':'risk','source':'derived','confidence':'high','text':'LLM verifier was explicitly disabled; deterministic result is not authoritative.'})
    checks=pkt['deterministicChecks']; checks.update({'validationEvidenceCount':len(validations),'requirementCoverage':coverage})
    return {'schemaVersion':'villani-ops-verifier-result-v2','verdict':verdict,'confidence':conf,'recommendedAction':action,'reason':reason,'requirementResults':pkt['requirements'],'successEvidence':to_jsonable(_top_success(cats)),'failureEvidence':active[:20],'recoveredFailures':cats['recoveredFailures'][:20],'missingEvidence':cats['missingEvidence'][:20],'riskFlags':risks,'evidenceByCategory':cats,'toolsUsed':[],'llmRawVerdict':{},'artifactsUsed':pkt['artifactIndex'],'deterministicChecks':checks,'debugDir':run.debugDir,'repoDir':repo_dir,'createdAt':datetime.now(timezone.utc).isoformat(),'verifier':{'mode':mode,'model':model,'baseUrl':base_url,'promptVersion':PROMPT_VERSION}}
