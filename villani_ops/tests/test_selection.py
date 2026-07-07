from types import SimpleNamespace
from pathlib import Path
from villani_ops.orchestrator.selection import select_winner, build_llm_comparison_packet


def test_accept_recommended_success_beats_inspect_success():
    s=select_winner([
        {'candidateId':'inspect','result':1,'confidence':1,'recommendedAction':'inspect_manually'},
        {'candidateId':'accept','result':1,'confidence':0,'recommendedAction':'accept','criticalRequirementCovered':True,'criticalRequirementCoverageProven':True},
    ], seed=1)
    assert s.winnerCandidateId == 'accept'


def test_no_accept_falls_back_to_quality_logic():
    s=select_winner([
        {'candidateId':'weak','result':1,'recommendedAction':'inspect_manually','failureEvidence':['x']},
        {'candidateId':'strong','result':1,'recommendedAction':'inspect_manually','successEvidence':['runtime validation passed']},
    ], seed=1)
    assert s.winnerCandidateId == 'strong'


def test_rejected_candidate_does_not_beat_success():
    s=select_winner([
        {'candidateId':'rejected','result':0,'recommendedAction':'accept','confidence':1},
        {'candidateId':'success','result':1,'recommendedAction':'inspect_manually','confidence':0},
    ], seed=1)
    assert s.winnerCandidateId == 'success'


def test_accept_tie_is_seed_deterministic():
    cs=[{'candidateId':f'c{i}','result':1,'recommendedAction':'accept','criticalRequirementCovered':True,'criticalRequirementCoverageProven':True} for i in range(4)]
    assert select_winner(cs, 99).winnerCandidateId == select_winner(cs, 99).winnerCandidateId


def test_comparison_packet_includes_and_truncates(tmp_path):
    patch=tmp_path/'diff.patch'; patch.write_text('diff --git a/x b/x\n' + 'A'*5000)
    c=SimpleNamespace(candidate_id='candidate-001', patch_path=patch, changed_files=['x'], verifier_result={'result':1,'confidence':.8,'recommendedAction':'accept','reason':'summary','criticalRequirement':'edge case','directEvidenceForCriticalRequirement':'targeted test','criticalRequirementCovered':True,'criticalRequirementEvidenceRefs':['ev-0001'],'criticalRequirementCoverageProven':True,'successEvidence':['B'*1000]})
    packet=build_llm_comparison_packet([c], diff_limit=100, evidence_limit=50)
    row=packet[0]
    assert row['candidateId']=='candidate-001'
    assert row['verifier']['reason']=='summary'
    assert row['changedFiles']==['x']
    assert 'diff --git' in row['diffExcerpt'] and 'truncated' in row['diffExcerpt']
    assert 'truncated' in row['verifier']['successEvidence'][0]



def test_success_with_critical_coverage_beats_uncovered_when_comparable():
    s=select_winner([
        {'candidateId':'uncovered','result':1,'confidence':.9,'recommendedAction':'inspect_manually','criticalRequirementCovered':False},
        {'candidateId':'covered','result':1,'confidence':.9,'recommendedAction':'inspect_manually','criticalRequirementCovered':True},
    ], seed=1)
    assert s.winnerCandidateId == 'covered'


def test_accept_without_coverage_not_stronger_than_manual_with_coverage():
    s=select_winner([
        {'candidateId':'accept_uncovered','result':1,'confidence':.9,'recommendedAction':'accept','criticalRequirementCovered':False},
        {'candidateId':'manual_covered','result':1,'confidence':.9,'recommendedAction':'inspect_manually','criticalRequirementCovered':True},
    ], seed=1)
    assert s.winnerCandidateId == 'manual_covered'


def test_comparison_packet_includes_critical_requirement_fields():
    packet=build_llm_comparison_packet([{'candidateId':'c1','result':1,'recommendedAction':'inspect_manually','criticalRequirement':'rollback','directEvidenceForCriticalRequirement':'rollback test passed','criticalRequirementCovered':True}])
    verifier=packet[0]['verifier']
    assert verifier['criticalRequirement'] == 'rollback'
    assert verifier['directEvidenceForCriticalRequirement'] == 'rollback test passed'
    assert verifier['criticalRequirementCovered'] is True
    assert 'criticalRequirementEvidenceRefs' in verifier


def test_success_with_coverage_proven_beats_declared_only_when_comparable():
    s=select_winner([
        {'candidateId':'declared','result':1,'confidence':.9,'recommendedAction':'inspect_manually','criticalRequirementCovered':True,'criticalRequirementCoverageProven':False},
        {'candidateId':'proven','result':1,'confidence':.9,'recommendedAction':'inspect_manually','criticalRequirementCovered':True,'criticalRequirementCoverageProven':True},
    ], seed=1)
    assert s.winnerCandidateId == 'proven'


def test_accept_without_coverage_proven_not_stronger_than_manual_proven():
    s=select_winner([
        {'candidateId':'accept_declared','result':1,'confidence':.9,'recommendedAction':'accept','criticalRequirementCovered':True,'criticalRequirementCoverageProven':False},
        {'candidateId':'manual_proven','result':1,'confidence':.9,'recommendedAction':'inspect_manually','criticalRequirementCovered':True,'criticalRequirementCoverageProven':True},
    ], seed=1)
    assert s.winnerCandidateId == 'manual_proven'


def test_comparison_packet_includes_coverage_proven_and_warnings():
    packet=build_llm_comparison_packet([{'candidateId':'c1','result':1,'recommendedAction':'inspect_manually','criticalRequirement':'rollback','directEvidenceForCriticalRequirement':'rollback test passed','criticalRequirementCovered':True,'criticalRequirementCoverageProven':False,'warnings':['accept_downgraded_without_evidence_proven_critical_requirement_coverage']}])
    verifier=packet[0]['verifier']
    assert verifier['criticalRequirementCoverageProven'] is False
    assert 'accept_downgraded_without_evidence_proven_critical_requirement_coverage' in verifier['warnings']


def test_no_candidate_with_coverage_proven_falls_back_without_fabricating():
    s=select_winner([
        {'candidateId':'a','result':1,'confidence':.8,'recommendedAction':'inspect_manually','criticalRequirementCovered':True,'criticalRequirementCoverageProven':False},
        {'candidateId':'b','result':1,'confidence':.9,'recommendedAction':'inspect_manually','criticalRequirementCovered':True,'criticalRequirementCoverageProven':False},
    ], seed=1)
    assert s.winnerCandidateId == 'b'
    assert all(not row['criticalRequirementCoverageProven'] for row in s.candidateQuality)


def test_same_condition_proven_coverage_outranks_concrete_nonmatching_evidence():
    s=select_winner([
        {'candidateId':'nearby','result':1,'confidence':.99,'recommendedAction':'inspect_manually','criticalRequirementCovered':True,'criticalRequirementCoverageProven':False,'criticalRequirementEvidenceRefs':['ev-1'],'criticalRequirementEvidenceMatch':{'ev-1':{'matchesCriticalRequirement':False}}},
        {'candidateId':'same','result':1,'confidence':.5,'recommendedAction':'inspect_manually','criticalRequirementCovered':True,'criticalRequirementCoverageProven':True,'criticalRequirementEvidenceRefs':['ev-2'],'criticalRequirementEvidenceMatch':{'ev-2':{'matchesCriticalRequirement':True}}},
    ], seed=1)
    assert s.winnerCandidateId=='same'


def test_accept_without_same_condition_proven_coverage_not_strong():
    s=select_winner([
        {'candidateId':'accept_nearby','result':1,'confidence':.99,'recommendedAction':'accept','criticalRequirementCovered':True,'criticalRequirementCoverageProven':False},
        {'candidateId':'manual_same','result':1,'confidence':.5,'recommendedAction':'inspect_manually','criticalRequirementCovered':True,'criticalRequirementCoverageProven':True},
    ], seed=1)
    assert s.winnerCandidateId=='manual_same'


def test_llm_comparison_packet_includes_evidence_match():
    packet=build_llm_comparison_packet([{'candidateId':'c1','result':1,'recommendedAction':'accept','criticalRequirementEvidenceMatch':{'ev-1':{'matchesCriticalRequirement':True,'requirementCondition':'edge'}}}])
    assert packet[0]['verifier']['criticalRequirementEvidenceMatch']['ev-1']['matchesCriticalRequirement'] is True


def test_candidate_with_non_material_limitations_and_proven_coverage_outranks_unproven():
    s=select_winner([
        {'candidateId':'unproven','result':1,'confidence':.99,'recommendedAction':'inspect_manually','criticalRequirementCoverageProven':False},
        {'candidateId':'proven_audit','result':1,'confidence':.5,'recommendedAction':'accept','criticalRequirementCoverageProven':True,'criticalRequirementEvidenceMatch':{'ev-1':{'matchesCriticalRequirement':True,'limitations':[{'text':'audit note','material':False}]}}},
    ], seed=1)
    assert s.winnerCandidateId=='proven_audit'


def test_material_limitations_do_not_count_as_proven_for_selection_strength():
    s=select_winner([
        {'candidateId':'material','result':1,'confidence':.99,'recommendedAction':'accept','criticalRequirementCoverageProven':False,'criticalRequirementEvidenceMatch':{'ev-1':{'matchesCriticalRequirement':True,'limitations':[{'text':'weaker nearby','material':True}]}}},
        {'candidateId':'manual_proven','result':1,'confidence':.5,'recommendedAction':'inspect_manually','criticalRequirementCoverageProven':True},
    ], seed=1)
    assert s.winnerCandidateId=='manual_proven'


def test_accept_strong_only_when_materiality_processed_coverage_remains_true():
    s=select_winner([
        {'candidateId':'accept_material_downgraded','result':1,'confidence':.99,'recommendedAction':'accept','criticalRequirementCoverageProven':False},
        {'candidateId':'manual_proven','result':1,'confidence':.5,'recommendedAction':'inspect_manually','criticalRequirementCoverageProven':True},
    ], seed=1)
    assert s.winnerCandidateId=='manual_proven'


def test_llm_comparison_packet_preserves_limitation_materiality():
    packet=build_llm_comparison_packet([{'candidateId':'c1','result':1,'recommendedAction':'accept','criticalRequirementCoverageProven':True,'criticalRequirementEvidenceMatch':{'ev-1':{'matchesCriticalRequirement':True,'limitations':[{'text':'audit note','material':False}]}}}])
    assert packet[0]['verifier']['criticalRequirementEvidenceMatch']['ev-1']['limitations'][0]['material'] is False
