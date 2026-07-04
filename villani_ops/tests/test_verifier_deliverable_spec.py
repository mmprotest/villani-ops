from villani_ops.verifier.extract import extract_deliverables
from villani_ops.verifier.types import CommandRecord, DebugRun
from villani_ops.verifier.deterministic import build_packet

def test_input_and_output_classification():
    s=extract_deliverables('Given input database data.db, generate report.json and modify app.py')
    assert 'data.db' in s.input_artifacts and 'data.db' not in s.required_output_files
    assert 'report.json' in s.required_output_files
    assert 'app.py' in s.required_edited_files

def test_archive_path_install_and_constraints():
    s=extract_deliverables('Using source archive vendor.tar.gz, make command available in PATH. only edit src/a.py and no warnings')
    assert 'vendor.tar.gz' in s.input_artifacts
    assert s.required_downstream_commands or s.required_binaries
    assert s.allowed_edit_constraints and s.negative_constraints

def test_validation_provenance_session_local_and_downstream(tmp_path):
    d=tmp_path; run=DebugRun(debugDir=str(d),objective='install tool available in PATH',commands=[CommandRecord(command='export PATH=/x:$PATH && which tool',exitCode=0,stdout='/x/tool',index=0),CommandRecord(command='python -m pip install --index-url http://localhost/simple pkg && python -c "import pkg"',exitCode=0,index=1)])
    pkt=build_packet(run)
    vals=pkt['evidence']['finalEndToEndValidation']+pkt['evidence']['testValidation']+pkt['evidence']['serviceValidation']+pkt['evidence']['setupEvidence']+pkt['evidence']['inspectionEvidence']
    assert any(v.get('sessionLocalOnly') for v in vals)
    assert any(v.get('downstreamValidation') for v in vals)
