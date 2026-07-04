from __future__ import annotations
import json, os, time, httpx
from urllib.parse import urlparse
from villani_ops.storage.files import FileStorage
from .deterministic import build_packet, PROMPT_VERSION
from .tools import VerifierTools
from .errors import *
ROLES={'review','selection','classification','policy','coding'}
SYSTEM='''You are the mandatory binary verifier for Villani Code runs inside Villani Ops.

You are not a coding agent.
You are not a repair agent.
You judge whether the run likely solved the user's objective using Villani Code debug artifacts and optional repo evidence.

You must make a binary prediction when verification completes.

Return result 1 if the run likely solved the task.
Return result 0 if the run likely did not solve the task.

Do not return unclear.
Do not return unknown.
Do not abstain.
No unclear verdict is allowed.

If evidence is incomplete, noisy, or contradictory, use the evidence you have and make the most conservative prediction.
False accepts are worse than false rejects.

You receive an initial evidence packet. The packet may be incomplete, noisy, or wrong. The deterministic evidence extractor is not authoritative.

You have read-only tools for inspecting debug files, command records, tool calls, transcripts, diffs, and repo files.

Use tools when:
- a material requirement is uncertain
- success evidence looks weak
- failure evidence may have been recovered
- the final answer makes an unsupported claim
- the packet contains contradictory evidence
- the task depends on file contents or diffs
- you need to confirm whether a command actually passed or failed

Do not use tools aimlessly.
Do not trust the agent's final answer unless supported by artifacts.
Earlier failures do not imply failure if later validation shows recovery.
A zero exit code does not prove success if output contains failure text.
A non-zero exit code does not prove final failure if a later end-to-end validation resolves the issue.
Visible validation passing is strong evidence but not proof.
You must distinguish between evidence that validates the actual deliverable and evidence that only validates an exploratory experiment.
Strong evidence validates the actual file, output file, service, endpoint, command, binary, function, generated artifact, or behavior required by the objective.
Weak evidence includes inline scripts that define local implementations, generic PASS strings, dependency installs, setup checks, environment inspections, exploratory benchmarks, package version checks, or agent claims that are not tied to the final deliverable.
For code tasks, a test is strongest when it imports/runs the final changed file or the project entrypoint after mutation.
For generated file tasks, successful Write to the required output file is meaningful deliverable evidence, especially if the content structure matches the objective.
For service tasks, endpoint checks against the required URL/service with expected content are strong evidence.
For build/install/instrumentation tasks, successful build/install/runtime checks and required generated artifacts/symbols are strong evidence.
For edit-constrained tasks, output passing is insufficient if the task restricts which files or words may be changed. You must verify the edit constraints before accepting.
Do not treat generic PASS output as sufficient if it does not exercise the final deliverable.
Do not treat dependency installation or environment inspection as final validation.
If deterministic validation counters conflict with deliverable linkage, rely on deliverable-linked evidence.
Return result 1 only when the evidence supports that the material requirements were satisfied.
Return result 0 when active blocking evidence remains, material requirements are unsatisfied, or evidence is too weak to safely accept.
Return only valid JSON.
'''
TOOLS=['list_debug_files','read_debug_file','search_debug_file','search_commands','read_command','search_tool_calls','read_tool_call','search_transcript','list_repo_files','read_repo_file','search_repo','read_diff','search_diff']

def _is_local(url):
    h=urlparse(url).hostname or ''; return h in {'127.0.0.1','localhost'}
def select_verifier_backend(workspace='.villani-ops', backend_name=None, base_url=None, model=None):
    if base_url and model:
        key=os.getenv('VILLANI_OPS_VERIFIER_API_KEY') or os.getenv('OPENAI_API_KEY') or ('dummy' if _is_local(base_url) else None)
        if not key: raise VerifierConfigurationError('missing API key for verifier direct config')
        return {'name':'direct','backend':'direct','baseUrl':base_url,'model':model,'apiKey':key}
    if (model or os.getenv('VILLANI_OPS_VERIFIER_MODEL')) and not backend_name and not base_url:
        bu=os.getenv('VILLANI_OPS_VERIFIER_BASE_URL') or 'http://127.0.0.1:1234/v1'; mo=model or os.getenv('VILLANI_OPS_VERIFIER_MODEL')
        key=os.getenv('VILLANI_OPS_VERIFIER_API_KEY') or os.getenv('OPENAI_API_KEY') or ('dummy' if _is_local(bu) else None)
        if not key: raise VerifierConfigurationError('missing API key for verifier direct config')
        return {'name':'direct','backend':'direct','baseUrl':bu,'model':mo,'apiKey':key}
    backs=FileStorage(workspace).load_backends()
    if backend_name:
        b=backs.get(backend_name)
        if not b: raise VerifierConfigurationError(f'missing verifier backend: {backend_name}')
        if not b.enabled: raise VerifierConfigurationError(f'verifier backend disabled: {backend_name}')
    else:
        elig=[b for b in backs.values() if b.enabled and (set(b.roles)&ROLES)]
        if not elig: raise VerifierConfigurationError('missing verifier backend or model config')
        b=sorted(elig,key=lambda x:(-x.capability_score,x.output_cost_per_million,x.input_cost_per_million,x.name))[0]
    bu=base_url or b.base_url or 'http://127.0.0.1:1234/v1'; mo=model or b.model
    if not mo or not bu: raise VerifierConfigurationError('missing verifier model or base URL')
    key=b.resolved_api_key() or ('dummy' if _is_local(bu) else None)
    if not key: raise VerifierConfigurationError(f'verifier backend {b.name} has no usable API key')
    return {'name':b.name,'backend':b.name,'baseUrl':bu,'model':mo,'apiKey':key}
def _chat(cfg,messages,timeout, trace=None):
    started=time.time(); raw=None; status="ok"; http_status=None
    try:
        r=httpx.post(cfg['baseUrl'].rstrip('/')+'/chat/completions',headers={'Authorization':f"Bearer {cfg['apiKey']}"},json={'model':cfg['model'],'messages':messages,'temperature':0},timeout=timeout)
        http_status=getattr(r,'status_code',None); r.raise_for_status(); raw=r.json(); return raw['choices'][0]['message'].get('content','')
    except Exception:
        status='error'; raise
    finally:
        if trace is not None:
            trace.append_jsonl('llm_raw_responses.jsonl',{'index':getattr(trace,'llm_call_count',0),'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'durationMs':int((time.time()-started)*1000),'provider':'openai-compatible','baseUrl':cfg.get('baseUrl'),'model':cfg.get('model'),'status':status,'httpStatus':http_status,'usage':(raw or {}).get('usage') if isinstance(raw,dict) else None,'raw':raw})
            trace.llm_call_count=getattr(trace,'llm_call_count',0)+1
def _parse(s):
    try: obj=json.loads(s)
    except Exception as e: raise VerifierSchemaError(str(e))
    if obj.get('type')=='tool_call': return obj
    if obj.get('type') is None and ('result' in obj or obj.get('verdict') in {'success','failure'}): obj['type']='final_verdict'
    if obj.get('type')!='final_verdict': raise VerifierSchemaError('invalid final verdict schema')
    if obj.get('result') not in (0,1): raise VerifierSchemaError('final verdict result must be 0 or 1')
    if obj.get('verdict') not in {'success','failure'}: raise VerifierSchemaError('final verdict verdict must be success or failure')
    if (obj['result']==1 and obj['verdict']!='success') or (obj['result']==0 and obj['verdict']!='failure'): raise VerifierSchemaError('result/verdict mismatch')
    if obj.get('recommendedAction') not in {'accept','reject','retry_same_model','retry_higher_model','run_more_tests','inspect_manually'}: obj['recommendedAction']='inspect_manually'
    obj.setdefault('confidence',0.0)
    for k in ['reason','requirementResults','successEvidence','failureEvidence','recoveredFailures','missingEvidence','riskFlags','toolsUsed']: obj.setdefault(k, [] if k!='reason' else '')
    obj.setdefault('uncertainty', {'level':'medium','reasons':[]})
    obj.setdefault('deliverableAssessment', {'requiredDeliverables':[],'validatedDeliverables':[],'missingDeliverables':[],'weakValidationReasons':[]})
    obj.setdefault('constraintAssessment', {'constraints':[],'satisfiedConstraints':[],'violatedConstraints':[],'uncheckedConstraints':[]})
    for r in obj.get('requirementResults') or []:
        if r.get('status') not in {'satisfied','unsatisfied'}: raise VerifierSchemaError('invalid requirement status')
    return obj
def _schema_text():
    return """Final verdict schema (return exactly this shape):
{ "type": "final_verdict", "result": 1, "verdict": "success", "confidence": 0.84, "recommendedAction": "accept", "reason": "short explanation grounded in evidence", "deliverableAssessment": {"requiredDeliverables":["string"],"validatedDeliverables":["string"],"missingDeliverables":["string"],"weakValidationReasons":["string"]}, "constraintAssessment": {"constraints":["string"],"satisfiedConstraints":["string"],"violatedConstraints":["string"],"uncheckedConstraints":["string"]}, "requirementResults": [{"id":"string","requirement":"string","status":"satisfied | unsatisfied","evidence":["string"],"risks":["string"]}], "successEvidence": ["string"], "failureEvidence": ["string"], "recoveredFailures": ["string"], "missingEvidence": ["string"], "riskFlags": ["string"], "uncertainty": {"level": "low | medium | high", "reasons": ["string"]}, "toolsUsed": [{"tool":"string","reason":"string"}] }
Rules: result must be 1 or 0. verdict must be success when result is 1. verdict must be failure when result is 0. requirementResults.status must be satisfied or unsatisfied only. Do not return unclear. Do not return unknown. Do not return null result. If evidence is incomplete, make the best conservative prediction and explain uncertainty. False accepts are worse than false rejects.
Tool-call schema:
{ "type": "tool_call", "tool": "search_commands", "args": {"query": "PASS", "limit": 10} }
Return exactly one JSON object."""
def _repair(cfg,bad,timeout, trace=None):
    msg=[{'role':'system','content':'Return only valid JSON. No markdown fences.'},{'role':'user','content':'Repair this invalid verifier response to required final_verdict schema. '+_schema_text()+'\nPrevious invalid response:\n'+bad}]
    if trace is not None:
        for m in msg: trace.append_jsonl('llm_messages.jsonl',{'index':getattr(trace,'msg_count',0),'role':m['role'],'name':None,'content':m['content'],'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(m['content']))}); trace.msg_count=getattr(trace,'msg_count',0)+1
    content=_chat(cfg,msg,timeout,trace=trace)
    if trace is not None:
        trace.append_jsonl('llm_messages.jsonl',{'index':getattr(trace,'msg_count',0),'role':'assistant','name':None,'content':content,'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(content))}); trace.msg_count=getattr(trace,'msg_count',0)+1
    return _parse(content)
def _adjudication_input(det, verdict, contradictions):
    cats=det.get('evidenceByCategory',{})
    return {'originalLlmVerdict':{k:verdict.get(k) for k in ['result','verdict','confidence','reason','recommendedAction','deliverableAssessment','constraintAssessment']},'deterministicContradictions':contradictions,'activeFailures':cats.get('activeFailures',[]),'recoveredFailures':cats.get('recoveredFailures',[]),'deliverableEvidence':cats.get('deliverableEvidence',[]),'finalEndToEndValidation':cats.get('finalEndToEndValidation',[]),'testValidation':cats.get('testValidation',[]),'serviceValidation':cats.get('serviceValidation',[]),'toolObservations':det.get('toolsUsed',[]),'instruction':'Decide whether to keep or change the binary result. False accepts are worse than false rejects.'}
def run_verifier_adjudication(cfg, adjudication_input, timeout, trace=None):
    schema='Return JSON: {"type":"adjudication_verdict","result":1,"verdict":"success","confidence":0.84,"recommendedAction":"accept","reason":"...","changedOriginalResult":false,"riskFlags":["..."]}'
    msgs=[{'role':'system','content':SYSTEM+'\nYou are adjudicating a conflict between an LLM verifier verdict and deterministic evidence. Return only JSON.'},{'role':'user','content':schema+'\nAdjudication input:\n'+json.dumps(adjudication_input,default=str)}]
    if trace is not None:
        for m in msgs: trace.append_jsonl('llm_messages.jsonl',{'index':getattr(trace,'msg_count',0),'role':m['role'],'name':None,'content':m['content'],'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(m['content']))}); trace.msg_count=getattr(trace,'msg_count',0)+1
    raw=_chat(cfg,msgs,timeout,trace=trace)
    obj=json.loads(raw)
    if obj.get('type')!='adjudication_verdict' or obj.get('result') not in (0,1): raise VerifierSchemaError('invalid adjudication schema')
    obj['verdict']='success' if obj['result']==1 else 'failure'
    return obj
def calibrate(det, verdict, trace=None, cfg=None, timeout=30):
    raw={'result':verdict.get('result'),'verdict':verdict['verdict'],'confidence':float(verdict.get('confidence',0)),'reason':verdict.get('reason','')}; changed=False
    cats=det.get('evidenceByCategory',{})
    validations=cats.get('finalEndToEndValidation',[])+cats.get('testValidation',[])+cats.get('serviceValidation',[])
    strong=[v for v in validations if isinstance(v,dict) and v.get('validationStrength')=='strong']
    active=cats.get('activeFailures',[])
    verdict['verdict']='success' if verdict['result']==1 else 'failure'
    rules=['schema_consistency']
    contradictions=[]
    if verdict['result']==1 and active: contradictions.append('LLM success but deterministic active failures remain.')
    if verdict['result']==0 and (strong or cats.get('finalEndToEndValidation')): contradictions.append('LLM failure but deterministic validation evidence looks strong.')
    da=det.get('deliverableAssessment') or {}
    if verdict['result']==1 and da.get('requiredDeliverables') and not (cats.get('deliverableEvidence') or strong): contradictions.append('LLM success but deterministic deliverable evidence is missing.')
    if verdict['result']==0 and cats.get('deliverableEvidence') and strong: contradictions.append('LLM failure but deliverable evidence and strong validation exist.')
    adj_in=None; adj=None
    if contradictions:
        rules.append('evidence_arbitration_adjudication')
        adj_in=_adjudication_input(det,verdict,contradictions)
        if cfg is not None:
            try:
                adj=run_verifier_adjudication(cfg,adj_in,timeout,trace=trace)
                if adj.get('result') != verdict.get('result'):
                    old=raw.copy()
                    verdict.update({'result':adj['result'],'verdict':adj['verdict'],'confidence':adj.get('confidence',verdict.get('confidence')),'recommendedAction':adj.get('recommendedAction',verdict.get('recommendedAction')),'reason':adj.get('reason',verdict.get('reason'))})
                    changed=True; raw=old
                verdict.setdefault('riskFlags',[]).extend(adj.get('riskFlags') or ['Deterministic disagreement adjudicated.'])
            except Exception as e:
                verdict.setdefault('riskFlags',[]).append(f'Adjudication failed; kept original LLM verdict: {e}')
        else:
            verdict.setdefault('riskFlags',[]).append('Deterministic disagreement noted; kept original LLM verdict without hard calibration flip.')
    if verdict['result']==1 and float(verdict.get('confidence',0))>.9:
        verdict['confidence']=.9; verdict.setdefault('riskFlags',[]).append('Success confidence capped at 0.9.'); rules.append('success_confidence_cap')
    verdict.setdefault('uncertainty', {'level':'medium','reasons':[]})
    cal={'schemaVersion':'villani-ops-verifier-calibration-v1','before':raw,'after':{'result':verdict.get('result'),'verdict':verdict.get('verdict'),'confidence':verdict.get('confidence'),'reason':verdict.get('reason')},'changes':([] if not changed else [{'field':'result/verdict/confidence/reason','from':raw,'to':{'result':verdict.get('result'),'verdict':verdict.get('verdict'),'confidence':verdict.get('confidence'),'reason':verdict.get('reason')},'reason':'Adjudication changed the LLM result based on hard contradictory evidence.'}]),'rulesApplied':rules,'adjudicationInputSummary':adj_in,'adjudicationResult':adj}
    if trace is not None: trace.write_json('calibration.json',cal)
    verdict['_calibration']=cal
    verdict['llmRawVerdict']=raw; return validate_final_result_consistency(verdict)
def llm_result(run, det, workspace='.villani-ops', backend=None, base_url=None, model=None, timeout_seconds=180, max_tool_calls=12, max_tool_result_chars=12000, max_read_lines=160, trace=None):
    cfg=select_verifier_backend(workspace,backend,base_url,model); tools=VerifierTools(run,det.get('repoDir'),max_tool_result_chars,max_read_lines)
    packet=build_packet(run,det.get('repoDir'))
    messages=[{'role':'system','content':SYSTEM},{'role':'user','content':'Objective:\n'+str(run.objective)+'\nAvailable tools: '+', '.join(TOOLS)+'\n'+_schema_text()+'\nEvidence packet:\n'+json.dumps(packet,default=str)}]
    if trace is not None:
        trace.msg_count=getattr(trace,'msg_count',0)
        for m in messages:
            trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':m['role'],'name':None,'content':m['content'],'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(m['content']))}); trace.msg_count+=1
    used=[]; deadline=time.monotonic()+timeout_seconds; calls=0; content=''
    while True:
        if time.monotonic()>deadline: raise VerifierLlmError('tool loop timeout')
        try: content=_chat(cfg,messages,max(1,deadline-time.monotonic()),trace=trace)
        except Exception as e:
            raise VerifierLlmError(f'HTTP failure: {e}')
        try: obj=_parse(content)
        except VerifierSchemaError:
            try: obj=_repair(cfg,content,max(1,deadline-time.monotonic()),trace=trace)
            except Exception as e: raise VerifierSchemaError(f'invalid JSON after repair: {e}')
        if obj.get('type')=='tool_call':
            if calls>=max_tool_calls:
                messages.append({'role':'user','content':'Maximum tool calls reached. Return final_verdict JSON using evidence gathered so far.'});
                max_tool_calls=-1; continue
            name=obj.get('tool'); args=obj.get('args') or {}; idx=calls; calls+=1; used.append({'tool':name,'reason':'LLM requested tool'})
            start=time.time(); status='ok'; err=None
            try: res=tools.dispatch(name,args)
            except Exception as e: res=json.dumps({'error':str(e)}); status='error'; err=str(e)
            if trace is not None:
                trace.append_jsonl('tool_calls.jsonl',{'index':idx,'tool':name,'args':args,'startedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'completedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'durationMs':int((time.time()-start)*1000),'status':status,'resultChars':len(res),'truncated':len(res)>=max_tool_result_chars,'error':err,'reason':'LLM requested tool call.'})
                trace.append_jsonl('tool_observations.jsonl',{'toolCallIndex':idx,'tool':name,'observation':None,'observationText':res,'chars':len(res),'truncated':len(res)>=max_tool_result_chars})
            messages.append({'role':'assistant','content':json.dumps(obj)})
            messages.append({'role':'user','content':'Tool result for '+str(name)+':\n'+res})
            if trace is not None:
                for m in messages[-2:]: trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':m['role'],'name':None,'content':m['content'],'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(m['content']))}); trace.msg_count+=1
            continue
        
        if trace is not None:
            trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':'assistant','name':None,'content':content,'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(content))}); trace.msg_count+=1
            trace.write_json('llm_final_verdict_raw.json',{'rawText':content,'parsed':obj})
            parsed={'schemaVersion':'villani-ops-verifier-llm-verdict-v1',**{k:obj.get(k) for k in ['result','verdict','confidence','recommendedAction','reason','deliverableAssessment','constraintAssessment','requirementResults','successEvidence','failureEvidence','recoveredFailures','missingEvidence','riskFlags','uncertainty','toolsUsed']}}
            trace.write_json('llm_final_verdict_parsed.json',parsed)
        obj=calibrate(det,obj,trace=trace,cfg=cfg,timeout=max(1,deadline-time.monotonic())); break
    det.update({'result':obj['result'],'verdict':obj['verdict'],'confidence':obj['confidence'],'recommendedAction':obj['recommendedAction'],'reason':obj['reason'],'deliverableAssessment':obj.get('deliverableAssessment',det.get('deliverableAssessment')),'constraintAssessment':obj.get('constraintAssessment',det.get('constraintAssessment')),'requirementResults':obj.get('requirementResults',det['requirementResults']),'successEvidence':obj.get('successEvidence',det['successEvidence']),'failureEvidence':obj.get('failureEvidence',det['failureEvidence']),'recoveredFailures':obj.get('recoveredFailures',det['recoveredFailures']),'missingEvidence':obj.get('missingEvidence',det['missingEvidence']),'riskFlags':obj.get('riskFlags',det['riskFlags']),'toolsUsed':used+obj.get('toolsUsed',[]),'llmRawVerdict':obj.get('llmRawVerdict',{}),'calibration':obj.get('_calibration',{}),'verifier':{'mode':'llm_tool_loop','backend':cfg['backend'],'model':cfg['model'],'baseUrl':cfg['baseUrl'],'promptVersion':PROMPT_VERSION}})
    return validate_final_result_consistency(det)

def validate_final_result_consistency(result):
    expected={1:'success',0:'failure',None:'error'}.get(result.get('result'))
    flags=result.setdefault('riskFlags',[])
    if expected and result.get('verdict')!=expected:
        result['verdict']=expected; flags.append('Final consistency fixed result/verdict mismatch.')
    reason=(result.get('reason') or '').lower()
    if result.get('result')==1:
        if result.get('recommendedAction')=='reject': result['recommendedAction']='accept'; flags.append('Final consistency fixed success recommendedAction.')
        if any(p in reason for p in ['run failed','did not solve','unsatisfied','blocking failure']) and not any(p in reason for p in ['despite','although']):
            result['reason']='The verifier accepted the run based on deliverable-linked evidence; stale contradictory success/failure wording was replaced.'; flags.append('Final consistency replaced stale failure reason under success verdict.')
    elif result.get('result')==0:
        if result.get('recommendedAction')=='accept': result['recommendedAction']='reject'; flags.append('Final consistency fixed failure recommendedAction.')
        if any(p in reason for p in ['run succeeded','successfully solved','accepted because']) and not any(p in reason for p in ['not ', 'despite','although']):
            result['reason']='The verifier rejected the run based on unresolved blocking or constraint evidence; stale success wording was replaced.'; flags.append('Final consistency replaced stale success reason under failure verdict.')
    return result
