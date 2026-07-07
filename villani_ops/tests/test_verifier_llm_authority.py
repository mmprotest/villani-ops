import json

import httpx
import pytest

from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.verifier.deterministic import build_packet, deterministic_result
from villani_ops.verifier.llm import SYSTEM, calibrate, finalize_verifier_result, llm_result
from villani_ops.verifier.load_debug_run import load_debug_run


def _det(active=None, validations=None, deliverables=None):
    return {
        'evidenceByCategory': {
            'activeFailures': active or [],
            'recoveredFailures': [],
            'deliverableEvidence': deliverables or [],
            'finalEndToEndValidation': validations or [],
            'testValidation': [],
            'serviceValidation': [],
        },
        'deliverableAssessment': {},
    }


def _verdict(result, confidence=.8, action=None, reason='raw LLM judgement'):
    return {
        'result': result,
        'verdict': 'success' if result == 1 else 'failure',
        'confidence': confidence,
        'recommendedAction': action or ('accept' if result == 1 else 'reject'),
        'reason': reason,
        'riskFlags': [],
    }


def test_raw_llm_success_remains_final_success_with_candidate_active_failures():
    out = calibrate(_det(active=[{'id':'ev-001','kind':'command','text':'candidate failure'}]), _verdict(1))
    assert out['llmRawVerdict']['result'] == 1
    assert out['result'] == 1 and out['verdict'] == 'success'
    assert out['postProcessingChangedResult'] is False
    assert out['resultSource'] == 'llm_verifier'
    assert out['recommendedAction'] == 'inspect_manually'
    assert out['_calibration']['resultChanged'] is False
    assert out['_calibration']['deterministicDisagreements'][0]['effect'] == 'risk_flag_only'


def test_raw_llm_failure_remains_final_failure_with_candidate_validation_signals():
    out = calibrate(_det(validations=[{'id':'ev-002','validationStrength':'strong','text':'PASS'}]), _verdict(0))
    assert out['llmRawVerdict']['result'] == 0
    assert out['result'] == 0 and out['verdict'] == 'failure'
    assert out['postProcessingChangedResult'] is False
    assert out['_calibration']['resultChanged'] is False


def test_confidence_cap_does_not_change_result():
    v = _verdict(1, confidence=.98)
    v['criticalRequirementCovered'] = True
    out = calibrate(_det(), v)
    assert out['result'] == 1 and out['verdict'] == 'success'
    assert out['confidence'] == .70
    assert out['postProcessingChangedResult'] is False
    assert out['_calibration']['confidenceChanged'] is True


def test_recommended_action_can_change_to_inspect_manually_not_result():
    out = calibrate(_det(active=[{'text':'candidate failure'}]), _verdict(1, action='accept'))
    assert out['result'] == 1 and out['verdict'] == 'success'
    assert out['recommendedAction'] == 'inspect_manually'
    assert out['_calibration']['recommendedActionChanged'] is True


def test_final_guard_restores_attempted_result_flip():
    raw = _verdict(1)
    processed = {**raw, 'result': 0, 'verdict': 'failure', 'riskFlags': []}
    out = finalize_verifier_result(raw, processed)
    assert out['result'] == 1 and out['verdict'] == 'success'
    assert out['postProcessingChangedResult'] is False
    assert any('Restored raw LLM result' in f for f in out['riskFlags'])


def test_audit_adjudication_not_in_result_path_even_with_cfg():
    out = calibrate(_det(active=[{'text':'candidate failure'}]), _verdict(1), cfg={'unused': True})
    assert out['result'] == 1
    assert out['_calibration']['auditAdjudication']['enabled'] is False


def test_raw_llm_failure_not_flipped_by_generic_pass_output():
    det = _det(validations=[{'text':'generic PASS/self-validation','validationStrength':'weak'}])
    out = calibrate(det, _verdict(0, reason='PASS does not validate actual contract'))
    assert out['result'] == 0


def test_raw_llm_success_not_flipped_by_recovered_candidate_failures():
    det = _det(active=[{'text':'candidate failure recovered later'}])
    out = calibrate(det, _verdict(1, reason='later evidence recovered failure'))
    assert out['result'] == 1


def test_raw_llm_failure_not_flipped_by_setup_or_install_evidence():
    det = _det(validations=[{'text':'setup/install succeeded','validationStrength':'medium'}])
    out = calibrate(det, _verdict(0, reason='downstream behavior not proven'))
    assert out['result'] == 0


def test_raw_llm_success_not_flipped_by_missing_deterministic_deliverable_label():
    det = _det()
    det['deliverableAssessment'] = {'requiredDeliverables': ['out.txt']}
    out = calibrate(det, _verdict(1, reason='tool evidence showed required file content'))
    assert out['result'] == 1
    assert any('deliverable evidence labels are missing' in f.lower() for f in out['riskFlags'])


def test_calibration_trace_records_non_mutating_policy(tmp_path):
    class Trace:
        def write_json(self, name, payload):
            (tmp_path / name).write_text(json.dumps(payload))
    out = calibrate(_det(active=[{'text':'candidate failure'}]), _verdict(1), trace=Trace())
    cal = json.loads((tmp_path / 'calibration.json').read_text())
    assert cal['schemaVersion'] == 'villani-ops-verifier-calibration-v2'
    assert cal['resultMutationAllowed'] is False
    assert cal['resultChanged'] is False
    assert out['postProcessingChangedResult'] is False


def test_llm_result_contains_raw_source_and_trace(monkeypatch, tmp_path):
    fix = __import__('pathlib').Path(__file__).parent / 'fixtures' / 'verifier_success'
    run = load_debug_run(fix)
    det = deterministic_result(run, mode='llm_tool_loop')
    s = FileStorage(tmp_path / 'ws'); s.init_workspace(); s.save_backends({'b': Backend(name='b', provider='local', base_url='http://127.0.0.1:1234/v1', model='m', roles=['review'], capability_score=1)})
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {'choices':[{'message':{'content':json.dumps({'type':'final_verdict','result':1,'verdict':'success','confidence':.7,'recommendedAction':'accept','reason':'verified','riskFlags':[]})}}]}
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: Resp())
    res = llm_result(run, det, workspace=str(tmp_path / 'ws'))
    assert res['resultSource'] == 'llm_verifier'
    assert res['llmRawVerdict']['result'] == 1
    assert res['postProcessingChangedResult'] is False


def test_candidate_evidence_packet_uses_neutral_candidate_fields(tmp_path):
    d = tmp_path / 'debug'; d.mkdir()
    (d/'session_meta.json').write_text(json.dumps({'objective':'Create out.txt'}))
    (d/'commands.jsonl').write_text(json.dumps({'command':'echo PASS','exit_code':0,'stdout':'PASS'})+'\n')
    (d/'tool_calls.jsonl').write_text(''); (d/'patches.jsonl').write_text(''); (d/'model_responses.jsonl').write_text('')
    pkt = build_packet(load_debug_run(d))
    assert 'candidateEvidence' in pkt
    assert set(pkt['candidateEvidence']) >= {'candidateFailures','candidateValidationSignals','candidateMutations','candidateArtifacts','candidateConstraints','candidateRisks','candidateAgentClaims'}


def test_transcript_includes_non_mutating_sections():
    from villani_ops.verifier.trace import transcript
    text = transcript({
        'result': 1, 'verdict': 'success', 'confidence': .8, 'recommendedAction': 'accept',
        'llmRawVerdict': {'result': 1, 'verdict': 'success'},
        'calibration': {'resultMutationAllowed': False, 'deterministicDisagreements': []},
        'riskFlags': [],
    })
    assert '## Raw LLM Verdict' in text
    assert '## Non-Mutating Calibration' in text
    assert '## Deterministic Disagreements' in text
    assert '## Final Result' in text


def test_prompt_declares_llm_authority_and_task_contract_checklist():
    assert 'The deterministic evidence collector is not authoritative.' in SYSTEM
    assert 'deterministic labels are hints, not conclusions' in SYSTEM
    for phrase in ['required outputs','required file modifications','required behavior','required services or installability','required performance or quality constraints','forbidden changes','allowed-edit constraints','negative requirements']:
        assert phrase in SYSTEM


def test_infrastructure_error_schema_still_possible():
    # Infrastructure errors are represented outside normal calibration; the consistency helper preserves error schema.
    out = {'result': None, 'verdict': 'error', 'recommendedAction': 'inspect_manually', 'riskFlags': []}
    from villani_ops.verifier.llm import validate_final_result_consistency
    assert validate_final_result_consistency(out)['verdict'] == 'error'


def test_critical_coverage_accept_remains_accept():
    v = _verdict(1, confidence=.82, action='accept')
    v.update({'criticalRequirement':'abnormal path works','directEvidenceForCriticalRequirement':'targeted test exercised abnormal path','criticalRequirementCovered': True,'criticalRequirementEvidenceRefs':['ev-pass'],'criticalRequirementEvidenceMatch': {'ev-pass': {'matchesCriticalRequirement': True, 'requirementCondition': 'abnormal path works', 'evidenceCondition': 'abnormal path works', 'whySameCondition': 'targeted test exercised abnormal path', 'limitations': []}}})
    out = calibrate(_det(validations=[{'id':'ev-pass','validationStrength':'strong'}]), v)
    assert out['result'] == 1
    assert out['recommendedAction'] == 'accept'
    assert out['confidence'] == .82


def test_critical_coverage_false_downgrades_accept_and_caps_confidence():
    v = _verdict(1, confidence=.88, action='accept')
    v.update({'criticalRequirement':'cleanup on cancellation','directEvidenceForCriticalRequirement':'normal path test only','criticalRequirementCovered': False})
    out = calibrate(_det(), v)
    assert out['result'] == 1
    assert out['recommendedAction'] == 'inspect_manually'
    assert out['confidence'] == .70
    assert 'accept_downgraded_without_evidence_proven_critical_requirement_coverage' in out['warnings']


def test_missing_critical_coverage_downgrades_accept_and_caps_confidence():
    out = calibrate(_det(), _verdict(1, confidence=.86, action='accept'))
    assert out['result'] == 1
    assert out['recommendedAction'] == 'inspect_manually'
    assert out['confidence'] == .70


def test_failure_result_not_changed_by_critical_coverage_gate():
    v = _verdict(0, confidence=.77, action='reject')
    v.update({'criticalRequirementCovered': False})
    out = calibrate(_det(), v)
    assert out['result'] == 0
    assert out['recommendedAction'] == 'reject'
    assert out['confidence'] == .77


def test_critical_coverage_warning_preserves_existing_warnings():
    v = _verdict(1, confidence=.8, action='accept')
    v.update({'warnings':['existing-warning'], 'criticalRequirementCovered': False})
    out = calibrate(_det(), v)
    assert 'existing-warning' in out['warnings']
    assert 'accept_downgraded_without_evidence_proven_critical_requirement_coverage' in out['warnings']


def _covered_verdict(refs=None, confidence=.82, action='accept'):
    v = _verdict(1, confidence=confidence, action=action)
    v.update({
        'criticalRequirement': 'critical behavior',
        'directEvidenceForCriticalRequirement': 'cited evidence',
        'criticalRequirementCovered': True,
        'criticalRequirementEvidenceRefs': refs or [],
        'criticalRequirementEvidenceMatch': {r:{'matchesCriticalRequirement':True,'requirementCondition':'critical behavior','evidenceCondition':'critical behavior','whySameCondition':'same condition','limitations':[]} for r in (refs or [])},
    })
    return v


def test_critical_coverage_proven_accept_stays_accept():
    det = _det(validations=[{'id': 'ev-pass', 'validationStrength': 'strong'}])
    out = calibrate(det, _covered_verdict(['ev-pass']))
    assert out['result'] == 1
    assert out['recommendedAction'] == 'accept'
    assert out['criticalRequirementCoverageProven'] is True


def test_critical_coverage_declared_without_refs_downgrades_accept():
    out = calibrate(_det(), _covered_verdict([]))
    assert out['result'] == 1
    assert out['recommendedAction'] == 'inspect_manually'
    assert out['confidence'] == .70
    assert out['criticalRequirementCoverageProven'] is False
    assert 'accept_downgraded_without_evidence_proven_critical_requirement_coverage' in out['warnings']


def test_critical_coverage_source_inspection_ref_downgrades_accept():
    det = _det(validations=[{'id': 'src', 'kind': 'source_inspection', 'validationStrength': 'strong'}])
    out = calibrate(det, _covered_verdict(['src']))
    assert out['recommendedAction'] == 'inspect_manually'
    assert out['criticalRequirementCoverageProven'] is False


def test_critical_coverage_import_and_file_existence_refs_downgrade_unless_artifact_only():
    det = _det(validations=[{'id': 'imp', 'kind': 'import_check', 'validationStrength': 'strong'}], deliverables=[{'id': 'exists', 'kind': 'file_existence_check'}])
    out = calibrate(det, _covered_verdict(['imp', 'exists']))
    assert out['recommendedAction'] == 'inspect_manually'
    assert out['criticalRequirementCoverageProven'] is False

    artifact_det = _det(deliverables=[{'id': 'artifact', 'kind': 'file_existence_check'}])
    artifact_det['deliverableAssessment'] = {'artifactExistenceOnly': True}
    artifact = calibrate(artifact_det, _covered_verdict(['artifact']))
    assert artifact['recommendedAction'] == 'accept'
    assert artifact['criticalRequirementCoverageProven'] is True


def test_critical_coverage_normal_path_for_abnormal_requirement_downgrades_accept():
    det = _det(validations=[{'id': 'normal', 'validationStrength': 'strong', 'normalPathOnly': True}])
    v = _covered_verdict(['normal'])
    v['criticalRequirement'] = 'abnormal path behavior'
    out = calibrate(det, v)
    assert out['recommendedAction'] == 'inspect_manually'
    assert out['criticalRequirementCoverageProven'] is False


def test_critical_coverage_behavioral_runtime_evidence_accepts():
    det = _det(validations=[{'id': 'runtime', 'kind': 'runtime_trace', 'validationStrength': 'strong'}])
    out = calibrate(det, _covered_verdict(['runtime']))
    assert out['recommendedAction'] == 'accept'
    assert out['criticalRequirementCoverageProven'] is True


def test_proven_coverage_not_downgraded_by_stale_or_diagnostic_disagreement():
    v=_covered_verdict(['ev-pass'])
    det=_det(active=[{'id':'old','stale':True,'text':'earlier failure'}], validations=[{'id':'ev-pass','validationStrength':'strong','evidenceKind':'validation','evidenceProvenance':'command_output'}])
    det['evidenceRegistry']={'ev-pass': {'id':'ev-pass','category':'finalEndToEndValidation','evidenceKind':'validation','evidenceProvenance':'command_output','validationStrength':'strong'}, 'old': {'id':'old','stale':True}}
    out=calibrate(det, v)
    assert out['recommendedAction']=='accept'
    det=_det(active=[{'id':'diag','kind':'diagnostic','diagnosticOnly':True}], validations=[{'id':'ev-pass','validationStrength':'strong','evidenceKind':'validation','evidenceProvenance':'command_output'}])
    det['evidenceRegistry']={'ev-pass': {'id':'ev-pass','category':'finalEndToEndValidation','evidenceKind':'validation','evidenceProvenance':'command_output','validationStrength':'strong'}, 'diag': {'id':'diag','kind':'diagnostic','diagnosticOnly':True}}
    out=calibrate(det, v)
    assert out['recommendedAction']=='accept'


def test_proven_coverage_downgraded_by_final_decisive_failure():
    v=_covered_verdict(['ev-pass'])
    det=_det(active=[{'id':'fail','validationStrength':'failure','finalStateDecisive':True}], validations=[{'id':'ev-pass','validationStrength':'strong','evidenceKind':'validation','evidenceProvenance':'command_output'}])
    det['evidenceRegistry']={'ev-pass': {'id':'ev-pass','category':'finalEndToEndValidation','evidenceKind':'validation','evidenceProvenance':'command_output','validationStrength':'strong'}, 'fail': {'id':'fail','validationStrength':'failure','finalStateDecisive':True}}
    out=calibrate(det, v)
    assert out['recommendedAction']=='inspect_manually'
