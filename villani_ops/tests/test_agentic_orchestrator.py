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
    blocks=[tc('ops_submit_investigation',{'summary':'s','confidence':1.0}),tc('ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':1,'expected_difficulty':'easy','confidence':1.0}),tc('ops_select_execution_path',{'path':'parallel_candidates','reason':'r'}),tc('ops_launch_candidates',{'attempts':1,'reason':'r'}),tc('ops_review_attempt',{'attempt_id':'candidate_001','scope':'candidate'}),tc('ops_select_winner',{'selected_attempt_id':'candidate_001','decision':'select','summary':'s','confidence':1.0}),tc('ops_finalize_run',{'decision':'accepted','summary':'done','selected_attempt_id':'candidate_001'})]
    r=OpsRunner(client=FakeClient(blocks)).run(req(tmp_path,candidate_attempts=1))
    for f in ['state.json','runtime_events.jsonl','event_digest.json','transcript.json','orchestration_graph.json']:
        assert (Path(r.run_dir)/f).exists()
    assert r.state.status=='completed'
