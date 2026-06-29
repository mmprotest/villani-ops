from pathlib import Path

from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.state import CandidateAttemptState, OpsRunState
from villani_ops.agentic.state_tooling import OpsToolContext
from villani_ops.agentic.tools import (
    OpsDeriveTaskActionContractInput,
    OpsMaterializeValidationProbesInput,
    _derive_task_action_contract,
    _probe_contract_conflict,
    h_derive_task_action_contract,
    h_materialize_validation_probes,
)
from villani_ops.core.acceptance import candidate_ranking_key


def _state(tmp_path, task):
    repo = tmp_path / 'repo'; run = tmp_path / 'run'; repo.mkdir(); run.mkdir()
    return OpsRunState(run_id='r', run_dir=str(run), repo_path=str(repo), task=task, success_criteria='done', mode='performance', runner='villani-code', candidate_attempts=2, investigation={'summary':'inspect available sources','risks':['ambiguous scope']}, plan={'strategy':'single_task'})


def _ctx(state):
    return OpsToolContext(run_dir=Path(state.run_dir), recorder=OpsEventRecorder(Path(state.run_dir), 'r'), transcript=[], production=False, allow_fake_dependencies=True)


def test_task_action_contract_is_derived_for_write_file_task(tmp_path):
    s = _state(tmp_path, 'Write the integer result to the requested output file.')
    c = h_derive_task_action_contract(s, OpsDeriveTaskActionContractInput(scope='task', reason='before probes'), _ctx(s))['task_action_contract']
    assert c['action_type'] == 'write_file'
    assert c['expected_artifacts'][0]['artifact_type'] in {'output_file', 'stdout'}
    assert any('Do not execute' in x for x in c['validation_implications'])


def test_task_action_contract_is_derived_for_compute_answer_task(tmp_path):
    s = _state(tmp_path, 'Compute the aggregate answer from the provided data and report the number.')
    c = _derive_task_action_contract(s, scope='task').model_dump(mode='json')
    assert c['action_type'] == 'compute_answer'
    assert c['source_grounding_requirements']
    assert any(a['audit_type'] == 'independent_recompute' for a in c['audit_requirements'])
    assert any(a['audit_type'] == 'semantic_assumption_check' for a in c['audit_requirements'])


def test_task_action_contract_is_derived_for_modify_code_repair_behavior_task(tmp_path):
    s = _state(tmp_path, 'Repair the cancellation behavior bug without regressing active cleanup.')
    c = _derive_task_action_contract(s, scope='task').model_dump(mode='json')
    assert c['action_type'] == 'repair_behavior'
    assert c['expected_artifacts'][0]['artifact_type'] == 'source_patch'
    assert any(a['audit_type'] == 'diff_review' for a in c['audit_requirements'])


def test_write_file_output_artifact_not_treated_as_executable_and_invalid_probe_downgraded(tmp_path):
    s = _state(tmp_path, 'Write an integer to an output file.')
    ctx = _ctx(s)
    contract = h_derive_task_action_contract(s, OpsDeriveTaskActionContractInput(scope='task', reason='before probes'), ctx)['task_action_contract']
    probe = {'id':'bad','description':'execute the output artifact as a program','executable':True,'command':'./artifact','related_requirement_ids':['R1'],'expected_observation':'runs'}
    assert _probe_contract_conflict(probe, contract)
    s.behavioural_oracles = [{'scope':'task','requirements':[{'id':'R1','priority':'critical'}], 'validation_probes':[probe], 'adversarial_review_checklist':[]}]
    packet = h_materialize_validation_probes(s, OpsMaterializeValidationProbesInput(scope='task', reason='materialize'), ctx)
    invalid = [p for p in packet['manual_review_items'] if p.get('invalid_probe')]
    assert invalid and invalid[0]['authority'] == 'diagnostic_only'
    assert not packet['materialized_probes']


def test_probe_generation_uses_expected_artifact_shape_for_compute_and_transform(tmp_path):
    s = _state(tmp_path, 'Transform the source dataset into the requested output schema using the correct field.')
    packet = h_materialize_validation_probes(s, OpsMaterializeValidationProbesInput(scope='task', reason='materialize'), _ctx(s))
    audits = packet['audit_requirements']
    assert any(a['audit_type'] == 'field_selection_audit' for a in audits)
    assert any(a['audit_type'] == 'row_count_or_scope_audit' for a in audits)
    assert packet['expected_artifacts'][0]['expected_shape']


def test_generated_audit_probe_authority_comes_from_strategy_not_acceptance_blocking(tmp_path):
    s = _state(tmp_path, 'Compute the answer from data.')
    packet = h_materialize_validation_probes(s, OpsMaterializeValidationProbesInput(scope='task', reason='materialize'), _ctx(s))
    assert all(p.get('authority') != 'acceptance_blocking' for p in packet['manual_review_items'] + packet['materialized_probes'])


def test_review_payload_includes_task_action_contract_audit_checklist(tmp_path):
    from villani_ops.agentic.tools import build_agentic_review_payload
    s = _state(tmp_path, 'Compute the answer from data.')
    h_derive_task_action_contract(s, OpsDeriveTaskActionContractInput(scope='task', reason='before review'), _ctx(s))
    a = CandidateAttemptState(attempt_id='candidate_001', status='completed', scope='candidate', changed_files=['x'], patch_path=__file__)
    payload = build_agentic_review_payload(s, a, 'candidate')
    assert 'task_action_contract' in payload
    checklist = ' '.join(payload['task_action_contract_audit_checklist'])
    assert 'assumptions grounded' in checklist
    assert 'agreement is supporting evidence only' in checklist


def test_candidate_selection_prefers_stronger_audit_evidence_over_generic_review_score(tmp_path):
    s = _state(tmp_path, 'Compute the answer from data.')
    weak = CandidateAttemptState(attempt_id='candidate_001', status='reviewed', scope='candidate', changed_files=['x'], patch_path=__file__, review={'decision':'pass','recommended_action':'accept','score':1.0,'confidence':1.0,'task_action_contract_satisfaction':0.2,'source_grounding_coverage':0.0,'audit_requirements_uncertain':['recompute']})
    strong = CandidateAttemptState(attempt_id='candidate_002', status='reviewed', scope='candidate', changed_files=['x'], patch_path=__file__, review={'decision':'pass','recommended_action':'accept','score':0.6,'confidence':0.7,'task_action_contract_satisfaction':1.0,'source_grounding_coverage':1.0,'audit_requirements_passed':['shape','recompute'],'independent_recompute_agreement':True})
    assert candidate_ranking_key(strong, state=s) > candidate_ranking_key(weak, state=s)


def test_retry_directives_target_failed_audit_requirements(tmp_path):
    from villani_ops.agentic.tools import create_attempt_observation
    s = _state(tmp_path, 'Compute the answer from data.')
    a = CandidateAttemptState(attempt_id='candidate_001', status='reviewed', scope='candidate', changed_files=['x'], patch_path=__file__, review={'decision':'fail','recommended_action':'retry','summary':'missing audit','score':0,'audit_requirements_failed':['source scope grounding']}, review_status='failed')
    obs = create_attempt_observation(s, a)
    assert any('audit requirement unresolved' in d for d in obs.next_attempt_directives)


def test_artifacts_show_task_action_contract_and_audit_results(tmp_path):
    from villani_ops.agentic.artifacts import derive_graph
    s = _state(tmp_path, 'Compute the answer from data.')
    h_derive_task_action_contract(s, OpsDeriveTaskActionContractInput(scope='task', reason='artifact'), _ctx(s))
    graph = derive_graph(s, [])
    assert any(n['type'] == 'task_action_contract' for n in graph['nodes'])
