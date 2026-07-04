from __future__ import annotations
import json, os, time, re, httpx
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

Before returning result 1, identify the task contract:
- required outputs
- required modifications
- required file modifications
- required behavior
- required entrypoints
- required downstream behavior
- required services/installability
- required services or installability
- required performance/quality constraints
- required performance or quality constraints
- required generated artifacts
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
- validation tests only setup, input artifacts, or exploratory code
- downstream consumer behavior is required but not shown
- session-local environment changes are the only installability evidence
- performance is required but only functional correctness is shown
- negative constraints or allowed-edit constraints are unchecked or violated
- generated output file content is not inspected when content matters
- the final deliverable is not verified
- the validation tests a local/exploratory substitute instead of the actual deliverable
- required downstream behavior is not shown
- required performance/quality constraints
- required performance or quality constraints are not demonstrated
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
def extract_response_text(message: dict) -> str:
    content=message.get('content') or ''
    if isinstance(content,list): content=''.join((x.get('text') if isinstance(x,dict) else str(x)) for x in content)
    content=str(content or '')
    for k in ['reasoning_content','reasoning','text','output_text']:
        v=message.get(k)
        if not content and v: return str(v)
    return content

def normalize_native_tool_calls(message: dict):
    out=[]
    for tc in message.get('tool_calls') or []:
        fn=(tc or {}).get('function') or {}; name=fn.get('name'); args=fn.get('arguments') or {}
        if isinstance(args,str):
            try: args=json.loads(args or '{}')
            except Exception: args={}
        if name: out.append({'type':'tool_call','tool':name,'args':args if isinstance(args,dict) else {}})
    return out

def _balanced_json_candidates(text):
    text=text or ''; starts=[i for i,ch in enumerate(text) if ch=='{']
    for st in starts:
        depth=0; instr=False; esc=False
        for i in range(st,len(text)):
            ch=text[i]
            if instr:
                if esc: esc=False
                elif ch=='\\': esc=True
                elif ch=='"': instr=False
            else:
                if ch=='"': instr=True
                elif ch=='{': depth+=1
                elif ch=='}':
                    depth-=1
                    if depth==0:
                        yield text[st:i+1]; break

def extract_first_json_object(text: str) -> dict:
    cleaned=re.sub(r'^```(?:json|text)?\s*|```$','',text.strip(),flags=re.I|re.M)
    last=None
    for cand in _balanced_json_candidates(cleaned):
        try: obj=json.loads(cand)
        except Exception as e: last=e; continue
        if isinstance(obj,dict) and (obj.get('type') in {'tool_call','final_verdict'} or (obj.get('type') is None and ('result' in obj or obj.get('verdict') in {'success','failure'}))):
            return obj
    raise VerifierSchemaError(str(last or 'no protocol JSON object found'))

def _chat(cfg,messages,timeout, trace=None):
    started=time.time(); raw=None; status="ok"; http_status=None; info={}
    try:
        r=httpx.post(cfg['baseUrl'].rstrip('/')+'/chat/completions',headers={'Authorization':f"Bearer {cfg['apiKey']}"},json={'model':cfg['model'],'messages':messages,'temperature':0},timeout=timeout)
        http_status=getattr(r,'status_code',None); r.raise_for_status(); raw=r.json(); msg=raw['choices'][0]['message']
        extracted=extract_response_text(msg); parsed_preview=None; parse_status='native_tool_call' if msg.get('tool_calls') else 'not_parsed'; parse_error=None
        if extracted:
            try:
                parsed_preview=extract_first_json_object(extracted); parse_status='parsed'
            except Exception as e:
                parse_status='parse_failed'; parse_error=str(e)
        info={'content':msg.get('content') or '', 'reasoning_content':msg.get('reasoning_content'), 'nativeToolCalls':msg.get('tool_calls') or [], 'extractedText':extracted, 'extractedJson':parsed_preview, 'parseStatus':parse_status, 'parseError':parse_error, 'toolCalls':normalize_native_tool_calls(msg)}
        return info
    except Exception:
        status='error'; raise
    finally:
        if trace is not None:
            trace.append_jsonl('llm_raw_responses.jsonl',{'index':getattr(trace,'llm_call_count',0),'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'durationMs':int((time.time()-started)*1000),'provider':'openai-compatible','baseUrl':cfg.get('baseUrl'),'model':cfg.get('model'),'status':status,'httpStatus':http_status,'usage':(raw or {}).get('usage') if isinstance(raw,dict) else None,**info})
            trace.llm_call_count=getattr(trace,'llm_call_count',0)+1
def _parse(s):
    try: obj=s if isinstance(s,dict) else extract_first_json_object(s)
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
    if not str(bad or '').strip(): raise VerifierSchemaError('empty response cannot be repaired')
    msg=[{'role':'system','content':'Return exactly one JSON object only. No markdown fences. No prose.'},{'role':'user','content':'Repair this non-empty malformed verifier response to the required schema. '+_schema_text()+'\nMalformed response:\n'+str(bad)}]
    if trace is not None:
        for m in msg: trace.append_jsonl('llm_messages.jsonl',{'index':getattr(trace,'msg_count',0),'role':m['role'],'name':None,'content':m['content'],'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(m['content']))}); trace.msg_count=getattr(trace,'msg_count',0)+1
    resp=_chat(cfg,msg,timeout,trace=trace); content=resp.get('extractedText','')
    if trace is not None:
        trace.append_jsonl('llm_messages.jsonl',{'index':getattr(trace,'msg_count',0),'role':'assistant','name':None,'content':content,'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(content))}); trace.msg_count=getattr(trace,'msg_count',0)+1
    return _parse(content)


def _corpus(packet): return json.dumps(packet,default=str).lower()
def detect_contract_risks(objective: str, packet: dict, deliverable_spec: dict|None=None) -> list[dict]:
    text=((objective or '')+'\n'+_corpus(packet)); spec_text=json.dumps(deliverable_spec or packet.get('deliverableSpec') or {},default=str).lower(); objective_text=(objective or '').lower(); risks=[]
    def add(id,kind,reason,req):
        if not any(r['kind']==kind for r in risks): risks.append({'id':id,'kind':kind,'reason':reason,'requiredInspection':req})
    if re.search(r'\b(performance|faster|runtime|time|timing|median|speed|speedup|optimize|optimized|efficient|within|threshold|benchmark|golden|reference|seconds|sec/call|elapsed)\b',text): add('risk-performance','performance','Objective/evidence includes runtime/performance requirement.',['search_commands:timing','read_command:benchmark'])
    if re.search(r'\b(install|available in path|\bpath\b|pip install|index-url|client|import package|from package import|server|service|localhost|port|curl|http|https)\b',text): add('risk-downstream','downstream_consumer','Objective/evidence requires installability/service/downstream consumer behavior.',['search_commands:install/client','read_command:downstream'])
    if re.search(r'\b(\.ics|\.json|\.csv|\.txt|\.html|\.xml|pdf|report|write|generate|create file|save|produce|output)\b',objective_text+'\n'+spec_text): add('risk-generated-output','generated_output','Objective/evidence mentions generated output artifacts.',['search_tool_calls:filename','read_tool_call:write'])
    if re.search(r'\b(only edit|do not edit|must not|do not modify|only replace|allowed|forbidden|no warnings|no errors|without warnings|unchanged)\b',text): add('risk-constraints','allowed_edit','Objective includes allowed-edit or negative constraints.',['search_diff','read_diff'])
    checks=packet.get('deterministicChecks') or {}
    if checks.get('activeFailureCount') or checks.get('recoveredFailureCount'): add('risk-earlier-failures','earlier_failures_conflict','Evidence contains candidate failures around success evidence.',['search_commands:error/fail','read_command:failure'])
    return risks

def _tool_texts(tools_used):
    return ' '.join(json.dumps(t,default=str).lower() for t in tools_used or [])
def needs_forced_inspection_before_accept(raw_verdict, contract_risks, tools_used, packet):
    if raw_verdict.get('result')!=1: return None
    seen=_tool_texts(tools_used); corpus=_corpus(packet)
    for r in contract_risks:
        k=r['kind']
        ok=False; msg=''
        if k=='performance': ok=bool(re.search(r'timing|performance|median|runtime|speedup|reference|golden|benchmark',seen)) or bool(re.search(r'command\[|exitcode',corpus) and re.search(r'benchmark|median|deployment completed in \d+s|elapsed|seconds|sec/call|speedup|threshold',corpus)); msg='You are about to accept a run with a performance/runtime requirement. Before final_verdict, inspect exact timing or benchmark evidence using tools. Look for baseline/reference/golden comparison, repeated/median timing, threshold, and whether functional correctness alone is insufficient.'
        elif k in {'downstream_consumer','installability','service_access'}: ok=bool(re.search(r'pip install|index-url|path|which|import|client|service|endpoint|curl|fresh|localhost|port',seen)) or bool(re.search(r'command\[|exitcode',corpus) and re.search(r'git clone|git push|curl .*localhost|pip install|client|fresh',corpus)); msg='You are about to accept a run with installability/service/downstream-consumer requirements. Use tools to inspect exact evidence of a downstream consumer command succeeding. Distinguish server/setup evidence from an actual client/install/import/run command. Distinguish session-local environment exports from persistent/default availability.'
        elif k=='generated_output': ok=bool(re.search(r'read_tool_call|search_tool_calls|read_repo_file|write|output|artifact|file',seen)); msg='You are about to accept a generated-output task. Use tools to inspect the generated artifact or Write tool content. Confirm the artifact exists and has structure/content relevant to the objective before final_verdict.'
        elif k in {'allowed_edit','negative_constraint','file_diff'}: ok=bool(re.search(r'search_diff|read_diff|patch|diff|constraint|changed',seen)); msg='You are about to accept a task with negative or allowed-edit constraints. Use tools to inspect diffs, changed files, and any allowed-replacement/constraint source. Output success alone is insufficient if forbidden changes or warnings remain.'
        elif k=='earlier_failures_conflict': ok=bool(re.search(r'fail|error|read_command|search_commands|recovery|success|pass',seen)); msg='The evidence contains earlier failures and later success claims. Use tools to inspect whether the failures were actually recovered or remain material before accepting.'
        if not ok: return {'risk':r,'message':msg}
    return None

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
    contract_risks=detect_contract_risks(run.objective,packet,packet.get('deliverableSpec') or {})
    packet['contractRisks']=contract_risks
    if trace is not None: trace.write_json('contract_risks.json',contract_risks)
    messages=[{'role':'system','content':SYSTEM},{'role':'user','content':'Objective:\n'+str(run.objective)+'\nAvailable tools: '+', '.join(TOOLS)+'\n'+_schema_text()+'\nEvidence packet:\n'+json.dumps(packet,default=str)}]
    if trace is not None:
        trace.msg_count=getattr(trace,'msg_count',0)
        for m in messages:
            trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':m['role'],'name':None,'content':m['content'],'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(m['content']))}); trace.msg_count+=1
    used=[]; forced=[]; deadline=time.monotonic()+timeout_seconds; calls=0; content=''; empty_retry=False
    while True:
        if time.monotonic()>deadline: raise VerifierLlmError('tool loop timeout')
        try: resp=_chat(cfg,messages,max(1,deadline-time.monotonic()),trace=trace)
        except Exception as e:
            raise VerifierLlmError(f'HTTP failure: {e}')
        native=list(resp.get('toolCalls') or [])
        content=resp.get('extractedText','') or ''
        if native:
            obj=native.pop(0)
            for extra in reversed(native): messages.append({'role':'assistant','content':json.dumps(extra)})
        elif not content.strip():
            if not empty_retry:
                empty_retry=True
                msg='Your previous response was empty. Return exactly one JSON object only: either a tool_call or final_verdict matching the schema. No prose.'
                messages.append({'role':'user','content':msg})
                if trace is not None:
                    trace.append_jsonl('errors.jsonl',{'kind':'empty_llm_response','message':'empty response; strict retry issued'})
                    trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':'user','name':None,'content':msg,'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(msg)}); trace.msg_count+=1
                continue
            raise VerifierLlmError('empty verifier response after strict retry')
        else:
            try: obj=_parse(content)
            except VerifierSchemaError as pe:
                if trace is not None: trace.append_jsonl('errors.jsonl',{'kind':'parse_failure','message':str(pe),'contentPreview':content[:1000]})
                try: obj=_repair(cfg,content,max(1,deadline-time.monotonic()),trace=trace)
                except Exception as e: raise VerifierSchemaError(f'invalid JSON after repair: {e}')
        if obj.get('type')=='tool_call':
            if calls>=max_tool_calls:
                messages.append({'role':'user','content':'Maximum tool calls reached. Return final_verdict JSON using evidence gathered so far.'});
                max_tool_calls=-1; continue
            name=obj.get('tool'); args=obj.get('args') or {}; idx=calls; calls+=1; used.append({'tool':name,'args':args,'reason':'LLM requested tool'})
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
        
        remaining_risks=[r for r in contract_risks if r.get('id') not in {((f.get('risk') or {}).get('id')) for f in forced}]
        forced_inspection=needs_forced_inspection_before_accept(obj,remaining_risks,used,packet)
        if forced_inspection and calls<max_tool_calls:
            forced.append({**forced_inspection,'applied':True,'toolsUsed':used[:]})
            if trace is not None:
                trace.append_jsonl('forced_inspections.jsonl',forced[-1])
            messages.append({'role':'user','content':forced_inspection['message']})
            if trace is not None:
                trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':'user','name':None,'content':forced_inspection['message'],'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(forced_inspection['message'])}); trace.msg_count+=1
            continue
        elif forced_inspection:
            forced.append({**forced_inspection,'applied':False,'toolLimitReached':True,'toolsUsed':used[:]})
            obj.setdefault('riskFlags',[]).append('High-risk contract accepted without required inspection because tool limit was reached; LLM result remains authoritative.')
            if trace is not None: trace.append_jsonl('forced_inspections.jsonl',forced[-1])
        if trace is not None:
            trace.append_jsonl('llm_messages.jsonl',{'index':trace.msg_count,'role':'assistant','name':None,'content':content,'createdAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'chars':len(str(content))}); trace.msg_count+=1
            trace.write_json('llm_final_verdict_raw.json',{'rawText':content,'parsed':obj})
            parsed={'schemaVersion':'villani-ops-verifier-llm-verdict-v1',**{k:obj.get(k) for k in ['result','verdict','confidence','recommendedAction','reason','deliverableAssessment','constraintAssessment','requirementResults','successEvidence','failureEvidence','recoveredFailures','missingEvidence','riskFlags','uncertainty','toolsUsed']}}
            trace.write_json('llm_final_verdict_parsed.json',parsed)
        obj=calibrate(det,obj,trace=trace,cfg=cfg,timeout=max(1,deadline-time.monotonic())); break
    det.update({'result':obj['result'],'verdict':obj['verdict'],'confidence':obj['confidence'],'recommendedAction':obj['recommendedAction'],'reason':obj['reason'],'resultSource':obj.get('resultSource'),'postProcessingChangedResult':obj.get('postProcessingChangedResult'),'deliverableAssessment':obj.get('deliverableAssessment',det.get('deliverableAssessment')),'constraintAssessment':obj.get('constraintAssessment',det.get('constraintAssessment')),'requirementResults':obj.get('requirementResults',det['requirementResults']),'successEvidence':obj.get('successEvidence',det['successEvidence']),'failureEvidence':obj.get('failureEvidence',det['failureEvidence']),'recoveredFailures':obj.get('recoveredFailures',det['recoveredFailures']),'missingEvidence':obj.get('missingEvidence',det['missingEvidence']),'riskFlags':obj.get('riskFlags',det['riskFlags']),'toolsUsed':used+obj.get('toolsUsed',[]),'contractRisks':contract_risks,'forcedInspections':forced,'llmRawVerdict':obj.get('llmRawVerdict',{}),'calibration':obj.get('_calibration',{}),'verifier':{'mode':'llm_tool_loop','backend':cfg['backend'],'model':cfg['model'],'baseUrl':cfg['baseUrl'],'promptVersion':PROMPT_VERSION}})
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
