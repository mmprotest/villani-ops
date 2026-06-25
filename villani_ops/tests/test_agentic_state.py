from pathlib import Path
from villani_ops.tests.test_agentic_tools import state, ctx
from villani_ops.agentic.state import OpsRunState
from villani_ops.agentic.artifacts import derive_graph
from villani_ops.agentic.state_tooling import execute_tool_with_policy

def test_state_reload_does_not_duplicate_completed_work(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path)
    execute_tool_with_policy(s,'ops_submit_investigation',{'summary':'i','confidence':1},'i',c)
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':1,'expected_difficulty':'easy','confidence':1},'p',c)
    s.save(tmp_path/'state.json'); loaded=OpsRunState.load(tmp_path/'state.json')
    assert 'ops_submit_investigation' not in loaded.allowed_next_actions()
    assert 'ops_select_execution_path' in loaded.allowed_next_actions()

def test_graph_is_derived_not_truth(tmp_path):
    s=state(tmp_path); g=derive_graph(s,[])
    assert g['canonical']=='state.json'
    assert s.allowed_next_actions() != []
