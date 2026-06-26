from pathlib import Path
from types import SimpleNamespace

from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.state import OpsRunState
from villani_ops.agentic.state_tooling import OpsToolContext, execute_tool_with_policy
from villani_ops.agentic.tools import build_candidate_runner_prompt
from villani_ops.core.backend import Backend


def make_state(tmp_path):
    repo=tmp_path/'repo'; run=tmp_path/'run'; repo.mkdir(exist_ok=True); run.mkdir(exist_ok=True)
    return OpsRunState(run_id='r',run_dir=str(run),repo_path=str(repo),task='fix checkout rollback',success_criteria='tests pass',mode='performance',runner='villani-code',candidate_attempts=3,investigation={'summary':'checkout failure','validation_plan':{'commands':[{'cmd':'python -c "import sys; sys.exit(1)"','purpose':'failing test'}]}},plan={'summary':'single','strategy':'single_task'},execution_path='single_task')

class TelemetryRunner:
    name='fake-runner'
    def __init__(self): self.calls=[]
    def run_task(self, *, repo_path, task, success_criteria, backend_name, backend_config, timeout_seconds, context, artifacts_dir):
        self.calls.append({'backend_name':backend_name,'backend_config':backend_config,'task':task})
        Path(repo_path,'cart.py').write_text(f'# {context["attempt_id"]}\n')
        return SimpleNamespace(stdout='ok',stderr='',exit_code=0,telemetry_path=None,model_requests=1,model_failures=0,total_tool_calls=3,tool_calls_by_name={'read':1,'write':1,'cmd':1},total_file_reads=1,total_file_writes=1,commands_executed=1,commands_failed=1,first_substantive_file_read_tool_index=1,first_substantive_file_read_seconds=0.1,first_file_mutation_tool_index=2,first_file_mutation_seconds=0.2,first_command_tool_index=3,first_command_seconds=0.3,token_accounting_status='available',token_accounting_warnings=[],telemetry={'x':1},debug_artifact_dir=str(artifacts_dir),resolved_trace_dir=str(artifacts_dir),input_tokens=10,output_tokens=5,total_tokens=15,total_cost=0.01)

class FailReviewer:
    name='fake-reviewer'
    def review(self, *, state, attempt, scope):
        return {'decision':'fail','recommended_action':'retry','score':0.2,'summary':'Patch handles receipt rendering but not rollback path.','evidence':['rollback still broken'],'issues':['missing rollback'],'blockers':['rollback_not_fixed']}

def make_ctx(tmp_path, runner=None):
    run=tmp_path/'run'; run.mkdir(exist_ok=True)
    b1=Backend(name='b1',provider='local',model='m1',api_key='x',roles=['coding'])
    b2=Backend(name='b2',provider='local',model='m2',api_key='x',roles=['coding'])
    return OpsToolContext(run_dir=run,recorder=OpsEventRecorder(run,'r'),transcript=[],runner_adapter=runner or TelemetryRunner(),reviewer=FailReviewer(),backend=b1,backend_name='b1',coding_backend=b1,coding_backend_name='b1',backends={'b1':b1,'b2':b2},production=False,allow_fake_dependencies=True)

def test_next_candidate_attempt_runs_one_and_observes_with_telemetry(tmp_path):
    s=make_state(tmp_path); runner=TelemetryRunner(); c=make_ctx(tmp_path, runner)
    res=execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'reason':'first try'},'a1',c)
    assert not res.is_error, res.content
    assert len(s.candidates)==1
    assert runner.calls[0]['backend_name']=='b1'
    assert s.candidates[0].runner_telemetry['model_requests']==1
    assert len(s.attempt_observations)==1
    assert s.attempt_observations[0].outcome in {'validation_failed','review_failed'}
    assert s.backend_assessments['b1']['attempts']==1
    assert 'ops_run_next_candidate_attempt' in s.allowed_next_actions()


def test_second_attempt_prompt_contains_curated_learning_and_do_not_repeat(tmp_path):
    s=make_state(tmp_path); runner=TelemetryRunner(); c=make_ctx(tmp_path, runner)
    execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'reason':'first try'},'a1',c)
    prompt=build_candidate_runner_prompt(s, reason='focused retry')
    assert 'PREVIOUS ATTEMPT LEARNING' in prompt
    assert 'Attempt candidate_001 failed' in prompt
    assert 'cart.py' in prompt
    assert 'Do differently:' in prompt
    assert 'Do not repeat previously rejected broad rewrites' in prompt
    res=execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'reason':'focused retry'},'a2',c)
    assert not res.is_error, res.content
    assert len(s.candidates)==2
    assert 'PREVIOUS ATTEMPT LEARNING' in runner.calls[1]['task']


def test_backend_name_selects_actual_backend_and_unknown_errors(tmp_path):
    s=make_state(tmp_path); runner=TelemetryRunner(); c=make_ctx(tmp_path, runner)
    bad=execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'backend_name':'missing','reason':'try missing'},'bad',c)
    assert bad.is_error
    assert "unknown coding backend 'missing'" in bad.content
    ok=execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'backend_name':'b2','reason':'try b2'},'ok',c)
    assert not ok.is_error, ok.content
    assert runner.calls[-1]['backend_name']=='b2'
    assert runner.calls[-1]['backend_config'].name=='b2'


def test_next_candidate_attempt_rejects_after_budget_exhausted(tmp_path):
    s=make_state(tmp_path); s.candidate_attempts=1; runner=TelemetryRunner(); c=make_ctx(tmp_path, runner)
    first=execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'reason':'only try'},'a1',c)
    assert not first.is_error, first.content
    assert 'ops_run_next_candidate_attempt' not in s.allowed_next_actions()
    second=execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'reason':'too many'},'a2',c)
    assert second.is_error
    assert 'budget exhausted' in second.content


def test_observation_refreshes_after_later_validation_and_review_without_duplicates(tmp_path):
    from villani_ops.agentic.tools import h_observe_completed_attempt, OpsObserveCompletedAttemptInput, h_validation, OpsRunValidationInput, ValidationCommand, h_review_attempt, OpsReviewAttemptInput
    s=make_state(tmp_path); runner=TelemetryRunner(); c=make_ctx(tmp_path, runner)
    res=execute_tool_with_policy(s,'ops_run_next_candidate_attempt',{'reason':'first try'},'a1',c)
    assert not res.is_error, res.content
    aid=s.candidates[0].attempt_id
    original=s.attempt_observations[0]
    s.candidates[0].review=None; s.candidates[0].review_status='not_run'; s.candidates[0].review_retry_count=0
    h_observe_completed_attempt(s, OpsObserveCompletedAttemptInput(attempt_id=aid, reason='clear review snapshot'), c)
    original=s.attempt_observations[0]
    assert len(s.attempt_observations)==1
    h_observe_completed_attempt(s, OpsObserveCompletedAttemptInput(attempt_id=aid, reason='again'), c)
    assert len(s.attempt_observations)==1
    assert s.attempt_observations[0].attempt_id == aid
    # Add a later validation result; existing observation should be replaced with a fresh snapshot.
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=aid, commands=[ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', purpose='later pass')]), c)
    assert len(s.attempt_observations)==1
    after_validation=s.attempt_observations[0]
    assert after_validation.validation_snapshot_id != original.validation_snapshot_id
    # Add a later review; existing observation should again be replaced, not duplicated.
    before_review=after_validation.review_snapshot_id
    h_review_attempt(s, OpsReviewAttemptInput(attempt_id=aid, scope='candidate'), c)
    assert len(s.attempt_observations)==1
    assert s.attempt_observations[0].review_snapshot_id != before_review
