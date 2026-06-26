from typer.testing import CliRunner
from villani_ops.cli.main import app, run


def test_run_help_shows_agentic_default_and_graph_legacy():
    res=CliRunner().invoke(app,['run','--help'])
    assert res.exit_code == 0
    defaults=run.__defaults__
    orchestrator_default=defaults[12].default
    orchestrator_help=defaults[12].help
    assert orchestrator_default == 'agentic'
    assert 'agentic (default adaptive)' in orchestrator_help
    assert 'graph (explicit legacy)' in orchestrator_help
