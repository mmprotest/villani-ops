from __future__ import annotations
import json, urllib.request, pytest
from pathlib import Path
from typer.testing import CliRunner
from villani_ops.cli.main import app
from villani_ops.viewer.adapter import build_viewer_snapshot
from villani_ops.viewer.builder import write_offline_viewer
from villani_ops.viewer.server import ViewerServer, safe_join_under


def fake_run(tmp_path: Path):
    rd=tmp_path/'runs'/'20260626T000000Z-abc123'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':rd.name,'task':'Fix tests','status':'running','mode':'performance','runner':'villani-code','backend_model':'qwen35b','api_key':'secret'}))
    (rd/'runtime_events.jsonl').write_text('\n'.join([
        json.dumps({'event_id':'1','timestamp':'2026-06-26T00:00:00+00:00','type':'run_started','payload':{}}),
        '{not json}',
        json.dumps({'event_id':'2','timestamp':'2026-06-26T00:00:01+00:00','type':'candidate_attempt_started','payload':{'attempt_id':'candidate_001'}}),
        json.dumps({'event_id':'3','timestamp':'2026-06-26T00:00:02+00:00','type':'validation_completed','payload':{'attempt_id':'candidate_001','duration_seconds':1.2}}),
    ]))
    (rd/'usage.json').write_text(json.dumps({'input_tokens':10,'output_tokens':5,'total_tokens':15,'total_cost':0.01}))
    return rd


def test_snapshot_builder_handles_events_usage_graph_and_redaction(tmp_path):
    rd=fake_run(tmp_path)
    snap=build_viewer_snapshot(rd)
    assert snap['run']['run_id']==rd.name
    assert snap['usage']['total_tokens']==15
    assert any(e['type']=='candidate_attempt_started' for e in snap['timeline'])
    assert any(n['id']=='candidate_001' for n in snap['graph']['nodes'])
    assert 'secret' not in json.dumps(snap).lower()


def test_snapshot_builder_missing_usage_and_graph(tmp_path):
    rd=fake_run(tmp_path); (rd/'usage.json').unlink()
    snap=build_viewer_snapshot(rd)
    assert snap['usage']['total_tokens']==0
    assert snap['graph']['nodes']


def test_offline_viewer_embeds_self_contained_snapshot(tmp_path):
    rd=fake_run(tmp_path)
    out=write_offline_viewer(rd)
    text=out.read_text()
    assert out == rd/'viewer'/'index.html'
    assert 'villani-run-snapshot' in text
    assert 'https://' not in text and 'cdn' not in text.lower()
    assert '<style>' in text and '<script>' in text


def test_server_snapshot_and_path_safety(tmp_path):
    rd=fake_run(tmp_path)
    with pytest.raises(ValueError): safe_join_under(tmp_path, '../escape')
    srv=ViewerServer(tmp_path/'runs', port=18765).start(try_ports=1)
    try:
        assert srv.host=='127.0.0.1'
        data=json.loads(urllib.request.urlopen(srv.url(rd.name).replace('/runs/','/api/runs/') + '/snapshot').read())
        assert data['run']['run_id']==rd.name
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f'http://127.0.0.1:{srv.port}/api/runs/../snapshot')
        assert e.value.code==404
    finally:
        srv.stop()


def test_viewer_list_cli(tmp_path):
    rd=fake_run(tmp_path/'.villani-ops')
    result=CliRunner().invoke(app, ['viewer','list','--workspace', str(tmp_path/'.villani-ops')])
    assert result.exit_code==0
    assert rd.name in result.output


def test_viewer_progress_starts_low_and_finalizes(tmp_path):
    rd=tmp_path/'runs'/'early'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'early','status':'running'}))
    (rd/'runtime_events.jsonl').write_text(json.dumps({'timestamp':'2026-06-26T00:00:00+00:00','type':'run_started','payload':{}}))
    snap=build_viewer_snapshot(rd)
    assert snap['run']['progress_percent'] == 5
    (rd/'runtime_events.jsonl').write_text('\n'.join([
        json.dumps({'timestamp':'2026-06-26T00:00:00+00:00','type':'run_started','payload':{}}),
        json.dumps({'timestamp':'2026-06-26T00:00:01+00:00','type':'run_finalized','payload':{}}),
    ]))
    assert build_viewer_snapshot(rd)['run']['progress_percent'] == 100


def test_viewer_usage_model_from_cost_summary_and_usage_record(tmp_path):
    rd=tmp_path/'runs'/'usage'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'usage','status':'running'}))
    (rd/'runtime_events.jsonl').write_text(json.dumps({'timestamp':'2026-06-26T00:00:00+00:00','type':'run_started','payload':{'model':'qwen35b'}}))
    (rd/'cost_summary.json').write_text(json.dumps({'input_tokens':58342,'output_tokens':27615,'total_tokens':85957,'total_cost':0.1372}))
    snap=build_viewer_snapshot(rd)
    assert snap['run']['model'] == 'qwen35b'
    assert snap['usage']['input_tokens'] == 58342
    assert snap['usage']['output_tokens'] == 27615
    assert snap['usage']['total_cost'] == 0.1372
    assert snap['run']['run_id_short']
    assert snap['run']['run_dir_short'].endswith('/usage')


def test_viewer_timeline_order_labels_and_status(tmp_path):
    rd=tmp_path/'runs'/'timeline'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'timeline'}))
    (rd/'runtime_events.jsonl').write_text('\n'.join([
        json.dumps({'timestamp':'2026-06-26T00:00:02+00:00','type':'subtask_accepted','payload':{'subtask_id':'st1'}}),
        json.dumps({'timestamp':'2026-06-26T00:00:01+00:00','type':'subtask_attempt_started','payload':{'subtask_id':'st1','attempt_id':'st1_attempt_001'}}),
    ]))
    tl=build_viewer_snapshot(rd)['timeline']
    assert [e['type'] for e in tl] == ['subtask_attempt_started','subtask_accepted']
    assert tl[-1]['status'] == 'accepted'
    assert 'Subtask 1' in tl[-1]['subtitle']


def test_viewer_graph_layout_humanized_grouped_and_non_overlapping(tmp_path):
    rd=tmp_path/'runs'/'graph'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'graph'}))
    events=[
        {'timestamp':'2026-06-26T00:00:00+00:00','type':'run_started','payload':{}},
        {'timestamp':'2026-06-26T00:00:01+00:00','type':'investigation_submitted','payload':{}},
        {'timestamp':'2026-06-26T00:00:02+00:00','type':'classification_submitted','payload':{}},
        {'timestamp':'2026-06-26T00:00:03+00:00','type':'plan_submitted','payload':{}},
        {'timestamp':'2026-06-26T00:00:04+00:00','type':'decomposition_submitted','payload':{}},
        {'timestamp':'2026-06-26T00:00:05+00:00','type':'execution_path_selected','payload':{}},
    ] + [{'timestamp':f'2026-06-26T00:00:0{i}+00:00','type':'subtask_attempt_completed','payload':{'subtask_id':f'st{i}'}} for i in range(1,6)]
    (rd/'runtime_events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    graph=build_viewer_snapshot(rd)['graph']
    positions=[(n['row'],n['col']) for n in graph['nodes']]
    assert len(positions) == len(set(positions))
    assert any(n['id']=='subtasks_group' for n in graph['nodes'])
    assert any(n['label']=='Subtask 1' and n['status']=='completed' for n in graph['nodes'])
    assert sum(1 for e in graph['edges'] if e['source']=='select_path' and e['target'].startswith('st')) == 0


def test_offline_viewer_contains_required_ui_without_external_deps(tmp_path):
    rd=fake_run(tmp_path)
    text=write_offline_viewer(rd).read_text()
    assert 'timeline-rail' in text and 'timeline-dot' in text
    assert 'graphInner' in text and 'elbowPath' in text
    assert 'formatInteger' in text and 'formatCost' in text
    assert 'villani-run-snapshot' in text
    assert 'https://' not in text and 'cdn' not in text.lower() and 'npm' not in text.lower()


def _decision_run(tmp_path, state):
    rd=tmp_path/'runs'/state.get('run_id','decision'); rd.mkdir(parents=True, exist_ok=True)
    (rd/'state.json').write_text(json.dumps(state))
    (rd/'runtime_events.jsonl').write_text(json.dumps({'timestamp':'2026-06-26T00:00:00+00:00','type':'run_finalized','payload':state.get('final_decision') or {}}))
    return rd


def test_decision_summary_and_warnings_render_in_offline_html(tmp_path):
    rd=_decision_run(tmp_path, {'run_id':'decision','status':'completed','task':'A very long task objective that should remain fully present in the DOM','runner':'villani-code-long-runner-name','backend_model':'local/backend/model-name-long','selection':{'selected_attempt_id':'candidate_003'},'selection_basis':'best_effort_tournament_selection','candidates':[{'attempt_id':'candidate_003','status':'completed','patch_path':'x.patch','changed_files':['src/calculator.py'],'runner_status':'completed','review_status':'passed','validation_status':'not_run','acceptance_eligible':True}]})
    snap=build_viewer_snapshot(rd)
    assert snap['decision']['state'] == 'accepted_with_warnings'
    html=write_offline_viewer(rd).read_text()
    assert 'Decision Summary' in html
    assert 'Raw final state' in html and 'Final result\\n' not in html
    assert 'Validation did not run. Treat this result as unverified.' in html
    assert 'A very long task objective that should remain fully present in the DOM' in html
    assert 'candidate_003' in html and 'Candidate Evidence' in html


def test_decision_summary_states(tmp_path):
    base={'run_id':'r','selection':{'selected_attempt_id':'candidate_001'},'candidates':[{'attempt_id':'candidate_001','validation_status':'passed','review_status':'passed'}]}
    assert build_viewer_snapshot(_decision_run(tmp_path/'a',{**base,'status':'completed'}))['decision']['state']=='accepted'
    assert build_viewer_snapshot(_decision_run(tmp_path/'b',{**base,'run_id':'r2','status':'completed','candidates':[{'attempt_id':'candidate_001','review_status':'passed'}]}))['decision']['state']=='accepted_with_warnings'
    assert build_viewer_snapshot(_decision_run(tmp_path/'c',{'run_id':'r3','status':'failed','failure_message':'Could not connect to backend http://127.0.0.1:9/v1'}))['decision']['state']=='failed'
    assert build_viewer_snapshot(_decision_run(tmp_path/'d',{'run_id':'r4','status':'completed','candidates':[]}))['decision']['state']=='incomplete'


def test_graph_and_candidate_evidence_expose_winner_missing_validation_and_failure(tmp_path):
    rd=_decision_run(tmp_path, {'run_id':'graph2','status':'failed','selection':{'selected_attempt_id':'candidate_001'},'candidates':[{'attempt_id':'candidate_001','status':'completed','validation_status':'not_run','changed_files':['a.py']},{'attempt_id':'candidate_002','status':'failed'}]})
    snap=build_viewer_snapshot(rd)
    assert any(n['subtitle']=='Selected winner' for n in snap['graph']['nodes'])
    assert any(n['id']=='validation_group' and n['status']=='missing' for n in snap['graph']['nodes'])
    assert any(n['id']=='finalization_group' and n['status']=='failed' for n in snap['graph']['nodes'])
    assert {c['candidate_id'] for c in snap['candidate_evidence']} == {'candidate_001','candidate_002'}


def test_normalized_usage_statuses(tmp_path):
    rd=tmp_path/'runs'/'usage-normalized'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'usage-normalized','status':'completed'}))
    (rd/'runtime_events.jsonl').write_text('')
    (rd/'usage.json').write_text(json.dumps({'input_tokens':1200,'output_tokens':5100,'total_tokens':6300,'calls_count':1,'unavailable_calls_count':1}))
    u=build_viewer_snapshot(rd)['usage']; assert u['tokens']['status']=='available' and u['cost']['status']=='unavailable'
    (rd/'usage.json').write_text(json.dumps({'calls_count':0,'unavailable_calls_count':0}))
    u=build_viewer_snapshot(rd)['usage']; assert u['tokens']['status']=='unavailable' and u['cost']['status']=='unavailable'
    (rd/'usage.json').write_text(json.dumps({'total_tokens':10,'total_cost':0.12,'calls_count':2,'unavailable_calls_count':1}))
    assert build_viewer_snapshot(rd)['usage']['cost']['status']=='partial'
    (rd/'usage.json').write_text(json.dumps({'total_tokens':10,'total_cost':0.01,'calls_count':1,'estimated':True}))
    assert build_viewer_snapshot(rd)['usage']['cost']['status']=='estimated'
    (rd/'usage.json').write_text(json.dumps({'total_tokens':10,'total_cost':0.0,'calls_count':1,'unavailable_calls_count':0}))
    assert build_viewer_snapshot(rd)['usage']['cost']['status']=='zero'
