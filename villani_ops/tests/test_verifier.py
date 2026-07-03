from __future__ import annotations
import json
from pathlib import Path
from typer.testing import CliRunner
import httpx
import pytest
from villani_ops.cli.main import app
from villani_ops.verifier.parse_jsonl import parse_jsonl
from villani_ops.verifier.load_debug_run import load_debug_run
from villani_ops.verifier.extract import extract_evidence, is_validation_command, classify_recovered
from villani_ops.verifier.deterministic import deterministic_result
from villani_ops.verifier.llm import llm_result
FIX=Path(__file__).parent/'fixtures'
def test_jsonl_parser_warns(tmp_path):
    p=tmp_path/'x.jsonl'; p.write_text('{"a":1}\nnope\n')
    rec,w,present=parse_jsonl(p)
    assert present and rec==[{'a':1}] and w
    assert parse_jsonl(tmp_path/'missing.jsonl', optional=True)==([],[],False)
def test_loader_success_and_missing():
    r=load_debug_run(FIX/'verifier_success')
    assert r.objective and r.commands and r.toolCalls and 'validations.jsonl' in r.missingArtifacts
    with pytest.raises(FileNotFoundError): load_debug_run(FIX/'missing')
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(FileNotFoundError): load_debug_run(d)
def test_evidence_extractor():
    r=load_debug_run(FIX/'verifier_success')
    s,f,_,m,mut,val=extract_evidence(r)
    assert is_validation_command('curl -sk https://x')
    assert any('PASS' in e.text for e in s)
    assert any('syntax error' in e.text for e in f)
    assert any('refused' in e.text for e in f)
    assert len([c for c in r.commands if c.command])==7
    assert mut and val and m
def test_recovery_classifier_and_active_failure():
    r=load_debug_run(FIX/'verifier_success'); s,f,*_=extract_evidence(r); active,rec=classify_recovered(f,s)
    assert rec and not active
    r2=load_debug_run(FIX/'verifier_failure'); s2,f2,*_=extract_evidence(r2); active2,rec2=classify_recovered(f2,s2)
    assert active2
def test_deterministic_verdicts():
    assert deterministic_result(load_debug_run(FIX/'verifier_success'))['verdict']=='success'
    assert deterministic_result(load_debug_run(FIX/'verifier_failure'))['verdict']=='failure'
    assert deterministic_result(load_debug_run(FIX/'verifier_unclear'))['verdict']=='unclear'
def test_cli_json_and_out(tmp_path):
    rr=CliRunner()
    res=rr.invoke(app,['verifier','--debug-dir',str(FIX/'verifier_success'),'--no-llm','--json'])
    assert res.exit_code==0
    obj=json.loads(res.stdout); assert obj['verdict']=='success'
    out=tmp_path/'v.json'
    res=rr.invoke(app,['verifier','--debug-dir',str(FIX/'verifier_failure'),'--no-llm','--json','--out',str(out)])
    assert res.exit_code==1 and out.exists()
    res=rr.invoke(app,['verifier','--debug-dir',str(FIX/'verifier_unclear'),'--no-llm','--json'])
    assert res.exit_code==2
def test_llm_adapter_fallback(monkeypatch):
    run=load_debug_run(FIX/'verifier_success'); det=deterministic_result(run)
    def boom(*a,**k): raise httpx.ConnectError('nope')
    monkeypatch.setenv('VILLANI_OPS_VERIFIER_MODEL','m')
    monkeypatch.setattr(httpx,'post',boom)
    res=llm_result(run,det)
    assert res['verdict']=='success' and res['riskFlags']
