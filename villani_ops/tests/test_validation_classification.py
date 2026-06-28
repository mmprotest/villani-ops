import sys
from villani_ops.agentic.state import CandidateAttemptState
from villani_ops.agentic.state_tooling import execute_tool_with_policy
from villani_ops.agentic.tools import _default_validation_commands
from villani_ops.core.acceptance import is_attempt_acceptance_eligible
from villani_ops.tests.test_agentic_finish_path import _eligible_attempt
from villani_ops.tests.test_agentic_tools import ctx, state

BROKEN='python -m pytest --tb=short -v 2>&1 || python -c "import run" && print(\'Import OK\')'

def _run(tmp_path, command):
    s=state(tmp_path); c=ctx(tmp_path); a,_=_eligible_attempt(tmp_path,s)
    out=execute_tool_with_policy(s,'ops_run_validation',{'target':'candidate','target_id':a.attempt_id,'commands':[command]},'v',c)
    assert not out.is_error
    return s,a,out.content

def test_explicit_user_validation_failure_remains_blocking_failed_candidate(tmp_path):
    s,a,res=_run(tmp_path, {'cmd':f'{sys.executable} -c "import sys; sys.exit(7)"','source':'user_provided','confidence':'high','blocking':True})
    assert res['status']=='failed_candidate'
    assert res['commands'][0]['blocking'] is True
    assert res['decision_status']=='failed'
    assert 'validation_failed' in a.acceptance_blockers

def test_explicit_user_validation_launch_failure_becomes_infrastructure_error(tmp_path):
    s,a,res=_run(tmp_path, {'cmd':'definitely_missing_validation_executable_zzz','source':'user_provided','confidence':'high','blocking':True})
    assert res['status']=='infrastructure_error'
    assert res['commands'][0]['infrastructure_error']
    assert 'validation_failed' not in a.acceptance_blockers

def test_project_detected_high_confidence_failure_is_blocking_failed_candidate(tmp_path):
    s,a,res=_run(tmp_path, {'cmd':f'{sys.executable} -c "import sys; sys.exit(3)"','source':'project_detected','confidence':'high','blocking':True})
    assert res['status']=='failed_candidate'
    assert res['decision_status']=='failed'
    assert 'validation_failed' in a.acceptance_blockers

def test_generated_low_confidence_failure_is_diagnostic_failed(tmp_path):
    s,a,res=_run(tmp_path, {'cmd':f'{sys.executable} -c "import sys; sys.exit(4)"','source':'generated','confidence':'low'})
    assert res['status']=='diagnostic_failed'
    assert res['commands'][0]['blocking'] is False
    ok, blockers=is_attempt_acceptance_eligible(a, state=s)
    assert 'validation_failed' not in blockers

def test_missing_reliable_validation_skipped_no_reliable_command(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); a,_=_eligible_attempt(tmp_path,s)
    out=execute_tool_with_policy(s,'ops_run_validation',{'target':'candidate','target_id':a.attempt_id,'commands':[]},'v',c)
    assert out.content['status']=='skipped_no_reliable_command'
    assert not _default_validation_commands(s)

def test_invalid_generated_validation_command_is_infrastructure_error_not_failed_candidate(tmp_path):
    s,a,res=_run(tmp_path, {'cmd':BROKEN,'source':'generated','confidence':'low'})
    assert res['status']=='infrastructure_error'
    assert res['commands'][0]['execution_mode']=='argv'
    assert res['commands'][0]['blocking'] is False
    assert res['commands'][0]['status']!='failed_candidate'
    assert 'validation_failed' not in a.acceptance_blockers

def test_diagnostic_failed_does_not_automatically_reject_candidate(tmp_path):
    s,a,res=_run(tmp_path, {'cmd':f'{sys.executable} -c "import sys; sys.exit(5)"','source':'diagnostic','confidence':'low'})
    assert res['status']=='diagnostic_failed'
    assert 'validation_failed' not in a.acceptance_blockers

def test_adaptive_mode_still_single_task_and_agentic_decomposition_unaffected(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    r=execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'candidate_attempts':2,'expected_difficulty':'medium','confidence':1},'p',c)
    assert not r.is_error and r.content['execution_path']=='single_task'
    s2=state(tmp_path); c2=ctx(tmp_path); s2.orchestrator='agentic'; s2.investigation={'summary':'i'}
    r2=execute_tool_with_policy(s2,'ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'candidate_attempts':2,'expected_difficulty':'medium','confidence':1},'p',c2)
    assert not r2.is_error
    assert s2.plan['strategy']=='decompose_then_execute'
