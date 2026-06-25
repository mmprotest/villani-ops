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

def test_prose_only_model_response_triggers_recovery_and_no_progress_fails(tmp_path):
    r=OpsRunner(client=FakeClient([[{'type':'text','text':'I will plan'}],[]]), max_recovery_attempts=1).run(OpsRunRequest(repo_path=str(tmp_path),task='t',workspace=str(tmp_path/'.v')))
    assert r.state.status=='failed'
    assert 'agentic_orchestrator_no_progress' in json.loads((Path(r.run_dir)/'state.json').read_text())['final_decision']['summary']
    events=(Path(r.run_dir)/'runtime_events.jsonl').read_text()
    assert 'recovery_injected' in events

def test_artifacts_written(tmp_path):
    blocks=[tc('ops_submit_investigation',{'summary':'s','confidence':1.0}),tc('ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':1,'expected_difficulty':'easy','confidence':1.0}),tc('ops_select_execution_path',{'path':'parallel_candidates','reason':'r'}),tc('ops_launch_candidates',{'attempts':1,'reason':'r'}),tc('ops_select_winner',{'selected_attempt_id':'candidate_001','decision':'select','summary':'s','confidence':1.0}),tc('ops_finalize_run',{'decision':'accepted','summary':'done','selected_attempt_id':'candidate_001'})]
    r=OpsRunner(client=FakeClient(blocks)).run(OpsRunRequest(repo_path=str(tmp_path),task='t',workspace=str(tmp_path/'.v'),candidate_attempts=1))
    for f in ['state.json','runtime_events.jsonl','event_digest.json','transcript.json','orchestration_graph.json']:
        assert (Path(r.run_dir)/f).exists()
    assert r.state.status=='completed'
