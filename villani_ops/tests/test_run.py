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
    res=runner.invoke(app,["run","--repo",str(repo),"--task","edit","--policy",".villani-ops/policies/p.yaml","--legacy-yaml-policy"], catch_exceptions=False)
    assert res.exit_code==0 and "REJECTED" in res.output
    run_dir=next((tmp_path/".villani-ops"/"runs").iterdir())
    assert "Shell runner command is not configured" in (run_dir/"report.md").read_text()

def test_run_configured_shell_edits_file_valid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); setup_workspace(tmp_path)
    repo=tmp_path/"repo"; repo.mkdir(); (repo/"hello.txt").write_text("hello\n")
    script=tmp_path/"edit.py"; script.write_text("from pathlib import Path\nPath('hello.txt').write_text('changed\\n')\n")
    assert runner.invoke(app,["runner","set","shell","--command",f"python {script}"], catch_exceptions=False).exit_code==0
    res=runner.invoke(app,["run","--repo",str(repo),"--task","edit","--policy",".villani-ops/policies/p.yaml","--legacy-yaml-policy"], catch_exceptions=False)
    assert res.exit_code==0 and "ACCEPTED" in res.output
    assert (repo/"hello.txt").read_text()=="hello\n"
    run_dir=next((tmp_path/".villani-ops"/"runs").iterdir()); attempt=run_dir/"attempts"/"attempt_001"
    assert (attempt/"diff.patch").exists() and "+changed" in (attempt/"diff.patch").read_text()
    assert (attempt/"attempt.json").exists() and (attempt/"validation.json").exists() and (run_dir/"decision.json").exists() and (run_dir/"report.md").exists()


def test_yaml_policy_without_legacy_flag_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); setup_workspace(tmp_path)
    repo=tmp_path/"repo"; repo.mkdir(); (repo/"hello.txt").write_text("hello\n")
    res=runner.invoke(app,["run","--repo",str(repo),"--task","edit","--policy",".villani-ops/policies/p.yaml"])
    assert res.exit_code != 0
    assert "YAML policy files use legacy smoke-test mode" in res.output


def test_cli_run_emits_progress_markers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); setup_workspace(tmp_path)
    repo=tmp_path/"repo"; repo.mkdir(); (repo/"hello.txt").write_text("hello\n")
    import villani_ops.cli.main as main
    class FakeDecision:
        accepted=True; final_state='accepted'; final_action='accept'; classification={'difficulty':'medium','category':'bug_fix','risk':'low'}; execution_strategy={'attempts':[{}]}; attempts_used=1; retries_used=0; escalations_used=0; human_reviews_requested=0; human_reviews_skipped=0; winning_attempt_id='attempt_001'; reviewer_decision='pass'; reviewer_score=1.0; human_override_used=False; reason='ok'; total_cost=0.0; reviewer_evidence=[]; run_id='run123'; failure_reason=''; attempts=[]
    class FakeOps:
        def __init__(self, *args, **kwargs): pass
        def run(self, **kwargs):
            print('Starting Villani Ops run')
            print('Classifying task')
            print('Generating policy')
            print('Running attempt_001')
            print('Reviewing attempt_001')
            print('Finalizing decision')
            print('Report: /tmp/report.md')
            return type('R', (), {'decision': FakeDecision(), 'report_path':'/tmp/report.md'})()
    monkeypatch.setattr(main, 'VillaniOps', FakeOps)
    res=runner.invoke(app,["run","--repo",str(repo),"--task","edit","--policy","balanced"], catch_exceptions=False)
    assert res.exit_code == 0
    for text in ['Starting Villani Ops run','Classifying task','Generating policy','Running attempt_001','Reviewing attempt_001','Finalizing decision','Report:']:
        assert text in res.output
