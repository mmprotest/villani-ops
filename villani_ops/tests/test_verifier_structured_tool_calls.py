from __future__ import annotations
import json, httpx, pytest
from pathlib import Path
from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.verifier.deterministic import deterministic_result
from villani_ops.verifier.load_debug_run import load_debug_run
from villani_ops.verifier.llm import llm_result
from villani_ops.verifier.trace import VerifierTraceWriter

FIX=Path(__file__).parent/'fixtures'/'verifier_success'

def _ws(tmp_path):
    s=FileStorage(tmp_path); s.init_workspace(); s.save_backends({'b':Backend(name='b',provider='local',base_url='http://127.0.0.1:1234/v1',model='m',roles=['review'],capability_score=1)})

def _verdict(**kw):
    d={'result':1,'verdict':'success','confidence':.84,'recommendedAction':'accept','reason':'verified','requirementResults':[],'successEvidence':['ok'],'failureEvidence':[],'recoveredFailures':[],'missingEvidence':[],'riskFlags':[],'uncertainty':{'level':'low','reasons':[]},'toolsUsed':[]}
    d.update(kw); return d

def _tc(name,args):
    return {'choices':[{'message':{'content':'ignored prose','tool_calls':[{'id':'c1','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}

def test_native_final_tool_call_wins_over_content_and_no_repair(monkeypatch,tmp_path):
    _ws(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    monkeypatch.setattr(httpx,'post',lambda *a,**k: type('R',(),{'raise_for_status':lambda s:None,'json':lambda s:_tc('verifier_final_verdict',_verdict())})())
    res=llm_result(run,det,workspace=str(tmp_path))
    assert res['result']==1 and res['llmProtocol']=='native_tool_calls'
    assert res['llmRawVerdict']['reason']=='verified'

def test_native_read_tool_then_final_records_outer_and_inner(monkeypatch,tmp_path):
    _ws(tmp_path/'ws'); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    trace=VerifierTraceWriter(tmp_path/'ws',FIX,tmp_path/'trace',True,'full'); trace.start({'x':1})
    calls=[]
    class R:
        def __init__(self,p): self.p=p
        def raise_for_status(self): pass
        def json(self):
            calls.append(self.p)
            if len(calls)==1: return _tc('verifier_read_tool',{'tool':'search_commands','args':{'query':'PASS','limit':2},'reason':'Need exact validation output.'})
            assert self.p['messages'][-1]['role']=='tool'
            assert 'PASS' in self.p['messages'][-1]['content']
            return _tc('verifier_final_verdict',_verdict(toolsUsed=[{'tool':'search_commands','reason':'Inspected validation output.'}]))
    monkeypatch.setattr(httpx,'post',lambda *a,**k: R(k['json']))
    res=llm_result(run,det,workspace=str(tmp_path/'ws'),trace=trace); trace.finish(res)
    rows=[json.loads(x) for x in (tmp_path/'trace'/'tool_calls.jsonl').read_text().splitlines()]
    assert res['llmProtocol']=='native_tool_calls'
    assert rows[0]['llmTool']=='verifier_read_tool' and rows[0]['verifierTool']=='search_commands'
    assert json.loads((tmp_path/'trace'/'verification_result.json').read_text())['llmProtocol']=='native_tool_calls'

def test_unknown_native_tool_no_hallucinated_verdict(monkeypatch,tmp_path):
    _ws(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    monkeypatch.setattr(httpx,'post',lambda *a,**k: type('R',(),{'raise_for_status':lambda s:None,'json':lambda s:_tc('bad_tool',{})})())
    with pytest.raises(Exception) as ei: llm_result(run,det,workspace=str(tmp_path))
    assert 'unknown verifier tool call' in str(ei.value)

def test_native_result_verdict_mismatch_rejected(monkeypatch,tmp_path):
    _ws(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    monkeypatch.setattr(httpx,'post',lambda *a,**k: type('R',(),{'raise_for_status':lambda s:None,'json':lambda s:_tc('verifier_final_verdict',_verdict(result=1,verdict='failure'))})())
    with pytest.raises(Exception) as ei: llm_result(run,det,workspace=str(tmp_path))
    assert 'result/verdict mismatch' in str(ei.value)

def test_empty_content_no_repair(monkeypatch,tmp_path):
    _ws(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    monkeypatch.setattr(httpx,'post',lambda *a,**k: type('R',(),{'raise_for_status':lambda s:None,'json':lambda s:{'choices':[{'message':{'content':''}}]}})())
    with pytest.raises(Exception) as ei: llm_result(run,det,workspace=str(tmp_path))
    assert 'empty verifier response' in str(ei.value)

def test_reasoning_content_traced_and_retried(monkeypatch,tmp_path):
    _ws(tmp_path/'ws'); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    trace=VerifierTraceWriter(tmp_path/'ws',FIX,tmp_path/'trace',True,'full'); trace.start({'x':1})
    n={'i':0}
    def post(*a,**k):
        n['i']+=1
        data={'choices':[{'message':{'content':'','reasoning_content':'hidden final'}}]} if n['i']==1 else _tc('verifier_final_verdict',_verdict())
        return type('R',(),{'raise_for_status':lambda s:None,'json':lambda s:data})()
    monkeypatch.setattr(httpx,'post',post)
    res=llm_result(run,det,workspace=str(tmp_path/'ws'),trace=trace)
    assert res['result']==1
    assert (tmp_path/'trace'/'llm_reasoning_content.jsonl').exists()

def test_backend_rejects_tools_falls_back_to_legacy(monkeypatch,tmp_path):
    _ws(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    n={'i':0}
    def post(*a,**k):
        n['i']+=1
        if 'tools' in k['json']: raise httpx.HTTPStatusError('tools rejected',request=None,response=None)
        return type('R',(),{'raise_for_status':lambda s:None,'json':lambda s:{'choices':[{'message':{'content':json.dumps({'type':'final_verdict',**_verdict()})}}]}})()
    monkeypatch.setattr(httpx,'post',post)
    res=llm_result(run,det,workspace=str(tmp_path))
    assert res['llmProtocol']=='legacy_json_fallback'
    assert res['llmProtocolWarnings']

def test_legacy_prose_plus_json_extracted(monkeypatch,tmp_path):
    _ws(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    text='Here is the result '+json.dumps({'type':'final_verdict',**_verdict()})
    monkeypatch.setattr(httpx,'post',lambda *a,**k: type('R',(),{'raise_for_status':lambda s:None,'json':lambda s:{'choices':[{'message':{'content':text}}]}})())
    res=llm_result(run,det,workspace=str(tmp_path))
    assert res['result']==1 and res['llmProtocol']=='legacy_json_fallback'
