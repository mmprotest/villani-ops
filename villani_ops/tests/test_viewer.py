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


def rich_run(tmp_path: Path):
    rd=tmp_path/'runs'/'rich'; rd.mkdir(parents=True)
    (rd/'attempts'/'candidate_002').mkdir(parents=True)
    (rd/'attempts'/'candidate_002'/'debug.json').write_text('{}')
    state={
        'run_id':'rich','status':'running','classification':{'summary':'medium async bug','rationale':'race risk'},
        'investigation':{'summary':'Found async task cleanup issue','response':'investigation response'},
        'plan':{'summary':'Run candidates in parallel','rationale':'compare patches'},
        'selection':{'selected_attempt_id':'candidate_002','rationale':'narrow patch'},
        'candidates':[{'attempt_id':'candidate_002','status':'running','changed_files':['run.py'],'commands':[{'cmd':'pytest','exit_code':1},{'cmd':'ruff','exit_code':0}], 'patch_path':'candidate_002.patch','patch_summary':'Changed async task orchestration logic.','runner_output':'Villani Code edited run.py','latest_activity':['Read run.py','Edited run.py']}]
    }
    (rd/'state.json').write_text(json.dumps(state))
    events=[
        {'event_id':'classification-1','timestamp':'2026-06-26T00:00:01+00:00','type':'classification_submitted','payload':{'difficulty':'medium','category':'async/concurrency bug','risk':'medium','model_response':'classified medium async bug because task cleanup may race'}},
        {'event_id':'investigation-1','timestamp':'2026-06-26T00:00:02+00:00','type':'investigation_submitted','payload':{'summary':'Found cleanup race','assistant_content':'investigation response text'}},
        {'event_id':'plan-1','timestamp':'2026-06-26T00:00:03+00:00','type':'plan_submitted','payload':{'execution_path':'adaptive_tournament','raw_response':'plan response text'}},
        {'event_id':'cand-start','timestamp':'2026-06-26T00:00:04+00:00','type':'candidate_attempt_started','payload':{'attempt_id':'candidate_002'}},
        {'event_id':'review-1','timestamp':'2026-06-26T00:00:05+00:00','type':'candidate_attempt_reviewed','payload':{'attempt_id':'candidate_002','recommendation':'accept','confidence':0.7,'rationale':'review response'}},
        {'event_id':'pair-1','timestamp':'2026-06-26T00:00:06+00:00','type':'candidate_pairwise_comparison_completed','payload':{'winner':'candidate_002','rationale':'comparison response'}},
        {'event_id':'select-1','timestamp':'2026-06-26T00:00:07+00:00','type':'selection_completed','payload':{'selected_attempt_id':'candidate_002','rationale':'ranking selection response'}},
    ]
    (rd/'runtime_events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    return rd


def test_viewer_snapshot_details_are_selectable_and_human_readable(tmp_path):
    snap=build_viewer_snapshot(rich_run(tmp_path))
    assert snap['details']
    assert all(e['id'] and e['detail_id'] for e in snap['timeline'])
    assert all(n['id'] and n['detail_id'] for n in snap['graph']['nodes'])
    assert 'raw' in snap['details']['classification-1']
    assert snap['details']['classification-1']['summary'].startswith('The orchestrator classified')
    assert snap['details']['investigation-1']['summary'] == 'Found cleanup race'
    assert 'adaptive_tournament' in snap['details']['plan-1']['summary']
    assert snap['details']['candidate_002']['summary'].startswith('Candidate 002 is running')
    assert 'run.py' in snap['details']['candidate_002']['evidence_summary']
    assert '2 recorded; 1 failed' in snap['details']['candidate_002']['evidence_summary']
    assert 'Changed async task' in snap['details']['candidate_002']['diff_summary']
    assert any('debug.json' in a['path'] for a in snap['details']['candidate_002']['artifacts'])


def test_viewer_ai_response_extraction_for_core_steps(tmp_path):
    snap=build_viewer_snapshot(rich_run(tmp_path)); d=snap['details']
    assert 'classified medium async bug' in d['classification-1']['ai_response']
    assert 'investigation response text' in d['investigation-1']['ai_response']
    assert 'plan response text' in d['plan-1']['ai_response']
    assert 'review response' in d['review-1']['ai_response']
    assert 'comparison response' in d['pair-1']['ai_response']
    assert 'ranking selection response' in d['select-1']['ai_response']


def test_candidate_detail_handles_missing_debug_artifacts_gracefully(tmp_path):
    rd=tmp_path/'runs'/'missing-debug'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'missing-debug','candidates':[{'attempt_id':'candidate_001','status':'completed'}]}))
    (rd/'runtime_events.jsonl').write_text('')
    detail=build_viewer_snapshot(rd)['details']['candidate_001']
    assert 'No changed files were recorded' in detail['summary']
    assert detail['diff_summary'] == 'No patch was produced.'


def test_viewer_server_candidate_debug_endpoint(tmp_path):
    rd=rich_run(tmp_path)
    srv=ViewerServer(tmp_path/'runs', port=18766).start(try_ports=1)
    try:
        data=json.loads(urllib.request.urlopen(f'http://127.0.0.1:{srv.port}/api/runs/{rd.name}/candidate/candidate_002/debug').read())
        assert data['candidate_id']=='candidate_002'
        assert 'run.py' in data['summary']
        assert data['commands']
    finally:
        srv.stop()
