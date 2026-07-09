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


def test_evidence_extraction_reads_debug_commands_jsonl(tmp_path):
    import json
    from villani_ops.orchestrator.selection import build_candidate_evidence_matrix
    d=tmp_path/'debug'; d.mkdir()
    (d/'session_meta.json').write_text(json.dumps({'tool_input': {'command': 'echo meta'}}))
    rows=[
        {'command':'pytest tests/test_app.py','exitCode':0},
        {'tool_input': {'command':'pytest tests/test_fail.py'}, 'exitCode':1},
        {'command':'python -m build','exitCode':0},
    ]
    (d/'commands.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    c=SimpleNamespace(candidate_id='candidate-001', debug_dir=d, changed_files=['src/app.py'], verifier_result={'result':1,'toolsUsed':['not-real-tool']})
    row=build_candidate_evidence_matrix([c])[0]
    assert 'pytest tests/test_app.py' in row['commands_run']
    assert 'pytest tests/test_fail.py' in row['commands_run']
    assert 'python -m build' in row['commands_run']
    test_cmds={t['command']: t for t in row['tests_run']}
    assert test_cmds['pytest tests/test_app.py']['passed'] is True
    assert test_cmds['pytest tests/test_fail.py']['passed'] is False
    assert 'python -m build' not in test_cmds
    assert 'not-real-tool' not in row['commands_run']


def test_test_source_classification_repo_vs_candidate():
    from villani_ops.orchestrator.selection import _classify_test_source
    assert _classify_test_source('pytest tests/test_app.py', ['src/app.py']) == 'repo'
    assert _classify_test_source('pytest tests/test_app.py', ['tests/test_app.py']) == 'candidate'
    assert _classify_test_source('python -c "print(1)"', []) == 'candidate'


def test_report_contains_specific_winner_and_loser_evidence(tmp_path):
    from villani_ops.orchestrator.selection import _finalize_evidence_reasons, write_selection_report
    matrix=[
        {'candidate_id':'winner','verifier_result':'pass','verifier_confidence':.5,'commands_run':['pytest tests/test_cleanup.py'],'tests_run':[{'command':'pytest tests/test_cleanup.py','passed':True,'source':'repo'}],'files_changed':[],'direct_behavioral_evidence':[{'requirement':'cleanup','evidence':'SIGINT shutdown closes resources','strength':'strong'}],'source_level_inference_evidence':[],'missing_requirement_flags':[],'risk_flags':[],'evidence_score':{'direct_behavioral':4,'repo_tests':8,'candidate_tests':0,'source_inference':0,'requirement_coverage':12,'risk_penalty':0,'final':24},'selection_status':'','final_selection_reason':''},
        {'candidate_id':'loser','verifier_result':'pass','verifier_confidence':.9,'commands_run':[],'tests_run':[],'files_changed':['src/app.py'],'direct_behavioral_evidence':[],'source_level_inference_evidence':[{'requirement':'changed files','evidence':'src/app.py','strength':'medium'}],'missing_requirement_flags':['no explicit cleanup/cancellation evidence'],'risk_flags':['resource leak risk'],'evidence_score':{'direct_behavioral':0,'repo_tests':0,'candidate_tests':0,'source_inference':1,'requirement_coverage':6,'risk_penalty':10,'final':-3},'selection_status':'','final_selection_reason':''},
    ]
    matrix=_finalize_evidence_reasons(matrix, 'winner')
    text=write_selection_report(tmp_path/'selection_report.md', matrix, 'winner').read_text()
    assert 'SIGINT shutdown closes resources' in text
    assert 'pytest tests/test_cleanup.py' in text
    assert 'no explicit cleanup/cancellation evidence' in text
    assert 'resource leak risk' in text
    assert 'winner' in text and 'loser' in text
    assert text.count('strongest evidence-ranked coverage') == 0


def test_lexicographic_tie_break_replaces_ord_sum():
    from villani_ops.orchestrator.selection import rank_candidates_by_evidence
    candidates=[{'candidateId':'candidate-ba','result':1,'successEvidence':['runtime validation passed']},{'candidateId':'candidate-ab','result':1,'successEvidence':['runtime validation passed']}]
    assert rank_candidates_by_evidence(candidates)[0]['candidate_id'] == 'candidate-ab'


def test_evidence_ranking_prefers_repo_test_over_candidate_microtest(tmp_path):
    import json
    from villani_ops.orchestrator.selection import rank_candidates_by_evidence
    da=tmp_path/'a'; db=tmp_path/'b'; da.mkdir(); db.mkdir()
    (da/'commands.jsonl').write_text(json.dumps({'command':'python -c "assert True"','exitCode':0})+'\n')
    (db/'commands.jsonl').write_text(json.dumps({'command':'pytest tests/test_cleanup.py','exitCode':0})+'\n')
    a=SimpleNamespace(candidate_id='A', debug_dir=da, changed_files=['tests/test_new.py'], verifier_result={'result':1,'confidence':.99,'successEvidence':['implemented code added']})
    b=SimpleNamespace(candidate_id='B', debug_dir=db, changed_files=['src/app.py'], verifier_result={'result':1,'confidence':.1,'successEvidence':['runtime validation observed cleanup behavior']})
    assert rank_candidates_by_evidence([a,b])[0]['candidate_id'] == 'B'


def test_evidence_matrix_records_gaps_when_no_artifact_commands():
    from villani_ops.orchestrator.selection import build_candidate_evidence_matrix
    row=build_candidate_evidence_matrix([{'candidateId':'c','result':1,'successEvidence':['implemented code']}])[0]
    assert row['commands_run'] == []
    assert row['tests_run'] == []
    assert any('no command log evidence' in r for r in row['risk_flags'])
    assert not any(t.get('source') == 'repo' for t in row['tests_run'])
