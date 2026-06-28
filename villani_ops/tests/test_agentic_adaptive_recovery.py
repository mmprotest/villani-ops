from villani_ops.agentic.recovery import recommend_next_agentic_action
from villani_ops.agentic.state import OpsRunState, CandidateAttemptState, AttemptObservation


def state(tmp_path, attempts=3):
    repo=tmp_path/'repo'; run=tmp_path/'run'; repo.mkdir(); run.mkdir()
    return OpsRunState(run_id='r',run_dir=str(run),repo_path=str(repo),task='fix bug',mode='performance',runner='villani-code',candidate_attempts=attempts,investigation={'summary':'i'},plan={'strategy':'single_task'},execution_path='single_task',phase='running_candidates',orchestrator='adaptive')


def attempt(aid='candidate_001'):
    return CandidateAttemptState(attempt_id=aid,status='completed',scope='candidate',changed_files=['app.py'],patch_path='/tmp/diff.patch')


def obs(aid, outcome, **kw):
    data=dict(attempt_id=aid,scope='candidate',outcome=outcome,backend_name='b1',changed_files=['app.py'],evidence=['pytest failed'],blockers=['validation_failed'],next_attempt_directives=['focus tests'],validation_snapshot_id='passed:1',review_snapshot_id='failed:1:True')
    data.update(kw)
    return AttemptObservation(**data)

def evidenced_attempt(aid='candidate_001'):
    a=attempt(aid); a.validation={'passed':False,'status':'failed','commands':[]}; a.validation_status='failed'; a.validation_results=[a.validation]; a.review={'decision':'fail','recommended_action':'retry','blockers':['review blocker']}; a.review_status='failed'; return a


def test_missing_completed_attempt_without_patch_observation_is_created_before_retry(tmp_path):
    s=state(tmp_path); a=attempt(); a.patch_path=None; a.changed_files=[]; s.candidates=[a]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_observe_completed_attempt'
    assert rec.tool_input['attempt_id'] == 'candidate_001'
    assert rec.action == 'create_or_refresh_observation'


def test_validation_failed_observation_runs_focused_retry(tmp_path):
    s=state(tmp_path); s.candidates=[evidenced_attempt()]; s.attempt_observations=[obs('candidate_001','validation_failed',should_repair=True,validation_snapshot_id='failed:1',review_snapshot_id='failed:0:True')]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_run_next_candidate_attempt'
    assert rec.tool_input['base_attempt_id'] == 'candidate_001'
    assert rec.tool_input['repair'] is True
    assert 'validation' in rec.tool_input['reason']


def test_review_failed_observation_runs_focused_retry(tmp_path):
    s=state(tmp_path); s.candidates=[evidenced_attempt()]; s.attempt_observations=[obs('candidate_001','review_failed',blockers=['review blocker'],should_repair=True,validation_snapshot_id='failed:1',review_snapshot_id='failed:0:True')]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_run_next_candidate_attempt'
    assert 'review blockers' in rec.tool_input['reason']


def test_no_patch_observation_adds_inspect_edit_directive(tmp_path):
    s=state(tmp_path); a=attempt(); a.changed_files=[]; s.candidates=[a]
    s.attempt_observations=[obs('candidate_001','no_patch',changed_files=[],next_attempt_directives=['inspect source files first'],validation_snapshot_id='not_run:0',review_snapshot_id='not_run:0:False')]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_run_next_candidate_attempt'
    assert 'inspect and edit' in rec.tool_input['reason']


def test_should_escalate_backend_uses_other_assessed_backend(tmp_path):
    s=state(tmp_path); s.candidates=[evidenced_attempt()]
    s.backend_assessments={'b1':{'attempts':1},'b2':{'attempts':0}}
    s.attempt_observations=[obs('candidate_001','runner_failed',should_escalate_backend=True,validation_snapshot_id='failed:1',review_snapshot_id='failed:0:True')]
    rec=recommend_next_agentic_action(s)
    assert rec.action == 'escalate_backend_retry'
    assert rec.tool_input['backend_name'] == 'b2'


def test_budget_exhaustion_reports_observation_blockers(tmp_path):
    s=state(tmp_path, attempts=1); s.candidates=[evidenced_attempt()]
    s.attempt_observations=[obs('candidate_001','validation_failed',validation_snapshot_id='failed:1',review_snapshot_id='failed:0:True')]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_select_winner'
    assert rec.tool_input['decision'] == 'reject_all'
    assert 'validation_failed' in rec.tool_input['reasons']


def test_allowed_next_actions_omits_next_attempt_when_budget_exhausted(tmp_path):
    s=state(tmp_path, attempts=1); a=attempt(); a.validation={'passed':False,'status':'failed','commands':[]}; a.validation_status='failed'; a.review={'decision':'fail','recommended_action':'retry','blockers':['b']}; a.review_status='failed'; s.candidates=[a]; s.attempt_observations=[obs('candidate_001','validation_failed',validation_snapshot_id='failed:0',review_snapshot_id='failed:0:True')]
    assert 'ops_run_next_candidate_attempt' not in s.allowed_next_actions()
    assert set(s.allowed_next_actions()) & {'ops_select_winner','ops_finalize_run'}


def test_recovery_validates_before_creating_missing_observation(tmp_path):
    s=state(tmp_path); s.candidates=[attempt()]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_run_validation'
    assert rec.tool_input['target_id'] == 'candidate_001'


def test_recovery_reviews_before_creating_missing_observation(tmp_path):
    s=state(tmp_path); a=attempt(); a.validation={'passed':True,'status':'passed','commands':[]}; a.validation_status='passed'; a.validation_results=[a.validation]; s.candidates=[a]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_review_attempt'
    assert rec.tool_input['attempt_id'] == 'candidate_001'


def test_recovery_creates_missing_observation_after_evidence(tmp_path):
    s=state(tmp_path); a=attempt(); a.validation={'passed':False,'status':'failed','commands':[]}; a.validation_status='failed'; a.validation_results=[a.validation]; a.review={'decision':'fail','recommended_action':'retry','blockers':['review blocker']}; a.review_status='failed'; s.candidates=[a]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_observe_completed_attempt'
    assert rec.action == 'create_or_refresh_observation'


def test_recovery_does_not_retry_before_current_attempt_is_observed(tmp_path):
    s=state(tmp_path, attempts=2); a=attempt(); a.validation={'passed':False,'status':'failed','commands':[]}; a.validation_status='failed'; a.validation_results=[a.validation]; a.review={'decision':'fail','recommended_action':'retry','blockers':['review blocker']}; a.review_status='failed'; s.candidates=[a]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_observe_completed_attempt'
    assert rec.tool_name != 'ops_run_next_candidate_attempt'


def test_budget_exhaustion_structured_failure_includes_observations(tmp_path):
    s=state(tmp_path, attempts=1); a=attempt(); a.validation={'passed':False,'status':'failed','commands':[]}; a.validation_status='failed'; a.review={'decision':'fail','recommended_action':'retry','blockers':['scope blocker']}; a.review_status='failed'; s.candidates=[a]
    s.attempt_observations=[obs('candidate_001','validation_failed',validation_status='failed',review_status='failed',validation_snapshot_id='failed:0',review_snapshot_id='failed:0:True',blockers=['scope blocker'])]
    rec=recommend_next_agentic_action(s)
    info=rec.tool_input['failure_observations']
    assert info['attempt_count'] == 1
    assert info['latest_outcome'] == 'validation_failed'
    assert info['attempt_observations'][0]['attempt_id'] == 'candidate_001'
    assert info['recommended_next_manual_action']


def usable_attempt(tmp_path, aid='candidate_001', score=0.8, confidence=0.6):
    p=tmp_path/f'{aid}.patch'
    p.write_text('diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n')
    a=attempt(aid)
    a.patch_path=str(p)
    a.changed_files=['app.py']
    a.validation={'passed':False,'status':'skipped_no_reliable_command','evidence_strength':'skipped','decision':{'status':'inconclusive','scope':'candidate','rationale':'no reliable command'}}
    a.validation_status='skipped_no_reliable_command'
    a.validation_results=[a.validation]
    a.review={'decision':'pass','recommended_action':'accept','blockers':[],'score':score,'confidence':confidence}
    a.review_status='passed'
    return a


def test_allowed_actions_do_not_allow_unverified_select_after_two_usable_candidates_by_default(tmp_path):
    s=state(tmp_path, attempts=3)
    s.candidates=[usable_attempt(tmp_path,'candidate_001'), usable_attempt(tmp_path,'candidate_002', score=0.9)]
    s.attempt_observations=[obs('candidate_001','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True'), obs('candidate_002','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True')]
    assert 'ops_select_winner' not in s.allowed_next_actions()
    assert 'ops_run_next_candidate_attempt' in s.allowed_next_actions()


def test_allowed_actions_continue_after_one_unverified_when_budget_remains(tmp_path):
    s=state(tmp_path, attempts=3)
    s.candidates=[usable_attempt(tmp_path,'candidate_001')]
    s.attempt_observations=[obs('candidate_001','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True')]
    assert 'ops_run_next_candidate_attempt' in s.allowed_next_actions()
    assert 'ops_select_winner' not in s.allowed_next_actions()


def test_allowed_actions_allow_one_unverified_under_deadline_pressure(tmp_path):
    s=state(tmp_path, attempts=3)
    s.adaptive_context['deadline_pressure']=True
    s.candidates=[usable_attempt(tmp_path,'candidate_001')]
    s.attempt_observations=[obs('candidate_001','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True')]
    assert 'ops_select_winner' in s.allowed_next_actions()
    assert 'ops_run_next_candidate_attempt' not in s.allowed_next_actions()


def test_recovery_retries_after_two_skipped_validations_when_budget_remains(tmp_path):
    s=state(tmp_path, attempts=3)
    s.candidates=[usable_attempt(tmp_path,'candidate_001', score=0.7), usable_attempt(tmp_path,'candidate_002', score=0.9)]
    s.attempt_observations=[obs('candidate_001','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True'), obs('candidate_002','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True')]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_run_next_candidate_attempt'
    assert rec.action.startswith('focused_retry_')


def test_allowed_actions_allow_unverified_select_when_budget_exhausted(tmp_path):
    s=state(tmp_path, attempts=2)
    s.candidates=[usable_attempt(tmp_path,'candidate_001'), usable_attempt(tmp_path,'candidate_002', score=0.9)]
    s.attempt_observations=[obs('candidate_001','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True'), obs('candidate_002','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True')]
    assert 'ops_select_winner' in s.allowed_next_actions()


def test_explicit_after_two_policy_allows_unverified_early_selection(tmp_path):
    s=state(tmp_path, attempts=3)
    s.unverified_selection_policy='after_two'
    s.candidates=[usable_attempt(tmp_path,'candidate_001'), usable_attempt(tmp_path,'candidate_002', score=0.9)]
    s.attempt_observations=[obs('candidate_001','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True'), obs('candidate_002','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True')]
    assert 'ops_select_winner' in s.allowed_next_actions()


def test_recovery_selects_best_unverified_when_budget_exhausted(tmp_path):
    s=state(tmp_path, attempts=2)
    s.candidates=[usable_attempt(tmp_path,'candidate_001', score=0.7), usable_attempt(tmp_path,'candidate_002', score=0.9)]
    s.attempt_observations=[obs('candidate_001','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True'), obs('candidate_002','partial_progress',validation_snapshot_id='skipped_no_reliable_command:1',review_snapshot_id='passed:0:True')]
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_select_winner'
    assert rec.action == 'select_best_unverified_candidate'
    assert rec.tool_input['selected_attempt_id'] == 'candidate_002'
