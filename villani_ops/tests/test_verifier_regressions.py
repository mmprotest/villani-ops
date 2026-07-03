import json
from pathlib import Path

import httpx

from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.verifier.deterministic import build_packet, deterministic_result
from villani_ops.verifier.llm import calibrate, llm_result
from villani_ops.verifier.load_debug_run import load_debug_run
from villani_ops.verifier.tools import VerifierTools
from villani_ops.verifier.errors import VerifierToolError
import pytest


def _debug(tmp_path, objective, commands=None, tools=None, final=None, patches=True):
    d=tmp_path/'debug'; d.mkdir()
    (d/'session_meta.json').write_text(json.dumps({'objective':objective,'run_id':'r'}))
    if commands is not None:
        (d/'commands.jsonl').write_text('\n'.join(json.dumps(x) for x in commands))
    if tools is not None:
        (d/'tool_calls.jsonl').write_text('\n'.join(json.dumps(x) for x in tools))
    if patches:
        (d/'patches.jsonl').write_text('')
    (d/'model_responses.jsonl').write_text('')
    (d/'final_summary.json').write_text(json.dumps(final or {'status':'completed'}))
    return d


def test_successful_write_read_and_final_summary_changed_files_create_evidence(tmp_path):
    d=_debug(tmp_path,'Create meeting_scheduled.ics',tools=[
        {'tool_call_id':'r1','tool_name':'Read','status':'completed','args':{'path':'calendar.ics'}},
        {'tool_call_id':'w1','tool_name':'Write','status':'completed','args':{'path':'meeting_scheduled.ics','content':'BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:1\nDTEND:2\nATTENDEE:a\nSUMMARY:s\nEND:VEVENT\nEND:VCALENDAR'}},
    ],final={'status':'completed','changed_files':['meeting_scheduled.ics']},commands=None,patches=False)
    pkt=build_packet(load_debug_run(d))
    cats=pkt['evidence']
    assert cats['fileMutation']
    assert any(e['kind']=='file_write' and e['path']=='meeting_scheduled.ics' for e in cats['fileMutation'])
    assert any(e['source']=='final_summary' for e in cats['fileMutation'])
    assert any(e['kind']=='file_read' for e in cats['inspectionEvidence'])
    assert len(cats['deliverableEvidence']) > 0
    assert not cats['activeFailures']


def test_file_output_no_commands_llm_success_not_flipped(monkeypatch, tmp_path):
    d=_debug(tmp_path,'Create meeting_scheduled.ics',tools=[
        {'tool_call_id':'w1','tool_name':'Write','status':'completed','args':{'path':'meeting_scheduled.ics','content':'BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:1\nDTEND:2\nEND:VEVENT\nEND:VCALENDAR'}},
    ],final={'status':'completed','changed_files':['meeting_scheduled.ics']},commands=None,patches=False)
    run=load_debug_run(d); det=deterministic_result(run, mode='llm_tool_loop')
    s=FileStorage(tmp_path/'ws'); s.init_workspace(); s.save_backends({'b':Backend(name='b',provider='local',base_url='http://127.0.0.1:1234/v1',model='m',roles=['review'],capability_score=1)})
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'choices':[{'message':{'content':json.dumps({'type':'final_verdict','result':1,'verdict':'success','confidence':0.95,'recommendedAction':'accept','reason':'Write created the required output file','deliverableAssessment':{'requiredDeliverables':['meeting_scheduled.ics'],'validatedDeliverables':['meeting_scheduled.ics'],'missingDeliverables':[],'weakValidationReasons':[]}})}}]}
    monkeypatch.setattr(httpx,'post',lambda *a,**k: Resp())
    res=llm_result(run,det,workspace=str(tmp_path/'ws'))
    assert res['result']==1 and res['verdict']=='success'
    assert res['confidence']==0.9


def test_recovered_refused_tool_calls_do_not_remain_active_after_validation(tmp_path):
    d=_debug(tmp_path,'Push main and dev branches and verify https://localhost:8443/index.html',tools=[
        {'tool_call_id':'bad','tool_name':'Bash','status':'failed','args':{'command':'curl http://x'},'error':'refused'},
    ],commands=[
        {'command':'git clone git@localhost:/git/project /tmp/final-test','exit_code':0,'stdout':'clone ok'},
        {'command':'git push origin main','exit_code':0,'stdout':'push ok'},
        {'command':'curl -k https://localhost:8443/index.html','exit_code':0,'stdout':'PASS: Main branch serves correct content'},
    ])
    pkt=build_packet(load_debug_run(d))
    assert pkt['evidence']['finalEndToEndValidation']
    assert pkt['evidence']['recoveredFailures']
    assert pkt['evidence']['activeFailures']==[]


def test_inline_heredoc_local_function_is_weak_and_calibration_keeps_failure(tmp_path):
    d=_debug(tmp_path,'Implement largest_eigenvalue in /app/eigen.py',commands=[
        {'command':"python - <<'PY'\ndef largest_eigenvalue(x):\n return 1\nprint('All correctness tests passed!')\nPY",'exit_code':0,'stdout':'All correctness tests passed!'},
    ],tools=[],final={'status':'completed'})
    det=deterministic_result(load_debug_run(d), mode='llm_tool_loop')
    vals=det['evidenceByCategory']['testValidation']
    assert vals and vals[0]['validationStrength']=='weak'
    assert 'inline' in vals[0]['validationWeakness']
    v={'result':0,'verdict':'failure','confidence':.7,'recommendedAction':'reject','reason':'Validation only tested an inline implementation, not /app/eigen.py','riskFlags':[]}
    out=calibrate(det,v)
    assert out['result']==0 and out['verdict']=='failure'
    assert 'inline implementation' in out['reason']


def test_actual_deliverable_import_validation_is_strong(tmp_path):
    d=_debug(tmp_path,'Implement largest_eigenvalue in /app/eigen.py',tools=[
        {'tool_call_id':'w1','tool_name':'Write','status':'completed','args':{'path':'/app/eigen.py','content':'def largest_eigenvalue(x): return 1'}},
    ],commands=[
        {'command':"python - <<'PY'\nfrom eigen import largest_eigenvalue\nprint('All correctness tests passed!')\nPY",'exit_code':0,'stdout':'All correctness tests passed!'},
    ],final={'status':'completed','changed_files':['/app/eigen.py']})
    det=deterministic_result(load_debug_run(d), mode='llm_tool_loop')
    vals=det['evidenceByCategory']['testValidation']
    assert det['evidenceByCategory']['deliverableEvidence']
    assert vals and vals[0]['validationStrength']=='strong'


def test_read_debug_file_filename_alias_and_safety(tmp_path):
    d=_debug(tmp_path,'x',commands=[],tools=[])
    tools=VerifierTools(load_debug_run(d))
    assert tools.read_debug_file(filename='tool_calls.jsonl')['path']=='tool_calls.jsonl'
    assert tools.read_debug_file(path='tool_calls.jsonl')['path']=='tool_calls.jsonl'
    with pytest.raises(VerifierToolError):
        tools.read_debug_file(filename='../secret')


def test_adjudication_changes_reason_not_stale():
    det={'evidenceByCategory':{'activeFailures':[{'text':'material post validation failure'}],'recoveredFailures':[],'deliverableEvidence':[],'finalEndToEndValidation':[],'testValidation':[],'serviceValidation':[]},'deliverableAssessment':{}}
    v={'result':1,'verdict':'success','confidence':.8,'recommendedAction':'accept','reason':'The run succeeded','riskFlags':[]}
    # No cfg means arbitration records disagreement but preserves the LLM verdict; critically it does not flip to a stale failure.
    out=calibrate(det,v)
    assert out['result']==1 and out['verdict']=='success'
    assert 'Deterministic disagreement' in ' '.join(out['riskFlags'])
