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
