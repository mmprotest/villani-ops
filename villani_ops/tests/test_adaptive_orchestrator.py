from villani_ops.agentic.state_tooling import execute_tool_with_policy
from villani_ops.tests.test_agentic_tools import state, ctx
from villani_ops.agentic.tools import openai_tool_specs, build_tournament_candidate_prompt


def test_adaptive_plan_constraint_selects_tournament_for_multiple_attempts(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    res=execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'decomposition_reason':'split','candidate_attempts':3,'expected_difficulty':'medium','confidence':1},'p',c)
    assert not res.is_error
    assert s.plan['should_decompose'] is False
    assert s.plan['execution_path'] == 'candidate_tournament'
    assert s.plan['plan_kind'] == 'candidate_tournament'
    assert s.subtasks == []
    assert s.decomposition is None
    assert any('adaptive_orchestrator_forced_single_task_plan' in w for w in s.warnings)


def test_adaptive_single_candidate_still_uses_single_task(tmp_path):
    s=state(tmp_path); s.candidate_attempts=1; c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    res=execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'single_task','should_decompose':False,'candidate_attempts':1,'expected_difficulty':'easy','confidence':1},'p',c)
    assert not res.is_error
    execute_tool_with_policy(s,'ops_select_execution_path',{'path':'single_task','reason':'go'},'x',c)
    assert s.execution_path == 'single_task'


def test_adaptive_invalid_execution_path_is_repaired_to_tournament(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'single_task','should_decompose':False,'candidate_attempts':3,'expected_difficulty':'easy','confidence':1},'p',c)
    res=execute_tool_with_policy(s,'ops_select_execution_path',{'path':'decomposed_subtasks','reason':'bad'},'x',c)
    assert not res.is_error
    assert s.execution_path == 'candidate_tournament'
    assert s.decomposition_executed is False
    assert s.subtasks == []
    assert any('adaptive_orchestrator_forced_tournament_execution_path' in w for w in s.warnings)


def test_adaptive_tool_schema_recommends_tournament_launcher_not_retry_tool():
    names={t['function']['name'] for t in openai_tool_specs(adaptive=True)}
    assert 'ops_submit_decomposition' not in names
    assert 'ops_run_next_subtask_attempt' not in names
    assert 'ops_run_next_candidate_attempt' not in names
    assert 'ops_launch_tournament_candidates' in names


def test_agentic_decomposition_still_available(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='agentic'; s.investigation={'summary':'i'}
    res=execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'decomposition_reason':'split','candidate_attempts':3,'expected_difficulty':'medium','confidence':1},'p',c)
    assert not res.is_error
    assert s.decomposition_requested is True
    assert 'ops_submit_decomposition' in s.allowed_next_actions()


def test_tournament_candidate_prompt_is_clean_pass_through(tmp_path):
    s=state(tmp_path); s.task='fix the bug'; s.success_criteria='tests pass'; s.execution_path='candidate_tournament'
    s.attempt_observations=[]; s.reviews=[{'summary':'looks good'}]
    prompt=build_tournament_candidate_prompt(s, reason='launch')
    assert 'fix the bug' in prompt
    assert 'tests pass' in prompt
    forbidden=['PREVIOUS ATTEMPT','AttemptObservation','review','oracle','behavioural','comparison','hidden-test','differ from another candidate']
    for term in forbidden:
        assert term.lower() not in prompt.lower()


def test_adaptive_tournament_launches_parallel_and_writes_ranking(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); c.backend.max_parallel=2; c.coding_backend.max_parallel=2
    s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':3,'expected_difficulty':'easy','confidence':1},'p',c)
    execute_tool_with_policy(s,'ops_select_execution_path',{'path':'candidate_tournament','reason':'go'},'sel',c)
    res=execute_tool_with_policy(s,'ops_launch_tournament_candidates',{'attempts':3,'reason':'parallel independent'},'launch',c)
    assert not res.is_error, res.content
    assert s.tournament_candidates_launched == 3
    assert s.tournament_parallelism_used == 2
    assert len({a.worktree_path for a in s.candidates}) == 3
    assert s.candidate_summaries
    assert s.candidate_risk_reviews
    assert s.pairwise_comparisons
    assert s.tournament_ranking and s.tournament_ranking.selected_candidate_id
    assert s.candidate_agreement_summary
    assert (tmp_path/'run'/'comparisons'/'ranking.json').exists()
