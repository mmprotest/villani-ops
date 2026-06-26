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
