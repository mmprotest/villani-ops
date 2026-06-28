from types import SimpleNamespace
import json
from pathlib import Path
from villani_ops.agentic.runner import OpsRunner, OpsRunRequest

class FakeResp(SimpleNamespace): pass
class FakeClient:
    def __init__(self, blocks): self.blocks=list(blocks)
    def create_message(self, **kw):
        content=self.blocks.pop(0) if self.blocks else []
        return FakeResp(content=content, finish_reason='stop')
def tc(name,input,id='1'): return [{'type':'tool_use','id':id,'name':name,'input':input}]

class FakeBackend:
    name='fake-backend'
    model='fake-model'
    max_parallel=1
    def create_message(self, **kwargs):
        return FakeResp(content=[], finish_reason='stop')

class FakeRunner:
    name='fake-runner'
    def run_task(self, *, repo_path, task, success_criteria, backend_name, backend_config, timeout_seconds, context, artifacts_dir):
        Path(repo_path, 'agentic_result.txt').write_text('done')
        return SimpleNamespace(stdout='changed agentic_result.txt', stderr='', exit_code=0, telemetry_path=None)

class FakeReviewer:
    name='fake-reviewer'
    def review(self, *, state, attempt, scope):
        return {'decision':'pass','recommended_action':'accept','score':1.0,'summary':'ok','evidence':['patch reviewed'],'issues':[]}

def req(tmp_path, **kw):
    base=dict(repo_path=str(tmp_path),task='t',workspace=str(tmp_path/'.v'),production=False,allow_fake_dependencies=True,backend=FakeBackend(),runner_adapter=FakeRunner(),reviewer=FakeReviewer())
    base.update(kw)
    return OpsRunRequest(**base)

def test_prose_only_model_response_triggers_recovery_and_no_progress_fails(tmp_path):
    r=OpsRunner(client=FakeClient([[{'type':'text','text':'I will plan'}],[]]), max_recovery_attempts=1).run(req(tmp_path))
    assert r.state.status=='failed'
    assert 'agentic_orchestrator_no_progress' in json.loads((Path(r.run_dir)/'state.json').read_text())['final_decision']['summary']
    events=(Path(r.run_dir)/'runtime_events.jsonl').read_text()
    assert 'recovery_injected' in events

def test_artifacts_written(tmp_path):
    blocks=[tc('ops_submit_investigation',{'summary':'s','confidence':1.0}),tc('ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':1,'expected_difficulty':'easy','confidence':1.0}),tc('ops_select_execution_path',{'path':'parallel_candidates','reason':'r'}),tc('ops_launch_candidates',{'attempts':1,'reason':'r'}),tc('ops_review_attempt',{'attempt_id':'candidate_001','scope':'candidate'}),tc('ops_run_validation',{'target':'candidate','target_id':'candidate_001','commands':[{'cmd':'python -c "print(1)"','source':'user_provided','confidence':'high','authority':'acceptance_blocking','blocking':True}]}),tc('ops_select_winner',{'selected_attempt_id':'candidate_001','decision':'select','summary':'s','confidence':1.0}),tc('ops_finalize_run',{'decision':'accepted','summary':'done','selected_attempt_id':'candidate_001'})]
    r=OpsRunner(client=FakeClient(blocks)).run(req(tmp_path,candidate_attempts=1))
    for f in ['state.json','runtime_events.jsonl','event_digest.json','transcript.json','orchestration_graph.json']:
        assert (Path(r.run_dir)/f).exists()
    assert r.state.status=='completed'

class FakeSubtaskRunner:
    name='fake-subtask-runner'
    def run_task(self, *, repo_path, task, success_criteria, backend_name, backend_config, timeout_seconds, context, artifacts_dir):
        Path(repo_path, f"{context['subtask_id']}.txt").write_text('done')
        return SimpleNamespace(stdout='changed subtask file', stderr='', exit_code=0, telemetry_path=None)

def test_decomposed_smoke_with_explicit_nonproduction_fakes(tmp_path):
    blocks=[
        tc('ops_submit_investigation',{'summary':'s','confidence':1.0}),
        tc('ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'decomposition_reason':'independent files','candidate_attempts':1,'expected_difficulty':'medium','confidence':1.0}),
        tc('ops_submit_decomposition',{'should_use_decomposition':True,'reason':'split','confidence':1.0,'subtasks':[
            {'id':'s0','title':'s0','objective':'make s0','success_criteria':'s0 file','relevant_files':[],'dependencies':[],'expected_difficulty':'easy','risk':'low','confidence':1.0,'can_run_parallel':True},
            {'id':'s1','title':'s1','objective':'make s1','success_criteria':'s1 file','relevant_files':[],'dependencies':['s0'],'expected_difficulty':'easy','risk':'low','confidence':1.0,'can_run_parallel':False}], 'merge_strategy':'dependency order'}),
        tc('ops_validate_decomposition',{'decomposition_id':'current','semantic':False}),
        tc('ops_select_execution_path',{'path':'decomposed_subtasks','reason':'validated'}),
        tc('ops_run_next_subtask_attempt',{'subtask_id':'s0','reason':'go'}),
        tc('ops_run_next_subtask_attempt',{'subtask_id':'s1','reason':'go'}),
        tc('ops_integrate_subtasks',{'reason':'merge accepted subtasks'}),
        tc('ops_review_attempt',{'attempt_id':'integration_001','scope':'integration'}),
        tc('ops_run_validation',{'target':'integration','commands':[{'cmd':'python -c \"print(1)\"'}]}),
        tc('ops_select_winner',{'selected_attempt_id':'integration_001','decision':'select','summary':'integrated','confidence':1.0}),
        tc('ops_finalize_run',{'decision':'accepted','summary':'done','selected_attempt_id':'integration_001'}),
    ]
    repo=tmp_path/'repo'; repo.mkdir()
    r=OpsRunner(client=FakeClient(blocks)).run(req(tmp_path,candidate_attempts=1,repo_path=str(repo),runner_adapter=FakeSubtaskRunner()))
    assert r.state.status=='completed'
    assert r.state.integration['applied_subtask_order'] == ['s0','s1']
    assert sum(len(st.attempts) for st in r.state.subtasks) == 2
    assert 'ops_launch_subtasks' not in r.state.allowed_next_actions()

def decomposed_prefix_blocks():
    return [
        tc('ops_submit_investigation',{'summary':'s','confidence':1.0}),
        tc('ops_submit_plan',{'summary':'p','strategy':'decompose_then_execute','should_decompose':True,'decomposition_reason':'independent files','candidate_attempts':1,'expected_difficulty':'medium','confidence':1.0}),
        tc('ops_submit_decomposition',{'should_use_decomposition':True,'reason':'split','confidence':1.0,'subtasks':[
            {'id':'s0','title':'s0','objective':'make s0','success_criteria':'s0 file','relevant_files':[],'dependencies':[],'expected_difficulty':'easy','risk':'low','confidence':1.0,'can_run_parallel':True},
            {'id':'s1','title':'s1','objective':'make s1','success_criteria':'s1 file','relevant_files':[],'dependencies':['s0'],'expected_difficulty':'easy','risk':'low','confidence':1.0,'can_run_parallel':False}], 'merge_strategy':'dependency order'}),
        tc('ops_validate_decomposition',{'decomposition_id':'current','semantic':False}),
    ]

def test_no_tool_call_after_accepted_decomposition_selects_path_deterministically(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir()
    r=OpsRunner(client=FakeClient(decomposed_prefix_blocks()+[[]]), max_turns=5, max_recovery_attempts=0).run(req(tmp_path,candidate_attempts=1,repo_path=str(repo),runner_adapter=FakeSubtaskRunner()))
    assert r.state.execution_path == 'decomposed_subtasks'
    assert (r.state.final_decision or {}).get('blockers') != ['agentic_orchestrator_no_progress']
    events=(Path(r.run_dir)/'runtime_events.jsonl').read_text()
    assert 'recovery_deterministic_action_executed' in events
    assert 'agentic_orchestrator_no_progress' not in events
    assert r.state.recovery_count == 0

def test_no_tool_call_after_decomposed_path_launches_ready_subtasks(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir()
    blocks=decomposed_prefix_blocks()+[
        tc('ops_select_execution_path',{'path':'decomposed_subtasks','reason':'validated'}),
        [],
    ]
    r=OpsRunner(client=FakeClient(blocks), max_turns=6, max_recovery_attempts=0).run(req(tmp_path,candidate_attempts=1,repo_path=str(repo),runner_adapter=FakeSubtaskRunner()))
    assert r.state.subtasks[0].attempts
    assert [s.subtask_id for s in r.state.subtasks if s.attempts] in (['s0'], ['s0','s1'])
    assert all(len(st.attempts) <= 1 for st in r.state.subtasks)
    assert (r.state.final_decision or {}).get('blockers') != ['agentic_orchestrator_no_progress']
    events=(Path(r.run_dir)/'runtime_events.jsonl').read_text()
    assert 'ops_run_next_subtask_attempt' in events
    assert 'agentic_orchestrator_no_progress' not in events

class TimeoutClient:
    def __init__(self, first=None): self.first=first; self.calls=0
    def create_message(self, **kw):
        self.calls += 1
        if self.first and self.calls == 1:
            return FakeResp(content=self.first, finish_reason='stop')
        raise TimeoutError('HTTP read timeout')


def test_model_call_read_timeout_writes_terminal_failed_state(tmp_path):
    r=OpsRunner(client=TimeoutClient(), max_turns=3).run(req(tmp_path))
    state=json.loads((Path(r.run_dir)/'state.json').read_text())
    assert r.state.status == 'failed'
    assert state['status'] == 'failed'
    assert state['phase'] == 'failed'
    assert state['final_decision']['terminal_state'] == 'timed_out'
    assert 'backend_timeout' in state['final_decision']['blockers']
    assert 'model_request_failed' in (Path(r.run_dir)/'runtime_events.jsonl').read_text()


def test_backend_timeout_after_completed_candidate_selects_unverified_best(tmp_path):
    blocks=tc('ops_submit_investigation',{'summary':'s','confidence':1.0})
    r=OpsRunner(client=TimeoutClient(first=blocks), max_turns=3).run(req(tmp_path))
    assert r.state.status == 'failed'
    # Now use a completed candidate path before the timeout.
    blocks=[
        tc('ops_submit_investigation',{'summary':'s','confidence':1.0}),
        tc('ops_submit_plan',{'summary':'p','strategy':'single_task','should_decompose':False,'candidate_attempts':1,'expected_difficulty':'easy','confidence':1.0}),
        tc('ops_select_execution_path',{'path':'single_task','reason':'r'}),
        tc('ops_run_next_candidate_attempt',{'reason':'go'}),
    ]
    class LaterTimeout(FakeClient):
        def create_message(self, **kw):
            if self.blocks:
                return super().create_message(**kw)
            raise TimeoutError('backend read timeout')
    r=OpsRunner(client=LaterTimeout(blocks), max_turns=10).run(req(tmp_path/'x', candidate_attempts=1))
    assert r.state.status == 'completed'
    assert r.state.selection['selected_attempt_id'] == 'candidate_001'
    assert r.state.final_decision['decision_bucket'] == 'accepted_unverified'
    assert r.state.final_decision['materialization_signal'] == 'unverified_best_candidate'
