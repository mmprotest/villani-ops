from pathlib import Path
from typer.testing import CliRunner
from villani_ops.cli.main import app

runner=CliRunner()

def setup_workspace(tmp_path):
    assert runner.invoke(app,["init"], catch_exceptions=False).exit_code==0
    assert runner.invoke(app,["backend","add","local","--provider","local","--model","m","--input-cost","0","--output-cost","0"], catch_exceptions=False).exit_code==0
    assert runner.invoke(app,["policy","create-default","--name","p"], catch_exceptions=False).exit_code==0

def test_run_unconfigured_runner_fails_honestly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); setup_workspace(tmp_path)
    repo=tmp_path/"repo"; repo.mkdir(); (repo/"hello.txt").write_text("hello\n")
    res=runner.invoke(app,["run","--repo",str(repo),"--task","edit","--policy",".villani-ops/policies/p.yaml"], catch_exceptions=False)
    assert res.exit_code==0 and "REJECTED" in res.output
    run_dir=next((tmp_path/".villani-ops"/"runs").iterdir())
    assert "Shell runner command is not configured" in (run_dir/"report.md").read_text()

def test_run_configured_shell_edits_file_valid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); setup_workspace(tmp_path)
    repo=tmp_path/"repo"; repo.mkdir(); (repo/"hello.txt").write_text("hello\n")
    script=tmp_path/"edit.py"; script.write_text("from pathlib import Path\nPath('hello.txt').write_text('changed\\n')\n")
    assert runner.invoke(app,["runner","set","shell","--command",f"python {script}"], catch_exceptions=False).exit_code==0
    res=runner.invoke(app,["run","--repo",str(repo),"--task","edit","--policy",".villani-ops/policies/p.yaml"], catch_exceptions=False)
    assert res.exit_code==0 and "ACCEPTED" in res.output
    assert (repo/"hello.txt").read_text()=="hello\n"
    run_dir=next((tmp_path/".villani-ops"/"runs").iterdir()); attempt=run_dir/"attempts"/"attempt_001"
    assert (attempt/"diff.patch").exists() and "+changed" in (attempt/"diff.patch").read_text()
    assert (attempt/"attempt.json").exists() and (attempt/"validation.json").exists() and (run_dir/"decision.json").exists() and (run_dir/"report.md").exists()
