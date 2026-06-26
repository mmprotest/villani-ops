from villani_ops.agentic.recovery import recommend_next_agentic_action
from villani_ops.agentic.state import OpsRunState, CandidateAttemptState, AttemptObservation


def state(tmp_path, attempts=3):
    repo=tmp_path/'repo'; run=tmp_path/'run'; repo.mkdir(); run.mkdir()
    return OpsRunState(run_id='r',run_dir=str(run),repo_path=str(repo),task='fix bug',mode='performance',runner='villani-code',candidate_attempts=attempts,investigation={'summary':'i'},plan={'strategy':'single_task'},execution_path='single_task',phase='running_candidates')


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
