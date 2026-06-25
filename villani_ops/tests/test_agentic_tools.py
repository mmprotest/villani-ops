from pathlib import Path
from villani_ops.agentic.state import OpsRunState, CandidateAttemptState
from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.state_tooling import execute_tool_with_policy, OpsToolContext

def state(tmp_path):
    return OpsRunState(run_id='r',run_dir=str(tmp_path),repo_path=str(tmp_path),task='t',mode='performance',runner='villani-code',candidate_attempts=3)
def ctx(tmp_path): return OpsToolContext(run_dir=tmp_path,recorder=OpsEventRecorder(tmp_path,'r'),transcript=[])

def test_tool_schemas_reject_extra_fields(tmp_path):
    s=state(tmp_path); res=execute_tool_with_policy(s,'ops_submit_investigation',{'summary':'x','confidence':1,'unknown':1},'u',ctx(tmp_path))
    assert res.is_error
    assert s.investigation is None
    assert 'tool_failed' in (tmp_path/'runtime_events.jsonl').read_text()

def test_investigation_validation_commands_no_format_keyerror(tmp_path):
    s=state(tmp_path); res=execute_tool_with_policy(s,'ops_submit_investigation',{'summary':'x','confidence':1,'validation_plan':{'commands':[{'cmd':'pytest','purpose':'tests'}]}},'u',ctx(tmp_path))
    assert not res.is_error
    assert s.investigation['validation_plan']['commands'][0]['cmd']=='pytest'

def test_finalize_blocked_while_running(tmp_path):
    s=state(tmp_path); s.candidates.append(CandidateAttemptState(attempt_id='a',status='running',scope='candidate'))
    res=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'failed','summary':'x'},'u',ctx(tmp_path))
    assert res.is_error and s.status=='active'
