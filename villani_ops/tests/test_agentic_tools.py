from pathlib import Path
from types import SimpleNamespace
from villani_ops.agentic.state import OpsRunState, CandidateAttemptState
from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.state_tooling import execute_tool_with_policy, OpsToolContext

class FakeBackend:
    name='fake-backend'
    model='fake-model'
    max_parallel=1

class FakeRunner:
    name='fake-runner'
    def run_task(self, *, repo_path, task, success_criteria, backend_name, backend_config, timeout_seconds, context, artifacts_dir):
        Path(repo_path, f"{context.get('attempt_id','attempt')}.txt").write_text('done')
        return SimpleNamespace(stdout='ok', stderr='', exit_code=0, telemetry_path=None)

class FakeReviewer:
    name='fake-reviewer'
    def review(self, *, state, attempt, scope):
        return {'decision':'pass','recommended_action':'accept','score':1.0,'summary':'ok','evidence':['reviewed patch'],'issues':[]}

def state(tmp_path):
    repo=tmp_path/'repo'; run=tmp_path/'run'; repo.mkdir(exist_ok=True); run.mkdir(exist_ok=True)
    return OpsRunState(run_id='r',run_dir=str(run),repo_path=str(repo),task='t',mode='performance',runner='villani-code',candidate_attempts=3)
def ctx(tmp_path):
    run=tmp_path/'run'; run.mkdir(exist_ok=True)
    return OpsToolContext(run_dir=run,recorder=OpsEventRecorder(run,'r'),transcript=[],runner_adapter=FakeRunner(),reviewer=FakeReviewer(),backend=FakeBackend(),coding_backend=FakeBackend(),production=False,allow_fake_dependencies=True)

def test_tool_schemas_reject_extra_fields(tmp_path):
    s=state(tmp_path); res=execute_tool_with_policy(s,'ops_submit_investigation',{'summary':'x','confidence':1,'unknown':1},'u',ctx(tmp_path))
    assert res.is_error
    assert s.investigation is None
    assert 'tool_failed' in (tmp_path/'run'/'runtime_events.jsonl').read_text()

def test_investigation_validation_commands_no_format_keyerror(tmp_path):
    s=state(tmp_path); res=execute_tool_with_policy(s,'ops_submit_investigation',{'summary':'x','confidence':1,'validation_plan':{'commands':[{'cmd':'pytest','purpose':'tests'}]}},'u',ctx(tmp_path))
    assert not res.is_error
    assert s.investigation['validation_plan']['commands'][0]['cmd']=='pytest'

def test_finalize_blocked_while_running(tmp_path):
    s=state(tmp_path); s.candidates.append(CandidateAttemptState(attempt_id='a',status='running',scope='candidate'))
    res=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'failed','summary':'x'},'u',ctx(tmp_path))
    assert res.is_error and s.status=='active'

def test_review_pass_cannot_accept_failed_evidence_free_attempt(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.investigation={'summary':'i'}; s.plan={'summary':'p'}; s.execution_path='parallel_candidates'; s.phase='selecting'
    s.candidates.append(CandidateAttemptState(attempt_id='candidate_001',status='failed',scope='candidate',exit_code=1,failure_reason='boom'))
    res=execute_tool_with_policy(s,'ops_review_attempt',{'attempt_id':'candidate_001','scope':'candidate'},'r',c)
    assert not res.is_error
    a=s.candidates[0]
    assert a.review and a.review['decision']=='pass'
    assert a.acceptance_eligible is False
    assert {'runner_failed','missing_patch','empty_changed_files'} <= set(a.acceptance_blockers)
    assert execute_tool_with_policy(s,'ops_select_winner',{'selected_attempt_id':'candidate_001','decision':'select','summary':'unsafe','confidence':1},'sel',c).is_error

def test_validation_failure_blocks_selection_after_passing_review(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.investigation={'summary':'i'}; s.plan={'summary':'p'}; s.execution_path='parallel_candidates'; s.phase='selecting'
    patch=tmp_path/'run'/'diff.patch'; patch.write_text('diff --git a/a.py b/a.py\n')
    s.candidates.append(CandidateAttemptState(attempt_id='candidate_001',status='completed',scope='candidate',exit_code=0,patch_path=str(patch),changed_files=['a.py'],validation={'passed':False,'commands':[{'cmd':'pytest','passed':False,'status':'failed'}]}))
    res=execute_tool_with_policy(s,'ops_review_attempt',{'attempt_id':'candidate_001','scope':'candidate'},'r',c)
    assert not res.is_error
    assert s.candidates[0].acceptance_eligible is False
    assert 'validation_failed' in s.candidates[0].acceptance_blockers

import json
import threading
import time


def test_event_recorder_thread_safe_jsonl(tmp_path):
    rec = OpsEventRecorder(tmp_path / 'run', 'r')
    def write(i):
        for j in range(25):
            rec.record('evt', payload={'i': i, 'j': j})
    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    lines = (tmp_path / 'run' / 'runtime_events.jsonl').read_text().splitlines()
    assert len(lines) == 200
    assert all(json.loads(line)['type'] == 'evt' for line in lines)


class SlowFakeRunner(FakeRunner):
    def run_task(self, **kwargs):
        time.sleep(0.05)
        return super().run_task(**kwargs)


def test_candidate_concurrency_main_thread_state_metadata(tmp_path):
    s = state(tmp_path); c = ctx(tmp_path); c.runner_adapter = SlowFakeRunner(); c.coding_backend.max_parallel = 2
    s.investigation={'summary':'i'}; s.plan={'summary':'p'}; s.execution_path='parallel_candidates'
    res = execute_tool_with_policy(s, 'ops_launch_candidates', {'attempts':3,'reason':'go'}, 'lc', c)
    assert not res.is_error, res.content
    assert len(s.candidates) == 3
    assert s.candidate_concurrency['concurrency_mode'] == 'parallel_candidates'
    assert s.candidate_concurrency['batch_count'] == 2
    assert s.candidate_concurrency['worker_state_mutation'] == 'disabled'


def test_malformed_review_fails_closed(tmp_path):
    class BadReviewer:
        name='fake-bad-reviewer'
        def review(self, **kwargs): return {'decision': 'pass', 'recommended_action': 'accept', 'score': 2}
    s=state(tmp_path); c=ctx(tmp_path); c.reviewer=BadReviewer()
    s.investigation={'summary':'i'}; s.plan={'summary':'p'}; s.execution_path='parallel_candidates'; s.phase='selecting'
    patch=tmp_path/'run'/'diff.patch'; patch.write_text('diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n')
    s.candidates.append(CandidateAttemptState(attempt_id='candidate_001',status='completed',scope='candidate',exit_code=0,patch_path=str(patch),changed_files=['a.py']))
    res=execute_tool_with_policy(s,'ops_review_attempt',{'attempt_id':'candidate_001','scope':'candidate'},'r',c)
    assert not res.is_error
    assert s.candidates[0].review['decision'] == 'fail'
    assert 'review_malformed' in s.candidates[0].acceptance_blockers

class CountingRunner(FakeRunner):
    def __init__(self): self.calls=[]
    def run_task(self, **kwargs):
        self.calls.append(kwargs['context']['attempt_id'])
        return super().run_task(**kwargs)

class FailingThenPassingReviewer:
    name='fake-reviewer'
    def __init__(self): self.calls=[]
    def review(self, *, state, attempt, scope):
        aid=attempt['attempt']['attempt_id']; self.calls.append(aid)
        if aid == 'candidate_001':
            return {'decision':'fail','recommended_action':'retry','score':0.1,'summary':'retry','evidence':['bad'],'issues':['bad'],'blockers':['review_failed']}
        return {'decision':'pass','recommended_action':'accept','score':1.0,'summary':'ok','evidence':['reviewed patch'],'issues':[]}


def test_single_task_path_rejects_parallel_and_allows_sequential_tool(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path)
    s.investigation={'summary':'i'}
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'single_task','should_decompose':False,'candidate_attempts':3,'expected_difficulty':'easy','confidence':1},'p',c)
    bad=execute_tool_with_policy(s,'ops_select_execution_path',{'path':'parallel_candidates','reason':'bad'},'x',c)
    assert bad.is_error
    assert 'use execution_path=single_task' in bad.content
    ok=execute_tool_with_policy(s,'ops_select_execution_path',{'path':'single_task','reason':'ok'},'s',c)
    assert not ok.is_error
    assert s.execution_path == 'single_task'
    assert 'ops_run_single_task_attempts' in s.allowed_next_actions()
    assert 'ops_launch_candidates' not in s.allowed_next_actions()
    launch=execute_tool_with_policy(s,'ops_launch_candidates',{'attempts':3,'reason':'bad'},'l',c)
    assert launch.is_error
    assert 'ops_run_single_task_attempts' in launch.content


def test_single_task_attempts_retry_sequentially_and_stop_once_accepted(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path)
    runner=CountingRunner(); reviewer=FailingThenPassingReviewer()
    c.runner_adapter=runner; c.reviewer=reviewer
    s.investigation={'summary':'i','validation_plan':{'commands':[{'cmd':'python -c "import sys; sys.exit(0)"','purpose':'ok'}]}}
    s.plan={'summary':'p','strategy':'single_task','candidate_attempts':3}
    s.execution_path='single_task'
    res=execute_tool_with_policy(s,'ops_run_single_task_attempts',{'attempts':3,'reason':'go'},'seq',c)
    assert not res.is_error, res.content
    assert runner.calls == ['candidate_001','candidate_002']
    assert len(s.candidates) == 2
    assert s.candidates[0].acceptance_eligible is False
    assert s.candidates[1].acceptance_eligible is True
    assert s.selection and s.selection['selected_attempt_id'] == 'candidate_002'
    assert s.attempts_requested == 3
    assert s.attempts_started == 2
    assert s.stopped_early is True
    assert s.stop_reason == 'accepted_attempt'
    events=(tmp_path/'run'/'runtime_events.jsonl').read_text()
    assert events.index('candidate_001') < events.index('candidate_002')


def test_recovery_recommends_single_task_path_and_runner(tmp_path):
    from villani_ops.agentic.recovery import recommend_next_agentic_action
    s=state(tmp_path); s.investigation={'summary':'i'}; s.plan={'strategy':'single_task'}
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_select_execution_path'
    assert rec.tool_input['path'] == 'single_task'
    s.execution_path='single_task'
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name == 'ops_run_single_task_attempts'
