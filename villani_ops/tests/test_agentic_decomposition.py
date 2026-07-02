import pytest

pytestmark = pytest.mark.integration
from villani_ops.tests.test_agentic_tools import state, ctx
from villani_ops.agentic.state_tooling import execute_tool_with_policy

def setup_plan(s,c):
    execute_tool_with_policy(s,'ops_submit_investigation',{'summary':'i','confidence':1},'i',c)
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'candidate_attempts':3,'expected_difficulty':'medium','confidence':1},'p',c)
def decomp(s,c,items=2):
    subs=[{'id':f's{i}','title':f'S{i}','objective':f'o{i}','confidence':1,'can_run_parallel':True} for i in range(items)]
    return execute_tool_with_policy(s,'ops_submit_decomposition',{'should_use_decomposition':True,'reason':'r','subtasks':subs,'confidence':1},'d',c)

def test_semantic_decomposition_acceptance_computed(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); setup_plan(s,c); decomp(s,c)
    res=execute_tool_with_policy(s,'ops_validate_decomposition',{'semantic':True},'v',c)
    assert res.content['accepted'] is True and s.decomposition_accepted is True

def test_no_advisory_only_rejected_falls_back_explicitly(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); setup_plan(s,c)
    s.decomposition={'should_use_decomposition':False}; s.subtasks=[]
    execute_tool_with_policy(s,'ops_validate_decomposition',{},'v',c)
    res=execute_tool_with_policy(s,'ops_select_execution_path',{'path':'parallel_candidates','reason':'rejected'},'x',c)
    assert not res.is_error and s.decomposition_fallback_used and not s.decomposition_executed
    assert not hasattr(s,'advisory_only')

def test_accepted_decomposition_runs_one_subtask_and_blocks_bulk_launchers(tmp_path):
    from villani_ops.agentic.tools import openai_tool_specs
    s=state(tmp_path); c=ctx(tmp_path); setup_plan(s,c); decomp(s,c); execute_tool_with_policy(s,'ops_validate_decomposition',{},'v',c)
    assert not execute_tool_with_policy(s,'ops_select_execution_path',{'path':'decomposed_subtasks','reason':'ok'},'x',c).is_error
    assert execute_tool_with_policy(s,'ops_launch_candidates',{'attempts':3,'reason':'bad'},'lc',c).is_error
    assert 'ops_run_next_subtask_attempt' in s.allowed_next_actions()
    assert 'ops_launch_subtasks' not in s.allowed_next_actions()
    assert 'ops_launch_subtasks' not in {t['function']['name'] for t in openai_tool_specs()}
    bulk=execute_tool_with_policy(s,'ops_launch_subtasks',{'subtask_ids':['s0','s1'],'attempts_per_subtask':3,'reason':'go'},'ls',c)
    assert bulk.is_error and 'legacy/internal compatibility tool' in str(bulk.content)
    res=execute_tool_with_policy(s,'ops_run_next_subtask_attempt',{'reason':'go'},'one',c)
    assert not res.is_error, res.content
    assert sum(len(st.attempts) for st in s.subtasks)==1

def test_rejected_decomposition_falls_back_then_launches_candidates(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); setup_plan(s,c); s.decomposition={'bad':True}; execute_tool_with_policy(s,'ops_validate_decomposition',{},'v',c)
    assert execute_tool_with_policy(s,'ops_select_execution_path',{'path':'decomposed_subtasks','reason':'bad'},'x',c).is_error
    assert not execute_tool_with_policy(s,'ops_select_execution_path',{'path':'parallel_candidates','reason':'fallback'},'p',c).is_error
    assert not execute_tool_with_policy(s,'ops_launch_candidates',{'attempts':3,'reason':'go'},'l',c).is_error
    assert len(s.candidates)==3

def test_recovery_recommends_select_path_after_accepted_decomposition(tmp_path):
    from villani_ops.agentic.recovery import recommend_next_agentic_action
    from villani_ops.agentic.state import SubtaskState
    from villani_ops.tests.test_agentic_tools import state
    s=state(tmp_path)
    s.investigation={'summary':'i'}; s.plan={'summary':'p','should_decompose':True}
    s.decomposition={'should_use_decomposition':True}
    s.decomposition_validated=True; s.decomposition_accepted=True; s.execution_path='unknown'
    s.subtasks=[SubtaskState(subtask_id='s0',title='s0',objective='o'),SubtaskState(subtask_id='s1',title='s1',objective='o')]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_select_execution_path'
    assert rec.tool_input['path'] == 'decomposed_subtasks'
    assert rec.can_execute_deterministically is True

def test_recovery_recommends_ready_subtask_launch_after_path_selected(tmp_path):
    from villani_ops.agentic.recovery import recommend_next_agentic_action
    from villani_ops.agentic.state import SubtaskState
    from villani_ops.tests.test_agentic_tools import state
    s=state(tmp_path)
    s.investigation={'summary':'i'}; s.plan={'summary':'p','should_decompose':True}
    s.decomposition_validated=True; s.decomposition_accepted=True; s.execution_path='decomposed_subtasks'
    s.subtasks=[SubtaskState(subtask_id='s0',title='s0',objective='o'),SubtaskState(subtask_id='s1',title='s1',objective='o',dependencies=['s0'])]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_run_next_subtask_attempt'
    assert rec.tool_input['subtask_id'] == 's0'
    assert rec.can_execute_deterministically is True

def test_failed_finalize_blocked_while_accepted_decomposition_can_continue(tmp_path):
    from villani_ops.agentic.state import SubtaskState
    from villani_ops.tests.test_agentic_tools import state, ctx
    from villani_ops.agentic.state_tooling import execute_tool_with_policy
    s=state(tmp_path)
    s.investigation={'summary':'i'}; s.plan={'summary':'p','should_decompose':True}
    s.decomposition_validated=True; s.decomposition_accepted=True; s.execution_path='unknown'; s.recovery_count=1
    s.subtasks=[SubtaskState(subtask_id='s0',title='s0',objective='o'),SubtaskState(subtask_id='s1',title='s1',objective='o')]
    res=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'failed','summary':'failed','blockers':['agentic_orchestrator_no_progress']},'fin',ctx(tmp_path))
    assert res.is_error
    assert 'deterministic next action' in str(res.content)
    s.execution_path='decomposed_subtasks'
    res=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'failed','summary':'failed','blockers':['agentic_orchestrator_no_progress']},'fin2',ctx(tmp_path))
    assert res.is_error
