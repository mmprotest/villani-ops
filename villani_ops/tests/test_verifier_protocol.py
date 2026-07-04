from __future__ import annotations
import json, httpx, pytest
from pathlib import Path
from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.verifier.load_debug_run import load_debug_run
from villani_ops.verifier.deterministic import deterministic_result
from villani_ops.verifier.llm import _parse, extract_first_json_object, llm_result
from villani_ops.verifier.errors import VerifierLlmError, VerifierSchemaError

FIX=Path(__file__).parent/'fixtures'/'verifier_success'

def test_mixed_and_fenced_protocol_json_parse():
    assert _parse('I need this {"type":"tool_call","tool":"search_commands","args":{"query":"PASS"}}')['tool']=='search_commands'
    assert _parse('```json\n{"type":"final_verdict","result":1,"verdict":"success"}\n```')['result']==1
    assert extract_first_json_object('{"x":1} then {"type":"final_verdict","result":0,"verdict":"failure"}')['result']==0

def _workspace(tmp_path):
    s=FileStorage(tmp_path); s.init_workspace(); s.save_backends({'b':Backend(name='b',provider='local',base_url='http://127.0.0.1:1234/v1',model='m',roles=['review'],capability_score=1)})

def test_native_tool_call_executes(monkeypatch,tmp_path):
    _workspace(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop'); calls=[]
    class Resp:
        def __init__(self,payload): self.payload=payload
        def raise_for_status(self): pass
        def json(self):
            if not calls:
                calls.append(1); return {'choices':[{'message':{'content':'','tool_calls':[{'function':{'name':'search_commands','arguments':json.dumps({'query':'PASS'})}}]}}]}
            return {'choices':[{'message':{'content':json.dumps({'type':'final_verdict','result':1,'verdict':'success','reason':'ok'})}}]}
    monkeypatch.setattr(httpx,'post',lambda *a,**k: Resp(k['json']))
    out=llm_result(run,det,workspace=str(tmp_path))
    assert out['toolsUsed'][0]['tool']=='search_commands' and out['result']==1

def test_reasoning_content_and_empty_retry(monkeypatch,tmp_path):
    _workspace(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop'); seen=[]
    class Resp:
        def __init__(self,payload): self.payload=payload
        def raise_for_status(self): pass
        def json(self):
            seen.append(self.payload['messages'][-1]['content'])
            if len(seen)==1: return {'choices':[{'message':{'content':'','reasoning_content':json.dumps({'type':'final_verdict','result':0,'verdict':'failure','reason':'x'})}}]}
            return {'choices':[{'message':{'content':''}}]}
    monkeypatch.setattr(httpx,'post',lambda *a,**k: Resp(k['json']))
    assert llm_result(run,det,workspace=str(tmp_path))['result']==0

def test_empty_after_strict_retry_errors_not_repair(monkeypatch,tmp_path):
    _workspace(tmp_path); run=load_debug_run(FIX); det=deterministic_result(run,mode='llm_tool_loop')
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'choices':[{'message':{'content':''}}]}
    monkeypatch.setattr(httpx,'post',lambda *a,**k: Resp())
    with pytest.raises(VerifierLlmError): llm_result(run,det,workspace=str(tmp_path))
