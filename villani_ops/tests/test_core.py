from pathlib import Path
from typer.testing import CliRunner
from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.core.policy import Policy, AttemptPlan
from villani_ops.core.pricing import estimate_cost
from villani_ops.core.attempt import Attempt
from villani_ops.validation.base import ValidationResult
from villani_ops.core.decision import select_attempt
from villani_ops.storage.files import FileStorage, capture_diff
from villani_ops.isolation.copy import CopyIsolation

runner=CliRunner()

def test_backend_save_load(tmp_path):
    s=FileStorage(tmp_path/".villani-ops"); s.init_workspace(); b=Backend(name="local",provider="local",model="m",input_cost_per_million=1,output_cost_per_million=2); s.save_backends({"local":b}); assert s.load_backends()["local"].model=="m"

def test_policy_load_save(tmp_path):
    p=Policy(name="p", attempts=[AttemptPlan(backend="b")]); path=tmp_path/"p.yaml"; p.save(path); assert Policy.load(path).attempts[0].backend=="b"

def test_init_creates_workspace(tmp_path):
    s=FileStorage(tmp_path/".villani-ops"); s.init_workspace(); assert (s.workspace/"config.yaml").exists() and (s.workspace/"runs").exists()

def test_copy_isolation_does_not_mutate_source(tmp_path):
    src=tmp_path/"src"; src.mkdir(); (src/"a.txt").write_text("a")
    dst=CopyIsolation().create(src,tmp_path/"run"/"repo"); (dst/"a.txt").write_text("b")
    assert (src/"a.txt").read_text()=="a"

def test_diff_capture_detects_modified_file(tmp_path):
    a=tmp_path/"a"; b=tmp_path/"b"; a.mkdir(); b.mkdir(); (a/"x.txt").write_text("one\n"); (b/"x.txt").write_text("two\n")
    out=capture_diff(a,b,tmp_path/"d.patch"); assert "-one" in out.read_text() and "+two" in out.read_text()

def test_pricing_calculation():
    b=Backend(name="b",provider="local",model="m",input_cost_per_million=1,output_cost_per_million=2)
    assert estimate_cost(1_000_000,500_000,b)==2

def test_selector_score_and_tie_break():
    a1=Attempt(attempt_id="a1",run_id="r",backend_name="b",runner_name="shell",estimated_cost=1,validation=ValidationResult(passed=True,score=.8,summary="",validator="t"))
    a2=Attempt(attempt_id="a2",run_id="r",backend_name="b",runner_name="shell",estimated_cost=2,validation=ValidationResult(passed=True,score=.9,summary="",validator="t"))
    assert select_attempt("r",[a1,a2],Policy(name="p").selection).winning_attempt_id=="a2"
    a2.validation.score=.8
    assert select_attempt("r",[a1,a2],Policy(name="p").selection).winning_attempt_id=="a1"

def test_cli_help_works():
    assert runner.invoke(app,["--help"]).exit_code==0

def test_cli_init_backend_runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app,["init"]).exit_code==0
    assert runner.invoke(app,["backend","add","local","--provider","local","--model","m","--input-cost","0","--output-cost","0"]).exit_code==0
    res=runner.invoke(app,["backend","list"]); assert res.exit_code==0 and "local" in res.output
    assert runner.invoke(app,["runner","set","shell","--command","python x.py"]).exit_code==0
    res=runner.invoke(app,["runner","list"]); assert res.exit_code==0 and "python x.py" in res.output


def test_init_creates_nested_workspace_parents(tmp_path):
    from villani_ops.storage.files import FileStorage
    ws=tmp_path/'missing'/'parent'/'.villani-ops'
    FileStorage(ws).init_workspace()
    assert ws.exists()
    assert (ws/'config.yaml').exists()
    assert (ws/'backends.yaml').exists()
    assert (ws/'runs').is_dir()
