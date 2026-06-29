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


def _material_candidate(tmp_path, s, aid, changed='a.py', status='completed'):
    wt=tmp_path/'run'/'attempts'/aid/'worktree'; wt.mkdir(parents=True, exist_ok=True)
    patch=tmp_path/'run'/'attempts'/aid/'patch.diff'; patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(f'diff --git a/{changed} b/{changed}\n--- a/{changed}\n+++ b/{changed}\n@@ -0,0 +1 @@\n+x\n')
    from villani_ops.agentic.state import CandidateAttemptState
    a=CandidateAttemptState(attempt_id=aid,status=status,scope='candidate',worktree_path=str(wt),artifacts_dir=str(patch.parent),patch_path=str(patch),changed_files=[changed],exit_code=0,runner_status='completed')
    s.candidates.append(a)
    return a


def _ranking(selected, ranked=None):
    from villani_ops.agentic.state import TournamentRanking, RankedCandidate
    ranked=ranked or [selected]
    return TournamentRanking(
        ranked_candidates=[RankedCandidate(candidate_id=aid,rank=i+1,correctness_score=0.7,hidden_test_risk_score=0.2,pairwise_wins=0,pairwise_losses=0,validation_status='not_run',materiality_notes='patch') for i, aid in enumerate(ranked)],
        selected_candidate_id=selected,selection_confidence=0.8,unresolved_risks=['risk'],rationale='ranked evidence')


def test_tournament_ranking_commits_selection_immediately_and_idempotently(tmp_path):
    from villani_ops.agentic.tools import commit_tournament_selection
    s=state(tmp_path); c=ctx(tmp_path); s.investigation={'summary':'i'}; s.plan={'strategy':'parallel_candidates'}; s.execution_path='candidate_tournament'; s.phase='selecting'
    _material_candidate(tmp_path, s, 'candidate_001')
    s.tournament_ranking=_ranking('candidate_001')
    assert commit_tournament_selection(s, c) is True
    first=dict(s.selection)
    assert s.phase=='finalizing'
    assert s.selection['selected_attempt_id']=='candidate_001'
    assert s.selection['selection_basis']=='evidence_based_tournament_selection'
    assert 'ops_finalize_run' in s.allowed_next_actions()
    assert commit_tournament_selection(s, c) is False
    assert s.selection==first
    events=(tmp_path/'run'/'runtime_events.jsonl').read_text()
    assert events.count('selection_completed') == 1


def test_finalize_defensively_commits_tournament_selection(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.execution_path='candidate_tournament'; s.phase='selecting'
    _material_candidate(tmp_path, s, 'candidate_001')
    s.tournament_ranking=_ranking('candidate_001')
    res=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'accepted','summary':'done','selected_attempt_id':'candidate_001'},'fin',c)
    assert not res.is_error, res.content
    assert s.selection['selected_attempt_id']=='candidate_001'
    assert s.status=='completed'


def test_selecting_phase_does_not_expose_finalize_only_without_selection(tmp_path):
    s=state(tmp_path); s.investigation={'summary':'i'}; s.plan={'strategy':'parallel_candidates'}; s.execution_path='candidate_tournament'; s.phase='selecting'
    _material_candidate(tmp_path, s, 'candidate_001')
    s.tournament_ranking=_ranking('candidate_001')
    actions=s.allowed_next_actions()
    assert 'ops_finalize_run' in actions
    assert 'ops_select_winner' in actions


def test_recovery_commits_tournament_selection_and_never_recommends_blocked_review(tmp_path):
    from villani_ops.agentic.recovery import recommend_next_agentic_action
    s=state(tmp_path); s.investigation={'summary':'i'}; s.plan={'strategy':'parallel_candidates'}; s.execution_path='candidate_tournament'; s.phase='selecting'
    _material_candidate(tmp_path, s, 'candidate_001')
    s.tournament_ranking=_ranking('candidate_001')
    rec=recommend_next_agentic_action(s)
    assert s.selection['selected_attempt_id']=='candidate_001'
    assert rec.tool_name=='ops_finalize_run'
    assert rec.tool_name in s.allowed_next_actions()
    assert rec.tool_name!='ops_review_attempt'


def test_best_effort_selection_when_ranking_missing_but_candidate_materializable(tmp_path):
    from villani_ops.agentic.tools import commit_tournament_selection
    s=state(tmp_path); s.investigation={'summary':'i'}; s.plan={'strategy':'parallel_candidates'}; s.execution_path='candidate_tournament'; s.phase='selecting'
    _material_candidate(tmp_path, s, 'candidate_001')
    assert commit_tournament_selection(s, ctx(tmp_path)) is True
    assert s.tournament_ranking.selected_candidate_id=='candidate_001'
    assert s.selection['selection_basis']=='best_effort_tournament_selection'


def test_ranked_non_materializable_candidate_skipped_for_next_materializable(tmp_path):
    from villani_ops.agentic.tools import commit_tournament_selection
    s=state(tmp_path); s.investigation={'summary':'i'}; s.plan={'strategy':'parallel_candidates'}; s.execution_path='candidate_tournament'; s.phase='selecting'
    _material_candidate(tmp_path, s, 'candidate_001')
    s.candidates[0].patch_path=str(tmp_path/'missing.patch')
    _material_candidate(tmp_path, s, 'candidate_002', changed='b.py')
    s.tournament_ranking=_ranking('candidate_001', ['candidate_001','candidate_002'])
    assert commit_tournament_selection(s, ctx(tmp_path)) is True
    assert s.selection['selected_attempt_id']=='candidate_002'
    assert s.selection['selection_evidence']['skipped_ranked_candidates']


def test_no_materializable_tournament_candidates_fails_clearly(tmp_path):
    from villani_ops.agentic.tools import commit_tournament_selection
    s=state(tmp_path); s.investigation={'summary':'i'}; s.plan={'strategy':'parallel_candidates'}; s.execution_path='candidate_tournament'; s.phase='selecting'
    from villani_ops.agentic.state import CandidateAttemptState
    s.candidates.append(CandidateAttemptState(attempt_id='candidate_001',status='completed',scope='candidate'))
    s.tournament_ranking=_ranking('candidate_001')
    assert commit_tournament_selection(s, ctx(tmp_path)) is False
    assert 'no_materializable_tournament_candidate' in s.blockers
