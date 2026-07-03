from __future__ import annotations
import json, os, httpx
from .deterministic import build_packet
PROMPT='''You are a verifier for Villani Code runs. Judge using only debug artifacts. Do not trust final answer unless supported. Earlier failures may recover. Prefer unclear over success. Return only valid JSON.'''
def _packet(run, det):
    return {'schemaVersion':'villani-ops-verifier-packet-v1','run':{'debugDir':run.debugDir,'runId':run.runId,'model':run.model,'provider':run.provider,'status':run.status,'durationMs':run.durationMs},'objective':run.objective,'requirements':det['requirementResults'],'deterministicChecks':det['deterministicChecks'],'evidence':{'successSignals':det['successEvidence'][:20],'failureSignals':det['failureEvidence'][:20],'recoveredFailures':det['recoveredFailures'][:20],'risks':det['riskFlags'][:20],'missingEvidence':det['missingEvidence'][:20]},'finalAnswer':(run.modelResponses[-1].text[:4000] if run.modelResponses and run.modelResponses[-1].text else None),'parseWarnings':run.parseWarnings,'missingArtifacts':run.missingArtifacts}
def llm_result(run, det, base_url=None, model=None):
    base_url=base_url or os.getenv('VILLANI_OPS_VERIFIER_BASE_URL') or 'http://127.0.0.1:1234/v1'; model=model or os.getenv('VILLANI_OPS_VERIFIER_MODEL')
    api_key=os.getenv('VILLANI_OPS_VERIFIER_API_KEY') or os.getenv('OPENAI_API_KEY') or 'dummy'
    if not model: det['riskFlags'].append({'kind':'risk','source':'derived','confidence':'low','text':'LLM verifier skipped: no model configured.'}); return det
    try:
        r=httpx.post(base_url.rstrip('/')+'/chat/completions',headers={'Authorization':f'Bearer {api_key}'},json={'model':model,'messages':[{'role':'system','content':PROMPT},{'role':'user','content':'Return JSON for this packet:\n'+json.dumps(_packet(run,det))}],'temperature':0},timeout=30)
        r.raise_for_status(); content=r.json()['choices'][0]['message']['content']; obj=json.loads(content)
        if obj.get('verdict') not in {'success','failure','unclear'}: raise ValueError('bad verdict')
        det.update({'verdict':obj['verdict'],'confidence':min(float(obj.get('confidence',det['confidence'])),.9),'recommendedAction':obj.get('recommendedAction',det['recommendedAction']),'reason':obj.get('reason',det['reason'])})
        det['verifier']={'mode':'hybrid','model':model,'baseUrl':base_url,'promptVersion':'villani-ops-verifier-v1'}
    except Exception as e:
        det['riskFlags'].append({'kind':'risk','source':'derived','confidence':'medium','text':f'LLM verifier failed; deterministic result used: {e}'})
    return det
