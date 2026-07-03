from __future__ import annotations
import json
from pathlib import Path
from typer.testing import CliRunner
import pytest
from villani_ops.cli.main import app
from villani_ops.verifier.parse_jsonl import parse_jsonl
from villani_ops.verifier.load_debug_run import load_debug_run
from villani_ops.verifier.extract import extract_evidence, is_validation_command, classify_recovered
from villani_ops.verifier.deterministic import deterministic_result
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
def test_cli_missing_llm_config_errors(tmp_path):
    rr=CliRunner()
    res=rr.invoke(app,['verifier','--debug-dir',str(FIX/'verifier_success'),'--json','--workspace',str(tmp_path)])
    assert res.exit_code==3
    obj=json.loads(res.stdout)
    assert obj['verdict']=='error'
    assert obj['recommendedAction']=='inspect_manually'
    assert 'missing verifier backend' in obj['reason'] or 'missing verifier model' in obj['reason'] or 'base URL' in obj['reason']

def test_validation_before_cleanup_categorization_and_no_llm():
    run=load_debug_run(FIX/'verifier_success_validation_before_cleanup')
    res=deterministic_result(run)
    cats=res['evidenceByCategory']
    assert len(cats['finalEndToEndValidation']) >= 3
    assert len(cats['serviceValidation']) >= 1
    assert any('which git sshd nginx openssl' in e['text'] for e in cats['inspectionEvidence'])
    assert any('pgrep -a nginx' in e['text'] for e in cats['inspectionEvidence'])
    assert any('post-receive' in e['text'] for e in cats['setupEvidence'])
    assert cats['recoveredFailures'] and not cats['activeFailures']
    assert res['deterministicChecks']['finalValidationWindow'] is not None
    top='\n'.join(str(e) for e in res['successEvidence'][:3]).lower()
    assert any(x in top for x in ['git clone','git push','pass:'])
    assert not top.lstrip().startswith(('which','id ','cat /etc/os-release','pgrep'))
    assert res['verdict']=='success' and res['recommendedAction']=='accept'

def test_categorization_helpers_focused(tmp_path):
    from villani_ops.verifier.deterministic import build_packet
    base=FIX/'verifier_success_validation_before_cleanup'
    run=load_debug_run(base)
    pkt=build_packet(run); cats=pkt['evidence']
    assert any('which git sshd nginx openssl' in e['text'] for e in cats['inspectionEvidence'])
    assert not any('which git sshd nginx openssl' in e['text'] for e in cats['finalEndToEndValidation'])
    # id and os-release inspection in temporary fixture
    d=tmp_path/'fx'; d.mkdir()
    (d/'session_meta.json').write_text('{"objective":"check"}')
    (d/'summary.json').write_text('{"status":"completed"}')
    (d/'final_summary.json').write_text('{"status":"completed"}')
    for name in ['tool_calls.jsonl','patches.jsonl','model_responses.jsonl']:(d/name).write_text('')
    (d/'commands.jsonl').write_text('\n'.join([
        '{"command":"id git","exit_code":0,"stdout":"uid=1"}',
        '{"command":"cat /etc/os-release","exit_code":0,"stdout":"Debian"}',
        '{"command":"nginx -t","exit_code":0,"stderr":"test is successful"}',
        '{"command":"git clone git@localhost:/git/project /tmp/final-test","exit_code":0,"stdout":"Clone exit: 0"}',
        '{"command":"git push origin main","exit_code":0,"stdout":"Push exit: 0"}',
        '{"command":"python - <<\'PY\'\\nprint(\'PASS: Main branch serves correct content\')\\nPY","exit_code":0,"stdout":"PASS: Main branch serves correct content"}',
        '{"command":"find /tmp/final-test -mindepth 1 -delete","exit_code":1,"stderr":"cleanup permission denied"}',
    ]))
    pkt2=build_packet(load_debug_run(d)); c=pkt2['evidence']
    assert any('id git' in e['text'] for e in c['inspectionEvidence'])
    assert any('cat /etc/os-release' in e['text'] for e in c['inspectionEvidence'])
    assert any('nginx -t' in e['text'] for e in c['serviceValidation'])
    assert any('git clone' in e['text'] for e in c['finalEndToEndValidation'])
    assert any('git push origin main' in e['text'] for e in c['finalEndToEndValidation'])
    assert any('PASS: Main branch serves correct content' in e['text'] for e in c['finalEndToEndValidation'])
    assert any('find /tmp/final-test' in e['text'] for e in c['cleanupEvidence'])
    assert not c['activeFailures']

def test_post_validation_non_cleanup_failure_remains_active(tmp_path):
    d=tmp_path/'fx2'; d.mkdir()
    (d/'session_meta.json').write_text('{"objective":"check"}')
    (d/'summary.json').write_text('{"status":"completed"}')
    (d/'final_summary.json').write_text('{"status":"completed"}')
    for name in ['tool_calls.jsonl','patches.jsonl','model_responses.jsonl']:(d/name).write_text('')
    (d/'commands.jsonl').write_text('\n'.join([
        '{"command":"git clone git@localhost:/git/project /tmp/final-test","exit_code":0,"stdout":"Clone exit: 0"}',
        '{"command":"git push origin main","exit_code":0,"stdout":"Push exit: 0"}',
        '{"command":"python - <<\'PY\'\\nprint(\'PASS: Main branch serves correct content\')\\nPY","exit_code":0,"stdout":"PASS: Main branch serves correct content"}',
        '{"command":"curl -f https://localhost:8443/index.html","exit_code":22,"stderr":"HTTP 500 error"}',
    ]))
    pkt=__import__('villani_ops.verifier.deterministic',fromlist=['build_packet']).build_packet(load_debug_run(d))
    assert pkt['evidence']['activeFailures']
