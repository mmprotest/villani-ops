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

def test_accepted_decomposition_executes_subtasks_and_blocks_candidates(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); setup_plan(s,c); decomp(s,c); execute_tool_with_policy(s,'ops_validate_decomposition',{},'v',c)
    assert not execute_tool_with_policy(s,'ops_select_execution_path',{'path':'decomposed_subtasks','reason':'ok'},'x',c).is_error
    assert execute_tool_with_policy(s,'ops_launch_candidates',{'attempts':3,'reason':'bad'},'lc',c).is_error
    res=execute_tool_with_policy(s,'ops_launch_subtasks',{'subtask_ids':['s0','s1'],'attempts_per_subtask':3,'reason':'go'},'ls',c)
    assert not res.is_error and all(len(st.attempts)==1 for st in s.subtasks)

def test_rejected_decomposition_falls_back_then_launches_candidates(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); setup_plan(s,c); s.decomposition={'bad':True}; execute_tool_with_policy(s,'ops_validate_decomposition',{},'v',c)
    assert execute_tool_with_policy(s,'ops_select_execution_path',{'path':'decomposed_subtasks','reason':'bad'},'x',c).is_error
    assert not execute_tool_with_policy(s,'ops_select_execution_path',{'path':'parallel_candidates','reason':'fallback'},'p',c).is_error
    assert not execute_tool_with_policy(s,'ops_launch_candidates',{'attempts':3,'reason':'go'},'l',c).is_error
    assert len(s.candidates)==3
