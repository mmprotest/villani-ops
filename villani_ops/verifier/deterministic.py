from __future__ import annotations
from datetime import datetime, timezone
from .types import *
from .extract import extract_requirements, extract_evidence, classify_recovered
def build_packet(run:DebugRun, repo_dir=None):
    reqs=extract_requirements(run.objective); success,failures,risks,missing,mutations,validations=extract_evidence(run); active,recovered=classify_recovered(failures,success)
    corpus='\n'.join([e.text for e in success+mutations+validations]).lower()
    for r in reqs:
        words=[w for w in re_words(r.requirement) if len(w)>3]
        hits=sum(1 for w in words[:8] if w in corpus)
        if r.id in {'final_validation_present'}: ok=bool(validations)
        elif r.id in {'no_blocking_failures'}: ok=not active
        else: ok=hits>=max(1,min(3,len(words)//3)) or (bool(validations) and any('PASS' in e.text for e in validations) and bool(mutations)) or (bool(validations) and len(reqs)<=3 and r.id=='task_objective_addressed')
        r.status='satisfied' if ok else 'unclear'; r.evidence=success[:3] if ok else []; r.risks=missing[:2] if not ok else []
    return {'run':run,'requirements':reqs,'success':success[:20],'active':active[:20],'recovered':recovered[:20],'risks':risks[:20],'missing':missing[:20],'mutations':mutations[:20],'validations':validations[:20], 'repoDir':repo_dir}
def re_words(s):
    import re; return re.findall(r'[a-zA-Z0-9_./:-]+',s.lower())
def deterministic_result(run:DebugRun, repo_dir=None, mode='deterministic', model=None, base_url=None):
    p=build_packet(run, repo_dir); reqs=p['requirements']; active=p['active']; validations=p['validations']
    status=(run.status or '').lower(); sat=sum(1 for r in reqs if r.status=='satisfied'); coverage=sat/max(1,len(reqs))
    if status in {'failed','crashed','timed_out','timeout'} and not validations: verdict='failure'; conf=.78; action='retry_same_model'; reason='Run status indicates failure and no later validation evidence was found.'
    elif active and any(a.confidence=='high' for a in active) and not validations: verdict='failure'; conf=.8; action='retry_same_model'; reason='Active blocking failure evidence remains unresolved.'
    elif validations and not active and coverage>=.7 and status in {'completed','success',''}: verdict='success'; conf=.84; action='accept'; reason='Final validation evidence supports the task and earlier failures appear recovered.'
    elif active and validations: verdict='unclear'; conf=.5; action='inspect_manually'; reason='Validation evidence exists but conflicts with active failure evidence.'
    elif not validations: verdict='unclear'; conf=.45; action='run_more_tests'; reason='No validation evidence was found; final answer alone is insufficient.'
    else: verdict='unclear'; conf=.55; action='inspect_manually'; reason='Evidence is incomplete for one or more material requirements.'
    return {'schemaVersion':'villani-ops-verifier-result-v1','verdict':verdict,'confidence':conf,'recommendedAction':action,'reason':reason,'requirementResults':to_jsonable(reqs),'successEvidence':to_jsonable(p['success']),'failureEvidence':to_jsonable(active),'recoveredFailures':to_jsonable(p['recovered']),'missingEvidence':to_jsonable(p['missing']),'riskFlags':to_jsonable(p['risks']),'artifactsUsed':{'session_meta.json':bool(run.sessionMeta),'summary.json':bool(run.summary),'final_summary.json':bool(run.finalSummary),'commands.jsonl':bool(run.commands),'tool_calls.jsonl':bool(run.toolCalls),'patches.jsonl':bool(run.patches),'model_responses.jsonl':bool(run.modelResponses),'validations.jsonl':bool(run.validations),'repoDir':bool(repo_dir)},'deterministicChecks':{'validationEvidenceCount':len(validations),'activeFailureCount':len(active),'recoveredFailureCount':len(p['recovered']),'requirementCoverage':coverage},'debugDir':run.debugDir,'repoDir':repo_dir,'createdAt':datetime.now(timezone.utc).isoformat(),'verifier':{'mode':mode,'model':model,'baseUrl':base_url,'promptVersion':'villani-ops-verifier-v1'}}
