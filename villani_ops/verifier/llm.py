from __future__ import annotations
import json, os, time, httpx
from urllib.parse import urlparse
from villani_ops.storage.files import FileStorage
from .deterministic import build_packet, PROMPT_VERSION
from .tools import VerifierTools
from .errors import *
ROLES={'review','selection','classification','policy','coding'}
SYSTEM='''You are the mandatory binary verifier for Villani Code runs inside Villani Ops.

You are the judge. The deterministic evidence collector is not authoritative.
You are not a coding agent.
You are not a repair agent.
You judge whether the run likely solved the user's objective using Villani Code debug artifacts and optional repo evidence.

You must make a binary prediction:
result 1 means the run likely solved the task.
result 0 means the run likely did not solve the task.

Do not return unclear or unknown.
Do not abstain.
No unclear verdict is allowed.

First identify the task contract:
- required outputs
- required file modifications
- required behavior
- required services or installability
- required performance or quality constraints
- forbidden changes
- allowed-edit constraints
- negative requirements such as "do not", "must not", "only", "no warnings"

Then judge whether the evidence verifies that contract.

Distinguish:
- evidence that validates the actual deliverable
- evidence that validates only setup
- evidence that validates only an exploratory experiment
- evidence that is an agent claim
- evidence that is self-validation
- evidence from independent or downstream validation

Treat deterministic labels such as activeFailures, finalEndToEndValidation, deliverableEvidence, and recoveredFailures as hints, not conclusions. The deterministic labels are hints, not conclusions. The deterministic labels are candidate labels only. They are not authoritative. Decide whether the evidence actually proves or contradicts the task contract.

Use tools when the packet is insufficient, contradictory, or when accepting the run depends on exact evidence.
False accepts are worse than false rejects, but you must still make a binary prediction.
If evidence is weak, make the conservative prediction and explain uncertainty.

Accept only when the evidence supports the actual task contract.

Reject when:
- the final deliverable is not verified
- the validation tests a local/exploratory substitute instead of the actual deliverable
- required downstream behavior is not shown
- required performance/quality constraints are not demonstrated
- negative constraints or forbidden changes appear violated
- the evidence is too weak to safely accept

Use tools before accepting when:
- the task requires generated output files and content is not shown
- the task restricts allowed edits or forbidden files
- performance or quality constraints are material
- installability/service/downstream consumer behavior is material
- there are earlier failures that may or may not be recovered
- success depends on diff/file content

You have read-only tools for inspecting debug files, command records, tool calls, transcripts, diffs, and repo files.
Do not use tools aimlessly.
Do not trust the agent's final answer unless supported by artifacts.
Earlier failures do not imply failure if later validation shows recovery.
A zero exit code does not prove success if output contains failure text.
A non-zero exit code does not prove final failure if a later end-to-end validation resolves the issue.
Visible validation passing is strong evidence but not proof.
Strong evidence validates the actual file, output file, service, endpoint, command, binary, function, generated artifact, or behavior required by the objective.
Weak evidence includes inline scripts that define local implementations, generic PASS strings, dependency installs, setup checks, environment inspections, exploratory benchmarks, package version checks, or agent claims that are not tied to the final deliverable.
For code tasks, a test is strongest when it imports/runs the final changed file or the project entrypoint after mutation.
For generated file tasks, successful Write to the required output file is meaningful deliverable evidence, especially if the content structure matches the objective.
For service tasks, endpoint checks against the required URL/service with expected content are strong evidence.
For build/install/instrumentation tasks, successful build/install/runtime checks and required generated artifacts/symbols are strong evidence.
For edit-constrained tasks, output passing is insufficient if the task restricts which files or words may be changed. You must verify the edit constraints before accepting.
Do not treat generic PASS output as sufficient if it does not exercise the final deliverable.
Do not treat dependency installation or environment inspection as final validation.

You must respond by calling exactly one structured tool:
- verifier_read_tool when you need more evidence.
- verifier_final_verdict when you are ready to make the binary judgement.

Do not write JSON in normal assistant text.
Do not wrap JSON in markdown.
Do not put the final answer in reasoning text.
Do not include prose outside tool calls.

If you need evidence, call verifier_read_tool.
If you are ready to decide, call verifier_final_verdict.
'''
TOOLS=['list_debug_files','read_debug_file','search_debug_file','search_commands','read_command','search_tool_calls','read_tool_call','search_transcript','list_repo_files','read_repo_file','search_repo','read_diff','search_diff']
LLM_TOOLS=[
    {'type':'function','function':{'name':'verifier_read_tool','description':'Request a read-only verifier tool to inspect debug artifacts, commands, tool calls, transcripts, diffs, or repo files.','parameters':{'type':'object','additionalProperties':False,'required':['tool','args','reason'],'properties':{'tool':{'type':'string','enum':TOOLS},'args':{'type':'object','additionalProperties':True},'reason':{'type':'string'}}}}},
    {'type':'function','function':{'name':'verifier_final_verdict','description':'Return the final binary verifier judgement.','parameters':{'type':'object','additionalProperties':False,'required':['result','verdict','confidence','recommendedAction','reason','requirementResults','successEvidence','failureEvidence','recoveredFailures','missingEvidence','riskFlags','uncertainty','toolsUsed'],'properties':{'result':{'type':'integer','enum':[0,1]},'verdict':{'type':'string','enum':['success','failure']},'confidence':{'type':'number','minimum':0,'maximum':1},'recommendedAction':{'type':'string','enum':['accept','reject','retry_same_model','retry_higher_model','run_more_tests','inspect_manually']},'reason':{'type':'string'},'requirementResults':{'type':'array','items':{'type':'object','additionalProperties':False,'required':['id','requirement','status','evidence','risks'],'properties':{'id':{'type':'string'},'requirement':{'type':'string'},'status':{'type':'string','enum':['satisfied','unsatisfied']},'evidence':{'type':'array','items':{'type':'string'}},'risks':{'type':'array','items':{'type':'string'}}}}},'successEvidence':{'type':'array','items':{'type':'string'}},'failureEvidence':{'type':'array','items':{'type':'string'}},'recoveredFailures':{'type':'array','items':{'type':'string'}},'missingEvidence':{'type':'array','items':{'type':'string'}},'riskFlags':{'type':'array','items':{'type':'string'}},'uncertainty':{'type':'object','additionalProperties':False,'required':['level','reasons'],'properties':{'level':{'type':'string','enum':['low','medium','high']},'reasons':{'type':'array','items':{'type':'string'}}}},'deliverableAssessment':{'type':'object','additionalProperties':False,'required':['requiredDeliverables','validatedDeliverables','missingDeliverables','weakValidationReasons'],'properties':{'requiredDeliverables':{'type':'array','items':{'type':'string'}},'validatedDeliverables':{'type':'array','items':{'type':'string'}},'missingDeliverables':{'type':'array','items':{'type':'string'}},'weakValidationReasons':{'type':'array','items':{'type':'string'}}}},'constraintAssessment':{'type':'object','additionalProperties':False,'required':['constraints','satisfiedConstraints','violatedConstraints','uncheckedConstraints'],'properties':{'constraints':{'type':'array','items':{'type':'string'}},'satisfiedConstraints':{'type':'array','items':{'type':'string'}},'violatedConstraints':{'type':'array','items':{'type':'string'}},'uncheckedConstraints':{'type':'array','items':{'type':'string'}}}},'toolsUsed':{'type':'array','items':{'type':'object','additionalProperties':False,'required':['tool','reason'],'properties':{'tool':{'type':'string'},'reason':{'type':'string'}}}}}}}}
]

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
def _chat_message(cfg,messages,timeout, trace=None, use_tools=True, force_tool=None):
    started=time.time(); raw=None; status="ok"; http_status=None
    try:
        payload={'model':cfg['model'],'messages':messages,'temperature':0}
        if use_tools:
            payload.update({'tools':LLM_TOOLS,'tool_choice':force_tool or 'auto'})
        r=httpx.post(cfg['baseUrl'].rstrip('/')+'/chat/completions',headers={'Authorization':f"Bearer {cfg['apiKey']}"},json=payload,timeout=timeout)
        http_status=getattr(r,'status_code',None); r.raise_for_status(); raw=r.json(); return raw['choices'][0]['message']
    except Exception:
        status='error'; raise
    finally:
        if trace is not None:
            trace.append_jsonl('llm_raw_responses.jsonl',{'index':getattr(trace,'llm_call_count',0),'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'durationMs':int((time.time()-started)*1000),'provider':'openai-compatible','baseUrl':cfg.get('baseUrl'),'model':cfg.get('model'),'status':status,'httpStatus':http_status,'usage':(raw or {}).get('usage') if isinstance(raw,dict) else None,'raw':raw})
            trace.llm_call_count=getattr(trace,'llm_call_count',0)+1
def _chat(cfg,messages,timeout, trace=None):
    return (_chat_message(cfg,messages,timeout,trace=trace,use_tools=False) or {}).get('content','')
def _json_object_from_mixed(s):
    if not s: raise VerifierSchemaError('empty response')
    dec=json.JSONDecoder()
    for i,ch in enumerate(s):
        if ch=='{':
            try: return dec.raw_decode(s[i:])[0]
            except Exception: pass
    raise VerifierSchemaError('no JSON object found')
def _parse(s):
    try: obj=json.loads(s)
    except Exception:
        obj=_json_object_from_mixed(s)
    if obj.get('type')=='tool_call':
        if obj.get('name')=='verifier_read_tool' and isinstance(obj.get('arguments'),dict): return {'type':'tool_call',**obj['arguments']}
        return obj
    if obj.get('type')=='final_verdict' and isinstance(obj.get('arguments'),dict): obj={'type':'final_verdict',**obj['arguments']}
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
def _raw_snapshot(verdict):
    keys=['result','verdict','confidence','recommendedAction','reason','deliverableAssessment','constraintAssessment','requirementResults','successEvidence','failureEvidence','recoveredFailures','missingEvidence','riskFlags','uncertainty','toolsUsed']
    return {k:verdict.get(k) for k in keys if k in verdict}

def _make_disagreement(kind, summary, evidence=None):
    return {'kind':kind,'summary':summary,'evidenceIds':[str((e or {}).get('id') or (e or {}).get('kind') or i) for i,e in enumerate(evidence or [])],'effect':'risk_flag_only'}

def calibrate(det, verdict, trace=None, cfg=None, timeout=30):
    raw=_raw_snapshot(verdict)
    cats=det.get('evidenceByCategory',{}) or {}
    validations=cats.get('finalEndToEndValidation',[])+cats.get('testValidation',[])+cats.get('serviceValidation',[])
    strong=[v for v in validations if isinstance(v,dict) and v.get('validationStrength')=='strong']
    active=cats.get('activeFailures',[]) or []
    risk_before=list(verdict.get('riskFlags') or [])
    action_before=verdict.get('recommendedAction')
    conf_before=float(verdict.get('confidence',0) or 0)
    rules=['schema_consistency','non_mutating_calibration']
    disagreements=[]
    notes=[]
    if verdict['result']==1 and active:
        disagreements.append(_make_disagreement('active_failure_disagreement','Deterministic collector found candidate failures, but the LLM judged the run successful.',active))
        verdict.setdefault('riskFlags',[]).append('Deterministic disagreement: evidence collector found unresolved-looking failures, but the LLM judged them non-blocking.')
        rules.append('add_deterministic_disagreement_risk')
    if verdict['result']==0 and (strong or cats.get('finalEndToEndValidation')):
        disagreements.append(_make_disagreement('validation_signal_disagreement','Deterministic collector found candidate validation signals, but the LLM judged the run unsuccessful.',strong or cats.get('finalEndToEndValidation',[])))
        verdict.setdefault('riskFlags',[]).append('Deterministic disagreement: evidence collector found validation-looking signals, but the LLM judged them insufficient for the task contract.')
        rules.append('add_deterministic_disagreement_risk')
    da=det.get('deliverableAssessment') or {}
    if verdict['result']==1 and da.get('requiredDeliverables') and not (cats.get('deliverableEvidence') or strong):
        disagreements.append(_make_disagreement('missing_deterministic_deliverable_label','Deterministic deliverable labels are missing despite an LLM success judgement.',[]))
        verdict.setdefault('riskFlags',[]).append('Deterministic deliverable evidence labels are missing; LLM success remains authoritative and should be audited if unexpected.')
    if verdict['result']==0 and cats.get('deliverableEvidence') and strong:
        disagreements.append(_make_disagreement('deliverable_validation_disagreement','Deterministic deliverable and validation signals exist, but the LLM judged the task contract unmet.',cats.get('deliverableEvidence',[])+strong))
        verdict.setdefault('riskFlags',[]).append('Deterministic deliverable and validation signals exist, but the LLM judged the task contract unmet.')
    if verdict['result']==1 and conf_before>.9:
        verdict['confidence']=.9; verdict.setdefault('riskFlags',[]).append('Success confidence capped at 0.9.'); rules.append('cap_success_confidence')
    verdict.setdefault('uncertainty', {'level':'medium','reasons':[]})
    if disagreements and verdict.get('recommendedAction')=='accept':
        verdict['recommendedAction']='inspect_manually'; rules.append('conservative_manual_inspection_action')
    final=finalize_verifier_result(raw, verdict)
    cal={'schemaVersion':'villani-ops-verifier-calibration-v2','resultMutationAllowed':False,'rawLlmVerdict':raw,'finalResult':{k:final.get(k) for k in ['result','verdict','confidence','recommendedAction','reason']},'resultChanged':False,'confidenceChanged':float(final.get('confidence',0) or 0)!=conf_before,'recommendedActionChanged':final.get('recommendedAction')!=action_before,'riskFlagsAdded':[x for x in final.get('riskFlags',[]) if x not in risk_before],'deterministicDisagreements':disagreements,'contradictions':[d['summary'] for d in disagreements],'uncertaintyNotes':notes,'rulesApplied':rules,'auditAdjudication':{'enabled':False,'note':'Result-changing adjudication is disabled; deterministic disagreements are audit/risk only.'}}
    if trace is not None: trace.write_json('calibration.json',cal)
    final['_calibration']=cal
    return final
def llm_result(run, det, workspace='.villani-ops', backend=None, base_url=None, model=None, timeout_seconds=180, max_tool_calls=12, max_tool_result_chars=12000, max_read_lines=160, trace=None):
    cfg=select_verifier_backend(workspace,backend,base_url,model); tools=VerifierTools(run,det.get('repoDir'),max_tool_result_chars,max_read_lines)
    packet=build_packet(run,det.get('repoDir'))
    messages=[{'role':'system','content':SYSTEM},{'role':'user','content':'Objective:\n'+str(run.objective)+'\nAvailable tools: '+', '.join(TOOLS)+'\n'+_schema_text()+'\nEvidence packet:\n'+json.dumps(packet,default=str)}]
    if trace is not None:
        trace.msg_count=getattr(trace,'msg_count',0)
        for m in messages:
            trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':m['role'],'name':None,'content':m['content'],'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(m['content']))}); trace.msg_count+=1
    used=[]; deadline=time.monotonic()+timeout_seconds; calls=0; content=''; native_supported=True; protocol='native_tool_calls'; protocol_warnings=[]; empty_retried=False; protocol_retried=False
    while True:
        if time.monotonic()>deadline: raise VerifierLlmError('tool loop timeout')
        force={'type':'function','function':{'name':'verifier_final_verdict'}} if calls>max_tool_calls else None
        try:
            msg=_chat_message(cfg,messages,max(1,deadline-time.monotonic()),trace=trace,use_tools=native_supported,force_tool=force)
        except Exception as e:
            if native_supported and any(x in str(e).lower() for x in ['tool','tools','tool_choice','function']):
                native_supported=False; protocol='legacy_json_fallback'; protocol_warnings.append('Backend rejected native tool calls; falling back to legacy JSON protocol.')
                if trace is not None: trace.append_jsonl('llm_protocol.jsonl',{'protocol':protocol,'warning':protocol_warnings[-1]})
                continue
            raise VerifierLlmError(f'HTTP failure: {e}')
        tc=msg.get('tool_calls') or []
        content=msg.get('content') or ''
        if trace is not None:
            trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':'assistant','name':None,'content':content,'tool_calls':tc or None,'reasoning_content':msg.get('reasoning_content'),'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(content))}); trace.msg_count+=1
            if msg.get('reasoning_content'): trace.append_jsonl('llm_reasoning_content.jsonl',{'content':msg.get('reasoning_content'),'chars':len(str(msg.get('reasoning_content')))})
        if tc:
            call=tc[0]; fn=(call.get('function') or {}); cname=fn.get('name'); raw_args=fn.get('arguments') or '{}'
            try: args=json.loads(raw_args) if isinstance(raw_args,str) else (raw_args or {})
            except Exception as e: raise VerifierSchemaError(f'invalid tool arguments: {e}')
            if cname=='verifier_read_tool':
                obj={'type':'tool_call',**args}
            elif cname=='verifier_final_verdict':
                obj={'type':'final_verdict',**args}
                try: obj=_parse(json.dumps(obj))
                except VerifierSchemaError as e:
                    if not protocol_retried:
                        protocol_retried=True; messages.append({'role':'user','content':'Your verifier_final_verdict arguments were invalid: '+str(e)+'. Return exactly one valid structured tool call.'}); continue
                    raise
            else:
                if not protocol_retried:
                    protocol_retried=True; messages.append({'role':'user','content':'Unknown verifier tool call. Return exactly one valid structured tool call: verifier_read_tool or verifier_final_verdict.'}); continue
                raise VerifierSchemaError('unknown verifier tool call: '+str(cname))
        else:
            if not content.strip():
                if msg.get('reasoning_content') and not protocol_retried:
                    protocol_retried=True; messages.append({'role':'user','content':'Return your action using the structured tool call. Do not put the answer in reasoning_content.'}); continue
                if native_supported and not empty_retried:
                    empty_retried=True; calls=max_tool_calls+1; messages.append({'role':'user','content':'Empty response received. Return the final judgement using verifier_final_verdict.'}); continue
                raise VerifierSchemaError('empty verifier response without tool call')
            try:
                obj=_parse(content); protocol='legacy_json_fallback'
                if trace is not None: trace.append_jsonl('llm_protocol.jsonl',{'protocol':'legacy_json_fallback'})
            except VerifierSchemaError:
                try: obj=_repair(cfg,content,max(1,deadline-time.monotonic()),trace=trace)
                except Exception as e: raise VerifierSchemaError(f'invalid JSON after repair: {e}')
        if obj.get('type')=='tool_call':
            if calls>=max_tool_calls:
                messages.append({'role':'user','content':'Maximum tool calls reached. Return the final judgement using verifier_final_verdict.'});
                calls=max_tool_calls+1; continue
            name=obj.get('tool'); args=obj.get('args') or {}; idx=calls; calls+=1; used.append({'tool':name,'reason':'LLM requested tool'})
            start=time.time(); status='ok'; err=None
            try: res=tools.dispatch(name,args)
            except Exception as e: res=json.dumps({'error':str(e)}); status='error'; err=str(e)
            if trace is not None:
                trace.append_jsonl('tool_calls.jsonl',{'index':idx,'llmTool':'verifier_read_tool' if native_supported else None,'verifierTool':name,'tool':name,'args':args,'startedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'completedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'durationMs':int((time.time()-start)*1000),'status':status,'resultChars':len(res),'truncated':len(res)>=max_tool_result_chars,'error':err,'reason':obj.get('reason') or 'LLM requested tool call.'})
                trace.append_jsonl('tool_observations.jsonl',{'toolCallIndex':idx,'tool':name,'observation':None,'observationText':res,'chars':len(res),'truncated':len(res)>=max_tool_result_chars})
            if native_supported and protocol=='native_tool_calls':
                tid=f'verifier-call-{idx}'
                messages.append({'role':'assistant','content':None,'tool_calls':[{'id':tid,'type':'function','function':{'name':'verifier_read_tool','arguments':json.dumps({'tool':name,'args':args,'reason':obj.get('reason') or ''})}}]})
                messages.append({'role':'tool','tool_call_id':tid,'content':res})
            else:
                messages.append({'role':'assistant','content':json.dumps(obj)})
                messages.append({'role':'user','content':'Tool result for '+str(name)+':\n'+res})
            if trace is not None:
                for m in messages[-2:]: trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':m['role'],'name':None,'content':m.get('content'),'tool_calls':m.get('tool_calls'),'tool_call_id':m.get('tool_call_id'),'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(m.get('content')))}); trace.msg_count+=1
            continue
        
        if trace is not None:
            trace.write_json('llm_final_verdict_raw.json',{'protocol':protocol,'rawText':content,'parsed':obj})
            parsed={'schemaVersion':'villani-ops-verifier-llm-verdict-v1',**{k:obj.get(k) for k in ['result','verdict','confidence','recommendedAction','reason','deliverableAssessment','constraintAssessment','requirementResults','successEvidence','failureEvidence','recoveredFailures','missingEvidence','riskFlags','uncertainty','toolsUsed']}}
            trace.write_json('llm_final_verdict_parsed.json',parsed)
        obj.setdefault('riskFlags',[]); obj['riskFlags']+=protocol_warnings
        obj=calibrate(det,obj,trace=trace,cfg=cfg,timeout=max(1,deadline-time.monotonic())); break
    det.update({'result':obj['result'],'verdict':obj['verdict'],'confidence':obj['confidence'],'recommendedAction':obj['recommendedAction'],'reason':obj['reason'],'resultSource':obj.get('resultSource'),'postProcessingChangedResult':obj.get('postProcessingChangedResult'),'deliverableAssessment':obj.get('deliverableAssessment',det.get('deliverableAssessment')),'constraintAssessment':obj.get('constraintAssessment',det.get('constraintAssessment')),'requirementResults':obj.get('requirementResults',det['requirementResults']),'successEvidence':obj.get('successEvidence',det['successEvidence']),'failureEvidence':obj.get('failureEvidence',det['failureEvidence']),'recoveredFailures':obj.get('recoveredFailures',det['recoveredFailures']),'missingEvidence':obj.get('missingEvidence',det['missingEvidence']),'riskFlags':obj.get('riskFlags',det['riskFlags']),'toolsUsed':used+obj.get('toolsUsed',[]),'llmRawVerdict':obj.get('llmRawVerdict',{}),'llmProtocol':protocol,'llmProtocolWarnings':protocol_warnings,'calibration':obj.get('_calibration',{}),'verifier':{'mode':'llm_tool_loop','backend':cfg['backend'],'model':cfg['model'],'baseUrl':cfg['baseUrl'],'promptVersion':PROMPT_VERSION}})
    return validate_final_result_consistency(det)

def finalize_verifier_result(raw_llm_verdict, processed_verdict, trace_info=None):
    final=processed_verdict
    flags=final.setdefault('riskFlags',[])
    if final.get('result')!=raw_llm_verdict.get('result') or final.get('verdict')!=raw_llm_verdict.get('verdict'):
        final['result']=raw_llm_verdict.get('result')
        final['verdict']=raw_llm_verdict.get('verdict')
        flags.append('Post-processing attempted to change the LLM verifier result. Restored raw LLM result.')
    final['llmRawVerdict']=raw_llm_verdict
    final['resultSource']='llm_verifier'
    final['postProcessingChangedResult']=False
    return validate_final_result_consistency(final)

def validate_final_result_consistency(result):
    expected={1:'success',0:'failure',None:'error'}.get(result.get('result'))
    flags=result.setdefault('riskFlags',[])
    if expected and result.get('verdict')!=expected:
        result['verdict']=expected; flags.append('Final consistency fixed result/verdict mismatch.')
    reason=(result.get('reason') or '').lower()
    if result.get('result')==1:
        if result.get('recommendedAction')=='reject': result['recommendedAction']='inspect_manually'; flags.append('Final consistency fixed success recommendedAction.')
        if any(p in reason for p in ['run failed','did not solve','unsatisfied','blocking failure']) and not any(p in reason for p in ['despite','although']):
            result['reason']='The verifier accepted the run based on deliverable-linked evidence; stale contradictory success/failure wording was replaced.'; flags.append('Final consistency replaced stale failure reason under success verdict.')
    elif result.get('result')==0:
        if result.get('recommendedAction')=='accept': result['recommendedAction']='inspect_manually'; flags.append('Final consistency fixed failure recommendedAction.')
        if any(p in reason for p in ['run succeeded','successfully solved','accepted because']) and not any(p in reason for p in ['not ', 'despite','although']):
            result['reason']='The verifier rejected the run based on unresolved blocking or constraint evidence; stale success wording was replaced.'; flags.append('Final consistency replaced stale success reason under failure verdict.')
    return result
