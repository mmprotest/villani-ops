from pathlib import Path
from types import SimpleNamespace

from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.state import OpsRunState, SubtaskState, AttemptObservation
from villani_ops.agentic.state_tooling import OpsToolContext, execute_tool_with_policy
from villani_ops.agentic.tools import build_adaptive_subtask_runner_prompt, h_observe_completed_attempt, OpsObserveCompletedAttemptInput
from villani_ops.core.backend import Backend


class SubtaskRunner:
    def __init__(self): self.calls=[]
    def run_task(self, *, repo_path, task, success_criteria, backend_name, backend_config, timeout_seconds, context, artifacts_dir):
        self.calls.append({'attempt_id': context['attempt_id'], 'subtask_id': context.get('subtask_id'), 'task': task})
        Path(repo_path, 'src/pricing.py').parent.mkdir(exist_ok=True)
        Path(repo_path, 'src/pricing.py').write_text(f'# {context["attempt_id"]}\n')
        return SimpleNamespace(stdout='ok', stderr='', exit_code=0, telemetry_path=None, total_file_reads=1, total_file_writes=1)


class FailReviewer:
    def review(self, *, state, attempt, scope):
        assert scope == 'subtask'
        assert attempt['scope'] == 'subtask'
        assert 'subtask_review_criteria' in attempt
        return {'decision':'fail','recommended_action':'retry','score':0.2,'summary':'discount order is wrong','evidence':['coupon after tax'],'issues':['ordering'],'blockers':['discount_order_wrong']}


def make_state(tmp_path):
    repo=tmp_path/'repo'; run=tmp_path/'run'; repo.mkdir(); run.mkdir()
    (repo/'tests').mkdir(); (repo/'tests'/'test_pricing.py').write_text('def test_x():\n    assert False\n')
    return OpsRunState(run_id='r',run_dir=str(run),repo_path=str(repo),task='fix parser and pricing',success_criteria='all behaviours pass',mode='performance',runner='villani-code',candidate_attempts=3,investigation={'summary':'multi','validation_plan':{'commands':[{'cmd':'python -c "import sys; sys.exit(0)"','purpose':'global'}]}},plan={'summary':'decompose','strategy':'decompose_then_execute'},decomposition={'merge_strategy':'merge minimal patches'},decomposition_validated=True,decomposition_accepted=True,execution_path='decomposed_subtasks',subtasks=[SubtaskState(subtask_id='pricing',title='pricing',objective='fix pricing only',relevant_files=['src/pricing.py'])])


def make_ctx(tmp_path, runner=None, reviewer=None):
    run=tmp_path/'run'
    b=Backend(name='b1',provider='local',model='m1',api_key='x',roles=['coding'])
    return OpsToolContext(run_dir=run,recorder=OpsEventRecorder(run,'r'),transcript=[],runner_adapter=runner or SubtaskRunner(),reviewer=reviewer or FailReviewer(),backend=b,backend_name='b1',coding_backend=b,coding_backend_name='b1',backends={'b1':b},production=False,allow_fake_dependencies=True)


def test_ops_run_next_subtask_attempt_runs_one_and_observes(tmp_path):
    s=make_state(tmp_path); runner=SubtaskRunner(); c=make_ctx(tmp_path, runner)
    res=execute_tool_with_policy(s,'ops_run_next_subtask_attempt',{'subtask_id':'pricing','reason':'first'},'x',c)
    assert not res.is_error, res.content
    assert len(runner.calls) == 1
    assert len(s.subtasks[0].attempts) == 1
    assert len(s.attempt_observations) == 1
    obs=s.attempt_observations[0]
    assert obs.scope == 'subtask'
    assert obs.subtask_id == 'pricing'
    assert obs.outcome in {'validation_failed','review_failed'}
    assert s.backend_assessments['b1']['attempts'] == 1


def test_subtask_retry_prompt_includes_first_failure_and_do_not_repeat(tmp_path):
    s=make_state(tmp_path); runner=SubtaskRunner(); c=make_ctx(tmp_path, runner)
    execute_tool_with_policy(s,'ops_run_next_subtask_attempt',{'subtask_id':'pricing','reason':'first'},'x',c)
    prompt=build_adaptive_subtask_runner_prompt(s, s.subtasks[0], reason='retry', repair=True, base_attempt_id='pricing_attempt_001')
    assert 'PREVIOUS SUBTASK ATTEMPT LEARNING' in prompt
    assert 'pricing_attempt_001' in prompt
    assert 'discount_order_wrong' in prompt
    assert 'Do not repeat previous validation, review, patch hygiene, or scope mistakes' in prompt
    res=execute_tool_with_policy(s,'ops_run_next_subtask_attempt',{'subtask_id':'pricing','base_attempt_id':'pricing_attempt_001','repair':True,'reason':'retry'},'y',c)
    assert not res.is_error, res.content
    assert len(runner.calls) == 2
    assert 'PREVIOUS SUBTASK ATTEMPT LEARNING' in runner.calls[1]['task']


def test_subtask_observation_refresh_idempotent_and_distinct_attempts_count(tmp_path):
    s=make_state(tmp_path); c=make_ctx(tmp_path)
    execute_tool_with_policy(s,'ops_run_next_subtask_attempt',{'subtask_id':'pricing','reason':'first'},'x',c)
    aid=s.subtasks[0].attempts[0].attempt_id
    h_observe_completed_attempt(s, OpsObserveCompletedAttemptInput(attempt_id=aid, reason='refresh'), c)
    assert len(s.attempt_observations) == 1
    assert s.backend_assessments['b1']['attempts'] == 1
    execute_tool_with_policy(s,'ops_run_next_subtask_attempt',{'subtask_id':'pricing','reason':'second'},'y',c)
    assert len(s.attempt_observations) == 2
    assert s.backend_assessments['b1']['attempts'] == 2


def test_deadlock_fallback_starts_and_immediately_launches_candidate_with_learnings(tmp_path):
    s=make_state(tmp_path); runner=SubtaskRunner(); c=make_ctx(tmp_path, runner)
    st=s.subtasks[0]; st.status='failed'
    st.attempts=[]
    s.attempt_observations=[AttemptObservation(attempt_id='pricing_attempt_001',scope='subtask',subtask_id='pricing',backend_name='b1',outcome='review_failed',evidence=['coupon after tax'],blockers=['discount_order_wrong'])]
    s.decomposed_execution_status='blocked'; s.decomposed_execution_blockers=['decomposition_deadlocked']
    # Make deadlock detector true by adding an exhausted failed attempt shell.
    from villani_ops.agentic.state import CandidateAttemptState
    st.attempts=[CandidateAttemptState(attempt_id=f'pricing_attempt_{i:03d}',status='failed',scope='subtask',subtask_id='pricing') for i in range(1,4)]
    res=execute_tool_with_policy(s,'ops_start_candidate_fallback',{'reason':'deadlock'},'fb',c)
    assert not res.is_error, res.content
    assert len(s.candidates) == 1
    assert runner.calls[-1]['subtask_id'] is None
    assert 'DECOMPOSITION FALLBACK CONTEXT' in runner.calls[-1]['task']
    assert 'discount_order_wrong' in runner.calls[-1]['task']
    assert 'ops_launch_candidates' not in s.allowed_next_actions()
