from __future__ import annotations
import json, os, time, httpx
from urllib.parse import urlparse
from villani_ops.storage.files import FileStorage
from .deterministic import build_packet, PROMPT_VERSION
from .tools import VerifierTools
from .errors import *
ROLES={'review','selection','classification','policy','coding'}
SYSTEM='''You are the mandatory verifier for Villani Code runs inside Villani Ops.
You are not a coding agent. You are not a repair agent.
You judge whether the run likely solved the user's objective using Villani Code debug artifacts and optional repo evidence.
You receive an initial evidence packet. The packet may be incomplete, noisy, or wrong. The deterministic evidence extractor is not authoritative.
You have read-only tools for inspecting debug files, command records, tool calls, transcripts, diffs, and repo files.
Use tools when material requirements are unclear, success evidence is weak, failures may be recovered, claims unsupported, evidence contradictory, file contents matter, or command pass/fail needs confirmation.
Do not use tools aimlessly. Do not trust the agent's final answer unless supported by artifacts. Earlier failures do not imply failure if later validation shows recovery.
A zero exit code does not prove success if output contains failure text. A non-zero exit code does not prove final failure if later validation resolves it.
Visible validation passing is strong evidence but not proof. Return success only when every material requirement is satisfied or strongly supported.
Return failure when active blocking evidence shows the task was not solved. Return unclear when evidence is incomplete/noisy/contradictory/insufficient. False accepts are worse than false rejects.
Return either {"type":"tool_call","tool":"search_commands","args":{...}} or final verdict JSON with type final_verdict. Return only valid JSON for the final verdict.'''
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
def _chat(cfg,messages,timeout):
    r=httpx.post(cfg['baseUrl'].rstrip('/')+'/chat/completions',headers={'Authorization':f"Bearer {cfg['apiKey']}"},json={'model':cfg['model'],'messages':messages,'temperature':0},timeout=timeout)
    r.raise_for_status(); return r.json()['choices'][0]['message'].get('content','')
def _parse(s):
    try: obj=json.loads(s)
    except Exception as e: raise VerifierSchemaError(str(e))
    if obj.get('type')=='tool_call': return obj
    if obj.get('type') is None and obj.get('verdict') in {'success','failure','unclear'}: obj['type']='final_verdict'
    if obj.get('type')!='final_verdict' or obj.get('verdict') not in {'success','failure','unclear'}: raise VerifierSchemaError('invalid final verdict schema')
    obj.setdefault('confidence',0.0); obj.setdefault('recommendedAction','inspect_manually')
    for k in ['reason','requirementResults','successEvidence','failureEvidence','recoveredFailures','missingEvidence','riskFlags','toolsUsed']: obj.setdefault(k, [] if k!='reason' else '')
    return obj
def _repair(cfg,bad,timeout):
    msg=[{'role':'system','content':'Return only valid JSON. No markdown fences.'},{'role':'user','content':'Repair this invalid verifier response to required final_verdict schema. Previous invalid response:\n'+bad}]
    return _parse(_chat(cfg,msg,timeout))
def calibrate(det, verdict):
    raw={'verdict':verdict['verdict'],'confidence':float(verdict.get('confidence',0))}; changed=False
    validations=det['evidenceByCategory']['finalEndToEndValidation']+det['evidenceByCategory']['testValidation']+det['evidenceByCategory']['serviceValidation']
    if verdict['verdict']=='success':
        if not validations or any(r.get('status')=='unsatisfied' for r in verdict.get('requirementResults',[])) or det['evidenceByCategory']['activeFailures']:
            verdict['verdict']='unclear'; verdict['recommendedAction']='inspect_manually'; changed=True
        verdict['confidence']=min(float(verdict.get('confidence',0)),.9)
    elif verdict['verdict']=='failure' and validations and det['evidenceByCategory']['recoveredFailures']:
        verdict['verdict']='unclear'; verdict['recommendedAction']='inspect_manually'; changed=True
    if changed: verdict.setdefault('riskFlags',[]).append('Calibration changed the LLM verdict based on deterministic evidence checks.')
    verdict['llmRawVerdict']=raw; return verdict
def llm_result(run, det, workspace='.villani-ops', backend=None, base_url=None, model=None, timeout_seconds=180, max_tool_calls=12, max_tool_result_chars=12000, max_read_lines=160):
    legacy_optional = det.get('verifier',{}).get('mode') == 'deterministic' and not backend and not base_url and not model
    cfg=select_verifier_backend(workspace,backend,base_url,model); tools=VerifierTools(run,det.get('repoDir'),max_tool_result_chars,max_read_lines)
    packet=build_packet(run,det.get('repoDir'))
    messages=[{'role':'system','content':SYSTEM},{'role':'user','content':'Objective:\n'+str(run.objective)+'\nAvailable tools: '+', '.join(TOOLS)+'\nFinal schema: final_verdict JSON.\nEvidence packet:\n'+json.dumps(packet,default=str)}]
    used=[]; deadline=time.monotonic()+timeout_seconds; calls=0; content=''
    while True:
        if time.monotonic()>deadline: raise VerifierLlmError('tool loop timeout')
        try: content=_chat(cfg,messages,max(1,deadline-time.monotonic()))
        except Exception as e:
            if legacy_optional:
                det.setdefault('riskFlags',[]).append({'kind':'risk','source':'derived','confidence':'medium','text':f'LLM verifier failed; deterministic result used: {e}'})
                return det
            raise VerifierLlmError(f'HTTP failure: {e}')
        try: obj=_parse(content)
        except VerifierSchemaError:
            try: obj=_repair(cfg,content,max(1,deadline-time.monotonic()))
            except Exception as e: raise VerifierSchemaError(f'invalid JSON after repair: {e}')
        if obj.get('type')=='tool_call':
            if calls>=max_tool_calls:
                messages.append({'role':'user','content':'Maximum tool calls reached. Return final_verdict JSON using evidence gathered so far.'});
                max_tool_calls=-1; continue
            name=obj.get('tool'); args=obj.get('args') or {}; calls+=1; used.append({'tool':name,'reason':'LLM requested tool'})
            try: res=tools.dispatch(name,args)
            except Exception as e: res=json.dumps({'error':str(e)})
            messages.append({'role':'assistant','content':json.dumps(obj)})
            messages.append({'role':'user','content':'Tool result for '+str(name)+':\n'+res})
            continue
        obj=calibrate(det,obj); break
    det.update({'verdict':obj['verdict'],'confidence':obj['confidence'],'recommendedAction':obj['recommendedAction'],'reason':obj['reason'],'requirementResults':obj.get('requirementResults',det['requirementResults']),'successEvidence':obj.get('successEvidence',det['successEvidence']),'failureEvidence':obj.get('failureEvidence',det['failureEvidence']),'recoveredFailures':obj.get('recoveredFailures',det['recoveredFailures']),'missingEvidence':obj.get('missingEvidence',det['missingEvidence']),'riskFlags':obj.get('riskFlags',det['riskFlags']),'toolsUsed':used+obj.get('toolsUsed',[]),'llmRawVerdict':obj.get('llmRawVerdict',{}),'verifier':{'mode':'llm_tool_loop','backend':cfg['backend'],'model':cfg['model'],'baseUrl':cfg['baseUrl'],'promptVersion':PROMPT_VERSION}})
    return det
