import json
from pathlib import Path
from typer.testing import CliRunner
from villani_ops.cli.main import app

FIX=Path(__file__).parent/'fixtures'/'verifier_success'
rr=CliRunner()

def _run(tmp_path,*args):
    res=rr.invoke(app,['verifier','--debug-dir',str(FIX),'--no-llm','--json','--workspace',str(tmp_path),*args])
    assert res.exit_code==0, res.output
    return json.loads(res.output)

def test_no_llm_json_creates_trace_and_clean_stdout(tmp_path):
    obj=_run(tmp_path)
    assert obj['traceDir']
    assert isinstance(obj,dict)
    td=Path(obj['traceDir'])
    for name in ['manifest.json','input.json','source_artifacts.json','verifier_packet.json','evidence_by_category.json','verification_result.json','verifier_transcript.md','errors.jsonl']:
        assert (td/name).exists(), name
    assert json.loads((td/'verification_result.json').read_text())==obj
    assert (td.parent/'index.jsonl').exists()

def test_trace_levels_and_no_trace(tmp_path):
    obj=_run(tmp_path,'--trace-level','minimal')
    names={p.name for p in Path(obj['traceDir']).iterdir()}
    assert {'manifest.json','verification_result.json','errors.jsonl'} <= names
    assert 'input.json' not in names
    obj2=_run(tmp_path,'--trace-level','full')
    for name in ['timeline.jsonl','validation_windows.json','failure_classification.json']:
        assert (Path(obj2['traceDir'])/name).exists()
    obj3=_run(tmp_path,'--no-trace')
    assert obj3['traceDir'] is None and obj3['traceId'] is None

def test_trace_dir_exact_and_jsonl_valid(tmp_path):
    exact=tmp_path/'exact-trace'
    obj=_run(tmp_path,'--trace-dir',str(exact))
    assert Path(obj['traceDir'])==exact
    for name in ['timeline.jsonl','errors.jsonl']:
        for line in (exact/name).read_text().splitlines():
            json.loads(line)
    fc=json.loads((exact/'failure_classification.json').read_text())
    assert 'counts' in fc and 'postValidationRiskCount' in fc['counts']
    ev=json.loads((exact/'evidence_by_category.json').read_text())
    for key in ['finalEndToEndValidation','testValidation','serviceValidation','repoMutation','fileMutation','setupEvidence','inspectionEvidence','cleanupEvidence','agentClaims','activeFailures','recoveredFailures','missingEvidence','riskFlags']:
        assert key in ev
