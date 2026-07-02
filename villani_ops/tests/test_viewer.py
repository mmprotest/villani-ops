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
    assert any(n['type']=='validation' and n['status']=='missing' for n in snap['graph']['nodes'])
    assert any(n['id']=='final_decision' and n['status']=='failed' for n in snap['graph']['nodes'])
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


def test_provider_failure_graph_and_timeline_truthful(tmp_path):
    rd=tmp_path/'runs'/'backend-fail'; rd.mkdir(parents=True)
    state={'run_id':'backend-fail','status':'failed','failure_kind':'backend_connection_error','failure_message':'Could not connect','recoverable':True,'task':'Fix the failing calculator function','runner':'villani-code','backend_model':'qwen3.6-27b'}
    (rd/'state.json').write_text(json.dumps(state))
    (rd/'runtime_events.jsonl').write_text('\n'.join([
        json.dumps({'timestamp':'2026-07-02T00:00:00+00:00','type':'run_started','payload':{}}),
        json.dumps({'timestamp':'2026-07-02T00:00:01+00:00','type':'model_request_started','payload':{'backend':'local'}}),
        json.dumps({'timestamp':'2026-07-02T00:00:02+00:00','type':'provider_failure','payload':{'failure_kind':'backend_connection_error','failure_message':'Could not connect','backend':'local','recoverable':True}}),
        json.dumps({'timestamp':'2026-07-02T00:00:03+00:00','type':'run_finalized','payload':{}}),
    ]))
    snap=build_viewer_snapshot(rd)
    labels=' '.join(n['label']+' '+n.get('subtitle','') for n in snap['graph']['nodes'])
    assert snap['graph']['kind']=='provider_failure'
    assert 'Model request' in labels and 'Provider failure' in labels and 'Backend connection error' in labels and 'backend_connection_error' not in labels
    assert not any(x in labels for x in ['Candidates','Validation','Review','Selection'])
    tl=' '.join(e['title']+' '+e.get('subtitle','') for e in snap['timeline'])
    assert 'Model request started' in tl and 'Provider failure' in tl and 'Backend connection error' in tl and 'Could not connect' in tl and 'recoverable=true' in tl
    html=write_offline_viewer(rd).read_text()
    assert 'Provider failure' in html and 'Backend connection error' in html and 'Candidates' not in json.dumps(snap['graph'])


def test_header_uses_decision_and_cost_reasons_visible(tmp_path):
    rd=_decision_run(tmp_path, {'run_id':'20260702T004250Z-91ce53','status':'completed','task':'Fix the failing calculator function','runner':'villani-code','backend_model':'qwen3.6-27b','selection':{'selected_attempt_id':'candidate_003'},'candidates':[{'attempt_id':'candidate_003','validation_status':'not_run','review_status':'passed'}]})
    (rd/'usage.json').write_text(json.dumps({'total_tokens':10,'calls_count':1,'unavailable_calls_count':1}))
    html=write_offline_viewer(rd).read_text()
    assert 'Accepted with warnings' in html and 'completed' in html
    assert 'Fix the failing calculator function' in html and 'qwen3.6-27b' in html and 'villani-code' in html and '20260702T004250Z-91ce53' in html
    assert 'Backend pricing data missing' in html and '1 unavailable call' in html


def test_candidate_evidence_patch_aliases_and_changed_file_aliases(tmp_path):
    cases=[
        ({'attempt_id':'c1','patch_produced':True}, 'yes', []),
        ({'attempt_id':'c2','has_patch':True}, 'yes', []),
        ({'attempt_id':'c3','diff_path':'out.diff'}, 'yes', []),
        ({'attempt_id':'c4','changed_files':['a.py']}, 'yes', ['a.py']),
        ({'attempt_id':'c5','files_changed':['b.py']}, 'yes', ['b.py']),
        ({'attempt_id':'c6'}, 'unknown', []),
        ({'attempt_id':'c7','accepted_patch_application_status':'failed'}, 'no', []),
    ]
    rd=_decision_run(tmp_path, {'run_id':'patch-aliases','status':'completed','candidates':[c for c,_,__ in cases]})
    ev=build_viewer_snapshot(rd)['candidate_evidence']
    by_id={x['candidate_id']:x for x in ev}
    for c, patch, files in cases:
        assert by_id[c['attempt_id']]['patch'] == patch
        assert by_id[c['attempt_id']]['changed_files'] == files
    html=write_offline_viewer(rd).read_text()
    assert 'Patch</th>' in html and 'b.py' in html


def test_usage_normalizes_aliases_jsonl_state_runner_and_cost_reasons(tmp_path):
    rd=tmp_path/'runs'/'usage-aliases'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'usage-aliases','status':'completed','runner_telemetry':{'tokens_in':3,'tokens_out':4,'estimated_cost':0.02}}))
    (rd/'runtime_events.jsonl').write_text('')
    (rd/'usage.jsonl').write_text(json.dumps({'prompt_tokens':10,'completion_tokens':5,'amount':0.03})+'\n')
    u=build_viewer_snapshot(rd)['usage']
    assert u['input_tokens'] == 10 and u['output_tokens'] == 5 and u['total_tokens'] == 15
    assert round(u['total_cost'], 2) == 0.03


def test_execution_graph_demoted_below_evidence_sections(tmp_path):
    html=write_offline_viewer(fake_run(tmp_path)).read_text()
    assert 'graphPanel--secondary' in html
    assert html.index('Live Event Timeline') < html.index('Candidate Evidence') < html.index('Execution Graph')


def test_backend_failure_model_metadata_not_derived_from_v1_url(tmp_path):
    rd=tmp_path/'runs'/'backend-model'; rd.mkdir(parents=True)
    state={'run_id':'backend-model','status':'failed','failure_kind':'backend_connection_error','failure_message':'Could not connect','backend_model':'qwen3.6-27b','backend_name':'local','backend_url':'http://127.0.0.1:9/v1'}
    (rd/'state.json').write_text(json.dumps(state))
    (rd/'runtime_events.jsonl').write_text('\n'.join([
        json.dumps({'timestamp':'2026-07-02T00:00:01+00:00','type':'model_request_started','payload':{'backend_name':'local','model':'qwen3.6-27b','backend_url':'http://127.0.0.1:9/v1'}}),
        json.dumps({'timestamp':'2026-07-02T00:00:02+00:00','type':'provider_failure','payload':{'failure_kind':'backend_connection_error','failure_message':'Could not connect','backend_url':'http://127.0.0.1:9/v1'}}),
    ]))
    snap=build_viewer_snapshot(rd)
    assert snap['run']['model']=='qwen3.6-27b'
    assert snap['run']['backend_url'].endswith('/v1')
    assert 'v1' != snap['run']['model']
    details=json.dumps(snap['graph'])
    assert 'http://127.0.0.1:9/v1' in details


def test_missing_model_renders_unknown_model(tmp_path):
    rd=tmp_path/'runs'/'missing-model'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'missing-model','status':'failed','backend_url':'http://127.0.0.1:9/v1'}))
    (rd/'runtime_events.jsonl').write_text('')
    assert build_viewer_snapshot(rd)['run']['model']=='Unknown model'


def test_decision_summary_labels_are_humanized_and_raw_state_preserved(tmp_path):
    rd=_decision_run(tmp_path, {'run_id':'human','status':'failed','failure_kind':'backend_connection_error','failure_message':'backend_connection_error','selection_basis':'best_effort_tournament_selection','final_decision':{'failure_kind':'backend_connection_error'}})
    snap=build_viewer_snapshot(rd)
    assert snap['decision']['selection_basis']=='Best effort selection'
    assert snap['decision']['failure_reason']=='Backend connection error'
    html=write_offline_viewer(rd).read_text()
    assert 'Best effort selection' in html and 'Backend connection error' in html
    assert 'backend_connection_error' in html  # raw details remain available


def test_warning_graph_has_candidate_rows_winner_warning_and_human_labels(tmp_path):
    rd=_decision_run(tmp_path, {'run_id':'warn-graph','status':'completed','selection':{'selected_attempt_id':'candidate_003'},'selection_basis':'best_effort_tournament_selection','candidates':[
        {'attempt_id':'candidate_001','status':'completed','review_status':'passed','validation_status':'not_run'},
        {'attempt_id':'candidate_002','status':'failed','review_status':'failed','validation_status':'skipped'},
        {'attempt_id':'candidate_003','status':'completed','review_status':'passed','validation_status':'not_run','changed_files':['a.py']},
    ]})
    snap=build_viewer_snapshot(rd)
    graph=json.dumps(snap['graph'])
    assert 'Candidate 003' in graph and 'Selected winner' in graph
    assert 'Warning: validation not run' in graph
    assert 'best_effort_tournament_selection' not in graph
    assert all('details' in n for n in snap['graph']['nodes'])


def test_usage_duplicate_summaries_are_not_double_counted(tmp_path):
    rd=tmp_path/'runs'/'dup-summary'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'dup-summary','status':'completed'}))
    (rd/'runtime_events.jsonl').write_text('')
    (rd/'usage.json').write_text(json.dumps({'prompt_tokens':1000,'completion_tokens':1300,'total_cost':0.23,'calls_count':1}))
    (rd/'cost_summary.json').write_text(json.dumps({'input_tokens':1000,'output_tokens':1300,'total_tokens':2300,'total_cost':0.23,'calls_count':1}))
    snap=build_viewer_snapshot(rd)
    assert snap['usage']['total_tokens'] == 2300
    assert snap['usage']['total_cost'] == 0.23
    assert any('duplicate summary ignored' in x.lower() for x in snap['usage']['diagnostics'])
    html=write_offline_viewer(rd).read_text()
    assert '&quot;total_tokens&quot;:2300' in html or 'total_tokens' in html


def test_usage_jsonl_priority_and_call_id_dedupe(tmp_path):
    rd=tmp_path/'runs'/'jsonl-priority'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'jsonl-priority','status':'completed'}))
    (rd/'runtime_events.jsonl').write_text('')
    (rd/'usage.jsonl').write_text('\n'.join([
        json.dumps({'call_id':'a','prompt_tokens':10,'completion_tokens':5,'cost_usd':0.01}),
        json.dumps({'call_id':'a','prompt_tokens':10,'completion_tokens':5,'cost_usd':0.01}),
        json.dumps({'call_id':'b','tokens_in':20,'tokens_out':7,'estimated_cost':0.02}),
    ]))
    (rd/'usage.json').write_text(json.dumps({'total_tokens':999,'total_cost':9.99,'calls_count':1}))
    u=build_viewer_snapshot(rd)['usage']
    assert u['input_tokens'] == 30 and u['output_tokens'] == 12 and u['total_tokens'] == 42
    assert round(u['total_cost'], 2) == 0.03
    assert u['calls_count'] == 2


def test_candidate_lane_graph_contains_statuses_and_hooks(tmp_path):
    rd=_decision_run(tmp_path, {'run_id':'lanes','status':'completed','selection':{'selected_attempt_id':'candidate_003'},'selection_basis':'best_effort_tournament_selection','candidates':[
        {'attempt_id':'candidate_001','status':'completed','runner_status':'completed','review_status':'passed','validation_status':'not_run','changed_files':['a.py'],'patch_path':'a.patch'},
        {'attempt_id':'candidate_002','status':'completed','runner_status':'completed','review_status':'failed','validation_status':'skipped'},
        {'attempt_id':'candidate_003','status':'completed','runner_status':'completed','review_status':'passed','validation_status':'not_run','changed_files':['b.py','c.py'],'patch_produced':True,'acceptance_eligible':True},
    ]})
    snap=build_viewer_snapshot(rd)
    graph=snap['graph']
    assert graph['kind'] == 'candidate_lanes'
    labels=' '.join(n['label']+' '+n.get('subtitle','')+' '+str(n.get('badge') or '') for n in graph['nodes'])
    assert 'Candidate 001' in labels and 'Candidate 002' in labels and 'Candidate 003' in labels
    assert 'Runner completed' in labels and 'Review failed' in labels and 'Validation not run' in labels
    assert 'Winner' in labels and 'Patch yes' in labels and '2 changed files' in labels
    assert 'not_run' not in labels and 'best_effort_tournament_selection' not in labels
    html=write_offline_viewer(rd).read_text()
    assert 'data-node-id' in html and 'data-candidate-id' in html and 'data-node-kind' in html and 'data-detail-json' in html
    assert html.index('Live Event Timeline') < html.index('Candidate Evidence') < html.index('Execution Graph')


def test_candidate_evidence_primary_truth_surface_has_required_fields(tmp_path):
    rd=_decision_run(tmp_path, {'run_id':'evidence-primary','status':'completed','selection':{'selected_attempt_id':'candidate_003'},'candidates':[{'attempt_id':'candidate_003','status':'completed','patch_path':'x.patch','changed_files':['src/x.py'],'runner_status':'completed','review_status':'passed','validation_status':'not_run','acceptance_eligible':True}]})
    snap=build_viewer_snapshot(rd)
    ev=snap['candidate_evidence'][0]
    assert ev['selected'] is True and ev['patch']=='yes' and ev['changed_files']==['src/x.py']
    assert ev['review_status']=='Passed' and ev['validation_status']=='Not run'
    assert any('Validation did not run' in b for b in ev['blockers'])


def test_timeline_includes_review_validation_selection_and_human_labels(tmp_path):
    rd=tmp_path/'runs'/'key-events'; rd.mkdir(parents=True)
    (rd/'state.json').write_text(json.dumps({'run_id':'key-events','status':'completed'}))
    (rd/'runtime_events.jsonl').write_text('\n'.join(json.dumps(e) for e in [
        {'timestamp':'2026-07-02T00:00:01+00:00','type':'review_completed','payload':{'attempt_id':'candidate_003','result':'passed'}},
        {'timestamp':'2026-07-02T00:00:02+00:00','type':'validation_completed','payload':{'attempt_id':'candidate_003','result':'not_run','warning':'Validation did not run'}},
        {'timestamp':'2026-07-02T00:00:03+00:00','type':'winner_selected','payload':{'selected_attempt_id':'candidate_003','selection_basis':'best_effort_tournament_selection'}},
        {'timestamp':'2026-07-02T00:00:04+00:00','type':'run_finalized','payload':{}},
    ]))
    tl=build_viewer_snapshot(rd)['timeline']
    text=' '.join(e['title']+' '+e.get('subtitle','') for e in tl)
    assert 'Review completed' in text and 'Validation completed' in text and 'Winner selected' in text
    assert 'Candidate 003' in text and 'Not run' in text and 'Validation did not run' in text
    assert 'review_completed' not in text and 'validation_completed' not in text


def test_graph_detail_card_readable_before_raw_json(tmp_path):
    rd=_decision_run(tmp_path, {'run_id':'detail-card','status':'completed','selection':{'selected_attempt_id':'candidate_003'},'candidates':[{'attempt_id':'candidate_003','status':'completed','patch_path':'x.patch','changed_files':['src/x.py'],'runner_status':'completed','review_status':'passed','validation_status':'not_run','acceptance_eligible':True}]})
    html=write_offline_viewer(rd).read_text()
    assert 'graphDetailsCard' in html and 'Raw node data' in html and 'humanDetailLabel' in html
    assert html.index('graphDetailsCard') < html.index('Raw node data')
