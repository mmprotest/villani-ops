from pathlib import Path
from villani_ops.tests.test_agentic_tools import state, ctx
from villani_ops.agentic.state import OpsRunState
from villani_ops.agentic.artifacts import derive_graph
from villani_ops.agentic.state_tooling import execute_tool_with_policy

def test_state_reload_does_not_duplicate_completed_work(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path)
    execute_tool_with_policy(s,'ops_submit_investigation',{'summary':'i','confidence':1},'i',c)
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':1,'expected_difficulty':'easy','confidence':1},'p',c)
    s.save(tmp_path/'state.json'); loaded=OpsRunState.load(tmp_path/'state.json')
    assert 'ops_submit_investigation' not in loaded.allowed_next_actions()
    assert 'ops_select_execution_path' in loaded.allowed_next_actions()

def test_graph_is_derived_not_truth(tmp_path):
    s=state(tmp_path); g=derive_graph(s,[])
    assert g['canonical']=='state.json'
    assert s.allowed_next_actions() != []

from villani_ops.agentic.artifacts import write_json_utf8, read_json_utf8, write_text_utf8
from villani_ops.agentic.event_recorder import OpsEventRecorder
import json, threading


def test_state_save_load_utf8_unicode_atomic(tmp_path):
    s=state(tmp_path)
    s.warnings.append('unicode ✅ ❌ → “quotes” café 日本語')
    path=tmp_path/'nested'/'state.json'
    s.save(path)
    raw=path.read_bytes()
    assert '✅'.encode('utf-8') in raw
    loaded=OpsRunState.load(path)
    assert loaded.warnings == s.warnings
    assert not path.with_name(path.name+'.tmp').exists()


def test_empty_state_reports_helpful_error(tmp_path):
    path=tmp_path/'state.json'
    path.write_text('', encoding='utf-8')
    try:
        OpsRunState.load(path)
    except ValueError as e:
        assert 'state.json is empty or corrupted' in str(e)
    else:
        assert False, 'expected ValueError'


def test_json_helpers_utf8_atomic(tmp_path):
    path=tmp_path/'artifacts'/'data.json'
    write_json_utf8(path, {'message':'✅ café 日本語'}, atomic=True)
    assert read_json_utf8(path)['message'] == '✅ café 日本語'
    assert '✅' in path.read_text(encoding='utf-8')


def test_event_recorder_concurrent_utf8_jsonl(tmp_path):
    rec=OpsEventRecorder(tmp_path, 'run')
    def worker(i):
        rec.record('unicode_event', payload={'i':i, 'text':'✅ café'})
    threads=[threading.Thread(target=worker, args=(i,)) for i in range(25)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines=(tmp_path/'runtime_events.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(lines) == 25
    parsed=[json.loads(line) for line in lines]
    assert all(p['payload']['text'] == '✅ café' for p in parsed)
