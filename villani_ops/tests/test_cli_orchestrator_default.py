from typer.testing import CliRunner
from villani_ops.cli.main import app, run


def test_run_help_shows_adaptive_default_and_graph_legacy():
    res=CliRunner().invoke(app,['run','--help'])
    assert res.exit_code == 0
    defaults=run.__defaults__
    orchestrator_default=defaults[12].default
    orchestrator_help=defaults[12].help
    assert orchestrator_default == 'adaptive'
    assert 'adaptive (default' in orchestrator_help
    assert 'agentic (decomposition-capable)' in orchestrator_help
    assert 'graph (explicit legacy)' in orchestrator_help


def test_run_unavailable_backend_finalizes_without_traceback(tmp_path, monkeypatch):
    import json
    repo=tmp_path/'repo'; repo.mkdir(); (repo/'README.md').write_text('x')
    monkeypatch.chdir(tmp_path)
    runner=CliRunner()
    assert runner.invoke(app,['init']).exit_code==0
    assert runner.invoke(app,['backend','add','local','--provider','local','--base-url','http://127.0.0.1:9/v1','--model','m','--roles','coding,review,selection,policy,investigation']).exit_code==0
    res=runner.invoke(app,['run','--repo',str(repo),'--task','do x','--no-ui'])
    assert res.exit_code != 0
    assert 'Traceback' not in res.output and 'ConnectError' not in res.output
    assert 'Villani Ops run failed' in res.output and 'Run directory:' in res.output and 'Next step:' in res.output
    rd=next((tmp_path/'.villani-ops'/'runs').iterdir())
    state=json.loads((rd/'state.json').read_text())
    assert state['status']=='failed' and state['failure_kind']=='backend_connection_error'
    assert (rd/'final_report.md').exists() and (rd/'event_digest.json').exists() and (rd/'usage.json').exists()
    assert 'provider_failure' in (rd/'runtime_events.jsonl').read_text()
