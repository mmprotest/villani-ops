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
    assert s.candidate_evidence_packets
    assert not s.candidate_risk_reviews
    assert s.tournament_phase == 'candidates_complete'
    assert (tmp_path/'run'/'candidates'/'candidate_001'/'evidence.json').exists()
    ev=execute_tool_with_policy(s,'ops_evaluate_tournament',{'reason':'evaluate saved candidates'},'eval',c)
    assert not ev.is_error, ev.content
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



def test_hydrate_tournament_state_from_artifacts_restores_evidence_ranking_selection(tmp_path):
    from villani_ops.agentic.tools import build_candidate_evidence_packet, hydrate_tournament_state_from_artifacts, commit_tournament_selection, _write_tournament_artifacts
    s=state(tmp_path); s.execution_path='candidate_tournament'; s.phase='running_candidates'
    a=_material_candidate(tmp_path, s, 'candidate_001')
    s.candidate_evidence_packets[a.attempt_id]=build_candidate_evidence_packet(s,a)
    s.tournament_ranking=_ranking('candidate_001')
    assert commit_tournament_selection(s, ctx(tmp_path)) is True
    _write_tournament_artifacts(s)
    fresh=state(tmp_path); fresh.execution_path='candidate_tournament'; fresh.phase='running_candidates'
    assert hydrate_tournament_state_from_artifacts(fresh) is True
    assert fresh.candidate_evidence_packets['candidate_001']
    assert fresh.tournament_ranking.selected_candidate_id=='candidate_001'
    assert fresh.selection['selected_attempt_id']=='candidate_001'


def test_low_time_evaluation_uses_fallback_reviews_and_best_effort_selection(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.investigation={'summary':'i'}; s.plan={'strategy':'parallel_candidates'}; s.execution_path='candidate_tournament'; s.tournament_evaluation_deadline_seconds=1; s.reserve_finalization_seconds=60
    a=_material_candidate(tmp_path, s, 'candidate_001')
    from villani_ops.agentic.tools import _record_completed_tournament_candidate
    _record_completed_tournament_candidate(s,a,c)
    res=execute_tool_with_policy(s,'ops_evaluate_tournament',{'reason':'low time'},'eval',c)
    assert not res.is_error, res.content
    assert s.candidate_risk_reviews['candidate_001'].review_quality=='deterministic_fallback'
    assert s.selection['selected_attempt_id']=='candidate_001'
    assert s.selection['selection_basis']=='best_effort_tournament_selection'


def test_tolerant_structured_parsing_repairs_extra_and_missing_fields():
    from villani_ops.agentic.tools import _coerce_structured_payload
    from villani_ops.agentic.state import CandidateRiskReview, PairwiseCandidateComparison
    review=_coerce_structured_payload(CandidateRiskReview, {'candidate_id':'candidate_001','summary':'ok','extra':'ignored'}, quality='model_minimal', changed_files=['a.py'])
    assert review.candidate_id=='candidate_001'
    assert review.confidence <= 0.55
    assert 'model output repaired or missing fields' in review.evidence_gaps
    cmp=_coerce_structured_payload(PairwiseCandidateComparison, {'winner':'candidate_a','extra':'ignored'}, quality='model_minimal', candidate_id=('a','b'))
    assert cmp.candidate_a=='a' and cmp.candidate_b=='b'
    assert cmp.confidence <= 0.55

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


def test_signature_distinguishes_same_file_different_diff(tmp_path):
    from villani_ops.agentic.tools import build_candidate_implementation_signature
    d1='diff --git a/run.py b/run.py\n@@\n+if ready:\n+    return cleanup()\n'
    d2='diff --git a/run.py b/run.py\n@@\n+while ready:\n+    retry()\n'
    s1=build_candidate_implementation_signature('candidate_001',['run.py'],d1)
    s2=build_candidate_implementation_signature('candidate_002',['run.py'],d2)
    assert s1.normalized_patch_hash != s2.normalized_patch_hash
    assert s1.changed_files == s2.changed_files == ['run.py']


def test_agreement_same_file_different_diff_not_same_patch(tmp_path):
    from villani_ops.agentic.tools import build_candidate_evidence_packet, build_candidate_agreement_summary
    s=state(tmp_path); s.execution_path='candidate_tournament'
    a1=_material_candidate(tmp_path,s,'candidate_001','run.py')
    a2=_material_candidate(tmp_path,s,'candidate_002','run.py')
    import pathlib
    pathlib.Path(a2.patch_path).write_text('diff --git a/run.py b/run.py\n--- a/run.py\n+++ b/run.py\n@@\n+while ready:\n+    retry()\n')
    p1=build_candidate_evidence_packet(s,a1); p2=build_candidate_evidence_packet(s,a2)
    ag=build_candidate_agreement_summary({'candidate_001':p1,'candidate_002':p2})
    assert ag.consensus_type != 'same_patch'
    assert 'normalized_patch_hash' in ag.rationale


def test_agreement_same_normalized_diff_is_same_patch(tmp_path):
    from villani_ops.agentic.tools import build_candidate_evidence_packet, build_candidate_agreement_summary
    s=state(tmp_path); s.execution_path='candidate_tournament'
    a1=_material_candidate(tmp_path,s,'candidate_001','run.py')
    a2=_material_candidate(tmp_path,s,'candidate_002','run.py')
    p1=build_candidate_evidence_packet(s,a1); p2=build_candidate_evidence_packet(s,a2)
    ag=build_candidate_agreement_summary({'candidate_001':p1,'candidate_002':p2})
    assert ag.consensus_type == 'same_patch'


def test_command_evidence_from_nested_debug_jsonl(tmp_path):
    from villani_ops.agentic.tools import _extract_command_evidence_from_artifacts
    from villani_ops.agentic.state import CandidateAttemptState
    adir=tmp_path/'artifacts'; adir.mkdir()
    (adir/'events.jsonl').write_text('{"tool":"shell","input":{"command":"make test"},"exit_code":0,"stdout":"ok"}\n{"tool_call":{"name":"run_command","arguments":{"cmd":"lint"}},"returncode":1,"stderr":"bad"}\n')
    a=CandidateAttemptState(attempt_id='c',status='completed',scope='candidate',artifacts_dir=str(adir))
    cmds=_extract_command_evidence_from_artifacts(a)
    assert {c.command for c in cmds} >= {'make test','lint'}
    assert all(c.artifact_path for c in cmds)


def test_fallback_pairwise_uses_signature_not_only_changed_files(tmp_path):
    from villani_ops.agentic.tools import build_candidate_evidence_packet, _risk_review_from_summary, _candidate_summary_from_attempt, _compare_pair
    s=state(tmp_path)
    a1=_material_candidate(tmp_path,s,'candidate_001','run.py')
    a2=_material_candidate(tmp_path,s,'candidate_002','run.py')
    import pathlib
    pathlib.Path(a2.patch_path).write_text('diff --git a/run.py b/run.py\n--- a/run.py\n+++ b/run.py\n@@\n+while ready:\n+    retry()\n')
    p1=build_candidate_evidence_packet(s,a1); p2=build_candidate_evidence_packet(s,a2)
    r1=_risk_review_from_summary(_candidate_summary_from_attempt(a1),p1); r2=_risk_review_from_summary(_candidate_summary_from_attempt(a2),p2)
    cmp=_compare_pair(r1,r2,p1,p2)
    assert cmp.comparison_quality == 'deterministic_fallback'
    assert cmp.confidence <= 0.5
    assert any('normalized patch hash' in x for x in cmp.material_differences)
    assert 'no changed-file difference recorded' not in cmp.material_differences


def test_all_fallback_all_tie_best_effort_low_confidence(tmp_path):
    from villani_ops.agentic.tools import _rank_tournament
    from villani_ops.agentic.state import CandidateRiskReview, PairwiseCandidateComparison
    s=state(tmp_path); s.execution_path='candidate_tournament'
    def rv(cid):
        return CandidateRiskReview(candidate_id=cid,summary='s',changed_files=['a.py'],likely_correct=True,confidence=.4,implementation_strategy='x',minimality_score=.5,correctness_score=.5,hidden_test_risk_score=.5,recommendation='uncertain',rationale='fallback')
    s.candidate_risk_reviews={'candidate_001':rv('candidate_001'),'candidate_002':rv('candidate_002')}
    s.pairwise_comparisons=[PairwiseCandidateComparison(candidate_a='candidate_001',candidate_b='candidate_002',winner='tie',confidence=.5,rationale='tie')]
    rank=_rank_tournament(s)
    assert s.selection_basis == 'best_effort_tournament_selection'
    assert rank.selection_confidence <= .35
    assert any('review/comparison unavailable' in r for r in rank.unresolved_risks)

def test_default_timeout_constant_is_1500_and_policy_uses_it():
    from villani_ops.core.policy import AttemptPlan, DEFAULT_TIMEOUT_SECONDS
    from villani_ops.agentic.runner import OpsRunRequest
    assert DEFAULT_TIMEOUT_SECONDS == 1500
    assert AttemptPlan(backend='b').timeout_seconds == 1500
    assert OpsRunRequest(repo_path='.', task='x').timeout_seconds == 1500
    assert OpsRunRequest(repo_path='.', task='x', timeout_seconds=42).timeout_seconds == 42


def test_default_tournament_budget_policy_is_off(tmp_path):
    s=state(tmp_path)
    assert s.tournament_budget_policy == 'off'
    assert s.finalization_guard_seconds == 60

def test_default_budget_policy_does_not_protect_pairwise_before_reviews(tmp_path):
    from villani_ops.agentic.tools import _can_spend_candidate_review_budget, _tournament_budget_plan
    import time
    s=state(tmp_path)
    s.reserve_finalization_seconds=90; s.reserve_pairwise_seconds=180; s.reserve_ranking_seconds=30; s.max_candidate_review_seconds=240; s.per_candidate_review_timeout_seconds=30
    deadline=time.time()+329
    plan=_tournament_budget_plan(s, deadline)
    assert plan['reserve_finalization_seconds'] == 90
    assert plan['reserve_pairwise_seconds'] == 180
    ok, reason=_can_spend_candidate_review_budget(s, deadline, 0)
    assert ok
    assert reason is None

def test_default_budget_policy_does_not_exhaust_pairwise_budget(tmp_path):
    from villani_ops.agentic.tools import _can_spend_pairwise_budget
    import time
    s=state(tmp_path)
    s.reserve_finalization_seconds=90; s.reserve_ranking_seconds=30; s.per_pairwise_comparison_timeout_seconds=30
    ok, reason=_can_spend_pairwise_budget(s, time.time()+149)
    assert ok
    assert reason is None

def test_finalization_guard_is_only_default_time_based_skip(tmp_path):
    from villani_ops.agentic.tools import _can_spend_candidate_review_budget, _can_spend_pairwise_budget
    import time
    s=state(tmp_path); s.finalization_guard_seconds=60
    review_ok, review_reason=_can_spend_candidate_review_budget(s, time.time()+60, 0)
    pairwise_ok, pairwise_reason=_can_spend_pairwise_budget(s, time.time()+60)
    assert not review_ok and review_reason == 'finalization_guard_reached'
    assert not pairwise_ok and pairwise_reason == 'finalization_guard_reached'

def test_planned_tournament_budget_policy_retains_protected_reserves(tmp_path):
    from villani_ops.agentic.tools import _can_spend_candidate_review_budget, _can_spend_pairwise_budget
    import time
    s=state(tmp_path); s.tournament_budget_policy='planned'
    s.reserve_finalization_seconds=90; s.reserve_pairwise_seconds=180; s.reserve_ranking_seconds=30; s.max_candidate_review_seconds=240; s.per_candidate_review_timeout_seconds=30
    ok, reason=_can_spend_candidate_review_budget(s, time.time()+329, 0)
    assert not ok
    assert reason == 'candidate_review_skipped_protected_pairwise_or_finalization_reserve'
    ok, reason=_can_spend_pairwise_budget(s, time.time()+149)
    assert not ok
    assert reason == 'pairwise_skipped_pairwise_budget_exhausted'


def test_pairwise_priority_starts_with_top_two_then_top_three():
    from villani_ops.agentic.tools import _pairwise_pairs_by_priority
    assert _pairwise_pairs_by_priority(['a','b','c','d'])[:3] == [('a','b'),('a','c'),('b','c')]


def test_valid_tournament_plan_auto_commits_and_launch_is_next(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    res=execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':4,'expected_difficulty':'easy','confidence':1},'p',c)
    assert not res.is_error
    assert s.plan['execution_path']=='candidate_tournament'
    assert s.execution_path=='candidate_tournament'
    assert s.phase=='running_candidates'
    assert s.tournament_phase=='not_started'
    assert 'ops_launch_tournament_candidates' in s.allowed_next_actions()
    assert 'ops_select_execution_path' not in s.allowed_next_actions()
    events=(tmp_path/'run'/'runtime_events.jsonl').read_text()
    assert 'execution_path_auto_committed' in events
    assert 'tournament_launch_ready' in events


def test_select_execution_path_rejected_during_planning(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    res=execute_tool_with_policy(s,'ops_select_execution_path',{'path':'candidate_tournament','reason':'too early'},'sel',c)
    assert res.is_error
    assert s.execution_path=='unknown'
    assert 'ops_submit_plan' in s.allowed_next_actions()


def test_recovery_commits_stale_plan_path_and_launch_emits_recovered_event(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.orchestrator='adaptive'; s.investigation={'summary':'i'}
    s.plan={'summary':'p','strategy':'parallel_candidates','execution_path':'candidate_tournament','candidate_attempts':3}
    s.execution_path='unknown'; s.phase='failed'; s.tournament_phase='not_started'
    res=execute_tool_with_policy(s,'ops_launch_tournament_candidates',{'attempts':3,'reason':'recover stale planned path'},'launch',c)
    assert not res.is_error, res.content
    assert s.execution_path=='candidate_tournament'
    assert s.tournament_candidates_launched==3
    events=(tmp_path/'run'/'runtime_events.jsonl').read_text()
    assert 'execution_path_recovered_from_plan' in events
    assert 'tournament_candidates_launch_started' in events


def test_prompt_guidance_does_not_select_path_during_planning():
    from villani_ops.agentic.prompts import SYSTEM_PROMPT, ADAPTIVE_SYSTEM_APPENDIX
    guidance=SYSTEM_PROMPT + ADAPTIVE_SYSTEM_APPENDIX
    assert 'select execution_path=candidate_tournament when candidate_attempts > 1' not in guidance
    assert 'submit the plan only' in guidance
    assert 'system commits it automatically' in guidance
