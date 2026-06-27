from villani_ops.agentic.state import OpsRunState, SubtaskState, CandidateAttemptState, detect_decomposition_deadlock
from villani_ops.agentic.state_tooling import execute_tool_with_policy
from villani_ops.agentic.recovery import recommend_next_agentic_action
from villani_ops.tests.test_agentic_tools import state, ctx


def deadlocked_state(tmp_path):
    s=state(tmp_path)
    s.candidate_attempts=1
    s.investigation={'summary':'i'}; s.plan={'summary':'p'}
    s.execution_path='decomposed_subtasks'; s.phase='running_subtasks'; s.decomposition_accepted=True; s.decomposition_validated=True
    a=CandidateAttemptState(attempt_id='a_attempt_001',status='failed',scope='subtask',subtask_id='a',failure_reason='boom')
    s.subtasks=[
        SubtaskState(subtask_id='a',title='A',objective='oa',status='failed',attempts=[a]),
        SubtaskState(subtask_id='b',title='B',objective='ob',dependencies=['a'],status='skipped'),
        SubtaskState(subtask_id='c',title='C',objective='oc',dependencies=['b'],status='skipped'),
    ]
    s.decomposed_execution_status='blocked'
    s.decomposed_execution_failed_subtasks=['a']; s.decomposed_execution_blocked_subtasks=['b','c']
    s.decomposed_execution_blockers=['required_subtask_failed','dependency_failed','subtask_attempts_exhausted','no_ready_subtasks_remaining','blocked_dependents_exist','decomposition_deadlocked']
    s.partial_progress={'accepted_subtasks':[],'failed_subtasks':['a'],'blocked_subtasks':['b','c']}
    return s


def test_detect_decomposition_deadlock_records_failed_and_blocked(tmp_path):
    s=deadlocked_state(tmp_path)
    d=detect_decomposition_deadlock(s)
    assert d and d.deadlocked
    assert d.failed_subtasks == ['a']
    assert d.blocked_subtasks == ['b','c']
    assert d.can_continue_subtasks is False


def test_allowed_actions_after_deadlock_include_fallback_not_integration(tmp_path):
    s=deadlocked_state(tmp_path)
    actions=s.allowed_next_actions()
    assert 'ops_start_candidate_fallback' in actions
    assert 'ops_launch_candidates' not in actions
    assert 'ops_finalize_run' in actions
    assert 'ops_integrate_subtasks' not in actions
    assert actions != ['ops_get_state','ops_launch_subtasks']


def test_start_candidate_fallback_preserves_subtasks_and_enables_adaptive_candidate(tmp_path):
    s=deadlocked_state(tmp_path); c=ctx(tmp_path)
    res=execute_tool_with_policy(s,'ops_start_candidate_fallback',{'reason':'required subtask failed and dependent subtasks are blocked'},'fb',c)
    assert not res.is_error, res.content
    assert s.fallback_used is True
    assert s.fallback_from_execution_path == 'decomposed_subtasks'
    assert s.fallback_execution_path == 'parallel_candidates_after_decomposition_deadlock'
    assert [st.subtask_id for st in s.subtasks] == ['a','b','c']
    assert s.candidates == []
    assert 'ops_run_next_fallback_candidate_attempt' in res.content['next_allowed_actions']
    run=execute_tool_with_policy(s,'ops_run_next_fallback_candidate_attempt',{'reason':'run fallback'},'fb2',c)
    assert not run.is_error, run.content
    assert s.candidates[0].attempt_id == 'candidate_001'
    assert s.candidates[0].candidate_kind == 'fallback'


def test_recovery_recommends_fallback_then_launch(tmp_path):
    s=deadlocked_state(tmp_path)
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_start_candidate_fallback'
    assert execute_tool_with_policy(s,'ops_start_candidate_fallback',{'reason':'deadlock'},'fb',ctx(tmp_path)).is_error is False
    rec2=recommend_next_agentic_action(s)
    assert rec2.tool_name == 'ops_run_next_fallback_candidate_attempt'


def test_failed_finalize_clears_raw_subtask_selected_attempt(tmp_path):
    s=deadlocked_state(tmp_path); c=ctx(tmp_path)
    res=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'failed','summary':'failed','selected_attempt_id':'a_attempt_001','blockers':['required_subtask_failed']},'fin',c)
    assert not res.is_error, res.content
    assert s.final_decision.get('selected_attempt_id') is None
    assert s.final_decision.get('best_partial_attempt_id') == 'a_attempt_001'
    assert 'decomposition_deadlocked' in s.final_decision['blockers']
    assert 'b, c' in s.final_decision['summary']


def test_legacy_launch_subtasks_gate_default_and_enabled(tmp_path):
    from villani_ops.agentic.tools import openai_tool_specs
    s=deadlocked_state(tmp_path); c=ctx(tmp_path)
    assert 'ops_launch_subtasks' not in {t['function']['name'] for t in openai_tool_specs()}
    assert 'ops_launch_subtasks' not in s.allowed_next_actions()
    blocked=execute_tool_with_policy(s,'ops_launch_subtasks',{'subtask_ids':['a'],'attempts_per_subtask':1,'reason':'legacy'},'legacy',c)
    assert blocked.is_error
    assert 'legacy/internal compatibility tool' in str(blocked.content)
    s.adaptive_context['legacy_ops_launch_subtasks_enabled']=True
    allowed=execute_tool_with_policy(s,'ops_launch_subtasks',{'subtask_ids':['a'],'attempts_per_subtask':1,'reason':'legacy'},'legacy2',c)
    # The legacy call is past policy when explicitly gated; this deadlocked fixture may fail inside implementation rather than policy.
    assert 'legacy/internal compatibility tool' not in str(allowed.content)


def test_recovery_and_guidance_do_not_recommend_legacy_launch_subtasks(tmp_path):
    from villani_ops.agentic.recovery import handle_no_tool_call
    s=deadlocked_state(tmp_path)
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name != 'ops_launch_subtasks'
    msg=handle_no_tool_call(s).message['content']
    assert 'ops_launch_subtasks' not in msg
    assert 'ops_run_next_fallback_candidate_attempt' not in s.allowed_next_actions()
    assert 'ops_launch_candidates' not in s.allowed_next_actions()
