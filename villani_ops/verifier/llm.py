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
    for r in obj.get('requirementResults') or []:
        if r.get('status') not in {'satisfied','unsatisfied'}: raise VerifierSchemaError('invalid requirement status')
    return obj
def _schema_text():
    return """Final verdict schema (return exactly this shape):
{ "type": "final_verdict", "result": 1, "verdict": "success", "confidence": 0.84, "recommendedAction": "accept", "reason": "short explanation grounded in evidence", "requirementResults": [{"id":"string","requirement":"string","status":"satisfied | unsatisfied","evidence":["string"],"risks":["string"]}], "successEvidence": ["string"], "failureEvidence": ["string"], "recoveredFailures": ["string"], "missingEvidence": ["string"], "riskFlags": ["string"], "uncertainty": {"level": "low | medium | high", "reasons": ["string"]}, "toolsUsed": [{"tool":"string","reason":"string"}] }
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
def calibrate(det, verdict, trace=None):
    raw={'result':verdict.get('result'),'verdict':verdict['verdict'],'confidence':float(verdict.get('confidence',0)),'reason':verdict.get('reason','')}; changed=False
    validations=det['evidenceByCategory']['finalEndToEndValidation']+det['evidenceByCategory']['testValidation']+det['evidenceByCategory']['serviceValidation']
    active=det['evidenceByCategory']['activeFailures']
    reqs=verdict.get('requirementResults',[])
    if verdict['result']==1:
        if not validations or any(r.get('status')=='unsatisfied' for r in reqs) or active:
            verdict.update({'result':0,'verdict':'failure','recommendedAction':'run_more_tests','confidence':min(float(verdict.get('confidence',0)) or .6,.65)}); changed=True
        verdict['confidence']=min(float(verdict.get('confidence',0)),.9)
    elif verdict['result']==0 and det['evidenceByCategory']['finalEndToEndValidation'] and not active and not any(r.get('status')=='unsatisfied' for r in det.get('requirementResults',[])):
        verdict.update({'result':1,'verdict':'success','recommendedAction':'accept','confidence':min(max(float(verdict.get('confidence',0)),.75),.9)}); changed=True
    if changed: verdict.setdefault('riskFlags',[]).append('Calibration changed the LLM result based on deterministic evidence checks.')
    verdict.setdefault('uncertainty', {'level':'medium','reasons':[]})
    cal={'schemaVersion':'villani-ops-verifier-calibration-v1','before':raw,'after':{'result':verdict.get('result'),'verdict':verdict.get('verdict'),'confidence':verdict.get('confidence')},'changes':([] if not changed else [{'field':'result/verdict/confidence','from':raw,'to':{'result':verdict.get('result'),'verdict':verdict.get('verdict'),'confidence':verdict.get('confidence')},'reason':'Calibration adjusted verdict using deterministic evidence checks.'}]),'rulesApplied':(['deterministic_evidence_consistency'] if changed else [])}
    if trace is not None: trace.write_json('calibration.json',cal)
    verdict['_calibration']=cal
    verdict['llmRawVerdict']=raw; return verdict
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
            parsed={'schemaVersion':'villani-ops-verifier-llm-verdict-v1',**{k:obj.get(k) for k in ['result','verdict','confidence','recommendedAction','reason','requirementResults','successEvidence','failureEvidence','recoveredFailures','missingEvidence','riskFlags','uncertainty','toolsUsed']}}
            trace.write_json('llm_final_verdict_parsed.json',parsed)
        obj=calibrate(det,obj,trace=trace); break
    det.update({'result':obj['result'],'verdict':obj['verdict'],'confidence':obj['confidence'],'recommendedAction':obj['recommendedAction'],'reason':obj['reason'],'requirementResults':obj.get('requirementResults',det['requirementResults']),'successEvidence':obj.get('successEvidence',det['successEvidence']),'failureEvidence':obj.get('failureEvidence',det['failureEvidence']),'recoveredFailures':obj.get('recoveredFailures',det['recoveredFailures']),'missingEvidence':obj.get('missingEvidence',det['missingEvidence']),'riskFlags':obj.get('riskFlags',det['riskFlags']),'toolsUsed':used+obj.get('toolsUsed',[]),'llmRawVerdict':obj.get('llmRawVerdict',{}),'verifier':{'mode':'llm_tool_loop','backend':cfg['backend'],'model':cfg['model'],'baseUrl':cfg['baseUrl'],'promptVersion':PROMPT_VERSION}})
    return det
