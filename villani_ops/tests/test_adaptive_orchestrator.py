from villani_ops.agentic.state_tooling import execute_tool_with_policy
from villani_ops.tests.test_agentic_tools import state, ctx
from villani_ops.agentic.tools import openai_tool_specs


def test_adaptive_plan_constraint_forces_single_task_and_no_subtasks(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    res=execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'decomposition_reason':'split','candidate_attempts':3,'expected_difficulty':'medium','confidence':1},'p',c)
    assert not res.is_error
    assert s.plan['should_decompose'] is False
    assert s.plan['execution_path'] == 'single_task'
    assert s.plan['plan_kind'] == 'single_task'
    assert s.subtasks == []
    assert s.decomposition is None
    assert any('adaptive_orchestrator_forced_single_task_plan' in w for w in s.warnings)


def test_adaptive_invalid_execution_path_is_repaired_with_warning(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'single_task','should_decompose':False,'candidate_attempts':3,'expected_difficulty':'easy','confidence':1},'p',c)
    res=execute_tool_with_policy(s,'ops_select_execution_path',{'path':'decomposed_subtasks','reason':'bad'},'x',c)
    assert not res.is_error
    assert s.execution_path == 'single_task'
    assert s.decomposition_executed is False
    assert s.subtasks == []
    assert any('adaptive_orchestrator_forced_single_task_execution_path' in w for w in s.warnings)


def test_adaptive_tool_schema_hides_decomposition_tools():
    names={t['function']['name'] for t in openai_tool_specs(adaptive=True)}
    assert 'ops_submit_decomposition' not in names
    assert 'ops_run_next_subtask_attempt' not in names
    assert 'ops_run_next_candidate_attempt' in names


def test_agentic_decomposition_still_available(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='agentic'; s.investigation={'summary':'i'}
    res=execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'decomposition_reason':'split','candidate_attempts':3,'expected_difficulty':'medium','confidence':1},'p',c)
    assert not res.is_error
    assert s.decomposition_requested is True
    assert 'ops_submit_decomposition' in s.allowed_next_actions()


def test_adaptive_reuses_single_task_candidate_attempt_tool(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'single_task','should_decompose':False,'candidate_attempts':2,'expected_difficulty':'easy','confidence':1},'p',c)
    execute_tool_with_policy(s,'ops_select_execution_path',{'path':'single_task','reason':'go'},'sel',c)
    res=execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'reason':'try existing single-task machinery'},'a1',c)
    assert not res.is_error, res.content
    assert len(s.candidates) == 1
    assert s.candidates[0].scope == 'candidate'
