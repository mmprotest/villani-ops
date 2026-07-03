from __future__ import annotations
import json, httpx, pytest
from pathlib import Path
from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.verifier.load_debug_run import load_debug_run
from villani_ops.verifier.deterministic import deterministic_result
from villani_ops.verifier.llm import llm_result, select_verifier_backend
from villani_ops.verifier.tools import VerifierTools
from villani_ops.verifier.errors import VerifierToolError

FIX=Path(__file__).parent/'fixtures'/'verifier_success'

def test_backend_selection_prefers_capability_and_localhost_dummy(tmp_path):
    s=FileStorage(tmp_path); s.init_workspace()
    s.save_backends({
        'weak': Backend(name='weak',provider='local',base_url='http://127.0.0.1:1234/v1',model='a',roles=['review'],capability_score=1,output_cost_per_million=0),
        'strong': Backend(name='strong',provider='local',base_url='http://127.0.0.1:1234/v1',model='b',roles=['coding'],capability_score=9,output_cost_per_million=99),
    })
    cfg=select_verifier_backend(str(tmp_path))
    assert cfg['backend']=='strong' and cfg['apiKey']=='dummy'

def test_read_debug_file_blocks_unsafe_paths():
    tools=VerifierTools(load_debug_run(FIX))
    with pytest.raises(VerifierToolError): tools.read_debug_file('/etc/passwd')
    with pytest.raises(VerifierToolError): tools.read_debug_file('../session_meta.json')

def test_tool_loop_calls_search_commands(monkeypatch, tmp_path):
    run=load_debug_run(FIX); det=deterministic_result(run, mode='llm_tool_loop')
    s=FileStorage(tmp_path); s.init_workspace(); s.save_backends({'b':Backend(name='b',provider='local',base_url='http://127.0.0.1:1234/v1',model='m',roles=['review'],capability_score=1)})
    calls=[]
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            if not calls:
                calls.append('first')
                return {'choices':[{'message':{'content':json.dumps({'type':'tool_call','tool':'search_commands','args':{'query':'PASS','limit':2}})}}]}
            assert 'Tool result for search_commands' in self.payload['messages'][-1]['content']
            return {'choices':[{'message':{'content':json.dumps({'type':'final_verdict','result':1,'verdict':'success','confidence':0.95,'recommendedAction':'accept','reason':'PASS evidence found','requirementResults':[],'successEvidence':['PASS evidence'],'failureEvidence':[],'recoveredFailures':[],'missingEvidence':[],'riskFlags':[],'toolsUsed':[]})}}]}
    def fake_post(*args,**kwargs):
        r=Resp(); r.payload=kwargs['json']; return r
    monkeypatch.setattr(httpx,'post',fake_post)
    res=llm_result(run,det,workspace=str(tmp_path))
    assert res['toolsUsed'][0]['tool']=='search_commands'
    assert res['llmRawVerdict']['verdict']=='success' and res['result']==1
    assert res['confidence']==0.9

def test_read_debug_file_blocks_symlink_escape(tmp_path):
    outside=tmp_path/'outside.txt'; outside.write_text('secret')
    d=tmp_path/'debug'; d.mkdir(); (d/'session_meta.json').write_text('{"objective":"x"}')
    (d/'commands.jsonl').write_text('') ; (d/'tool_calls.jsonl').write_text(''); (d/'patches.jsonl').write_text(''); (d/'model_responses.jsonl').write_text('')
    link=d/'link.txt'
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation unavailable')
    tools=VerifierTools(load_debug_run(d))
    with pytest.raises(VerifierToolError): tools.read_debug_file('link.txt')

def test_read_repo_file_blocks_symlink_escape(tmp_path):
    outside=tmp_path/'outside.txt'; outside.write_text('secret')
    repo=tmp_path/'repo'; repo.mkdir(); link=repo/'link.txt'
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation unavailable')
    tools=VerifierTools(load_debug_run(FIX), repo_dir=repo)
    with pytest.raises(VerifierToolError): tools.read_repo_file('link.txt')

def test_llm_http_failure_is_error(monkeypatch, tmp_path):
    run=load_debug_run(FIX); det=deterministic_result(run, mode='llm_tool_loop')
    monkeypatch.setenv('VILLANI_OPS_VERIFIER_MODEL','m')
    def boom(*a,**k): raise httpx.ConnectError('nope')
    monkeypatch.setattr(httpx,'post',boom)
    with pytest.raises(Exception) as ei: llm_result(run,det,workspace=str(tmp_path))
    assert 'HTTP failure' in str(ei.value)

def test_llm_invalid_json_after_repair_is_error(monkeypatch, tmp_path):
    run=load_debug_run(FIX); det=deterministic_result(run, mode='llm_tool_loop')
    s=FileStorage(tmp_path); s.init_workspace(); s.save_backends({'b':Backend(name='b',provider='local',base_url='http://127.0.0.1:1234/v1',model='m',roles=['review'],capability_score=1)})
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'choices':[{'message':{'content':'not json'}}]}
    monkeypatch.setattr(httpx,'post',lambda *a,**k: Resp())
    with pytest.raises(Exception) as ei: llm_result(run,det,workspace=str(tmp_path))
    assert 'invalid JSON after repair' in str(ei.value)

def test_llm_binary_schema_accepts_and_rejects_unclear():
    from villani_ops.verifier.llm import _parse
    from villani_ops.verifier.errors import VerifierSchemaError
    assert _parse(json.dumps({'type':'final_verdict','result':1,'verdict':'success','confidence':.8,'recommendedAction':'accept','reason':'ok'}))['result']==1
    assert _parse(json.dumps({'type':'final_verdict','result':0,'verdict':'failure','confidence':.7,'recommendedAction':'reject','reason':'bad'}))['result']==0
    with pytest.raises(VerifierSchemaError):
        _parse(json.dumps({'type':'final_verdict','result':None,'verdict':'unclear','confidence':.5,'recommendedAction':'inspect_manually','reason':'no'}))
    with pytest.raises(VerifierSchemaError):
        _parse(json.dumps({'type':'final_verdict','result':1,'verdict':'failure','confidence':.5,'recommendedAction':'inspect_manually','reason':'mismatch'}))
