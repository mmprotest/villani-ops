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
        {'command':'run-tests checks/existing_case','exitCode':0},
        {'tool_input': {'command':'verify --case checks/failing_case'}, 'exitCode':1},
        {'command':'build','exitCode':0},
    ]
    (d/'commands.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    c=SimpleNamespace(candidate_id='candidate-001', debug_dir=d, changed_files=['src/app'], verifier_result={'result':1,'toolsUsed':['not-real-tool']})
    row=build_candidate_evidence_matrix([c])[0]
    assert 'run-tests checks/existing_case' in row['commands_run']
    assert 'verify --case checks/failing_case' in row['commands_run']
    assert 'build' in row['commands_run']
    test_cmds={t['command']: t for t in row['tests_run']}
    assert test_cmds['run-tests checks/existing_case']['passed'] is True
    assert test_cmds['verify --case checks/failing_case']['passed'] is False
    assert 'build' not in test_cmds
    assert 'not-real-tool' not in row['commands_run']


def test_validation_detection_uses_generic_terms_not_toolchains():
    from villani_ops.orchestrator.selection import _command_looks_like_validation
    positive = [
        'run-tests --suite cleanup',
        'project check',
        'verify-behavior --case interruption',
        'validate cleanup',
        'spec-runner cancellation',
        'acme-test-runner -q',
    ]
    negative = ['build', 'install dependencies', 'run app', 'format', 'compile']
    for command in positive:
        assert _command_looks_like_validation(command), command
    for command in negative:
        assert not _command_looks_like_validation(command), command


def test_structured_record_metadata_can_mark_validation(tmp_path):
    import json
    from villani_ops.orchestrator.selection import _record_declares_test, build_candidate_evidence_matrix
    records = [
        {'kind': 'test', 'command': 'opaque-runner --foo', 'exitCode': 0},
        {'category': 'verification', 'command': 'opaque-runner --bar', 'exitCode': 0},
        {'phase': 'validation', 'command': 'opaque-runner --baz', 'exitCode': 0},
    ]
    debug = tmp_path/'debug'; debug.mkdir()
    (debug/'commands.jsonl').write_text('\n'.join(json.dumps(record) for record in records))
    row = build_candidate_evidence_matrix([SimpleNamespace(candidate_id='c', debug_dir=debug, changed_files=[], verifier_result={'result': 1})])[0]
    test_cmds = {item['command'] for item in row['tests_run']}
    for record in records:
        assert _record_declares_test(record)
        assert record['command'] in test_cmds


def test_repo_vs_candidate_classification_uses_changed_file_overlap():
    from villani_ops.orchestrator.selection import _classify_test_source
    changed_files = ['checks/generated_cleanup_case', 'src/worker']
    assert _classify_test_source('run-tests checks/existing_cleanup_case', changed_files) == 'repo'
    assert _classify_test_source('run-tests checks/generated_cleanup_case', changed_files) == 'candidate'
    assert _classify_test_source('run-tests', changed_files) != 'candidate'


def test_inline_temp_and_scratch_validation_is_candidate():
    from villani_ops.orchestrator.selection import _classify_test_source
    commands = [
        'validate --case /tmp/generated_case',
        'run-tests scratch/generated_case',
        'verify <<EOF\nassert cleanup\nEOF',
    ]
    for command in commands:
        assert _classify_test_source(command, []) == 'candidate'


def test_unknown_when_validation_source_is_ambiguous():
    from villani_ops.orchestrator.selection import _classify_test_source, _command_looks_like_validation
    command = 'opaque-runner target'
    assert not _command_looks_like_validation(command)
    assert _classify_test_source(command, []) == 'unknown'


def test_no_language_or_toolchain_literals_in_selection_classifier():
    import inspect
    import villani_ops.orchestrator.selection as selection
    source = '\n'.join(
        inspect.getsource(obj).lower()
        for obj in (
            selection._record_declares_test,
            selection._command_looks_like_validation,
            selection._extract_path_like_tokens,
            selection._path_overlaps_changed_files,
            selection._classify_test_source,
            selection._extract_candidate_tests,
        )
    )
    banned = [
        'pytest', 'unittest', 'tox', 'nox', 'npm', 'pnpm', 'yarn', 'bun', 'jest',
        'vitest', 'npx', 'cargo', 'gradle', 'gradlew', 'mvn', 'rspec', 'poetry',
        'pipenv', 'python', 'python3', 'node', 'ruby', 'java', '.py', '.js', '.ts',
        '.tsx', '.jsx', '.rb', '.go', '.rs', '.java', '.kt',
    ]
    assert not [literal for literal in banned if literal in source]

def test_report_contains_specific_winner_and_loser_evidence(tmp_path):
    from villani_ops.orchestrator.selection import _finalize_evidence_reasons, write_selection_report
    matrix=[
        {'candidate_id':'winner','verifier_result':'pass','verifier_confidence':.5,'commands_run':['run-tests checks/cleanup_case'],'tests_run':[{'command':'run-tests checks/cleanup_case','passed':True,'source':'repo'}],'files_changed':[],'direct_behavioral_evidence':[{'requirement':'cleanup','evidence':'interruption shutdown closes resources','strength':'strong'}],'source_level_inference_evidence':[],'missing_requirement_flags':[],'risk_flags':[],'evidence_score':{'direct_behavioral':4,'repo_tests':8,'candidate_tests':0,'source_inference':0,'requirement_coverage':12,'risk_penalty':0,'final':24},'selection_status':'','final_selection_reason':''},
        {'candidate_id':'loser','verifier_result':'pass','verifier_confidence':.9,'commands_run':[],'tests_run':[],'files_changed':['src/app'],'direct_behavioral_evidence':[],'source_level_inference_evidence':[{'requirement':'changed files','evidence':'src/app','strength':'medium'}],'missing_requirement_flags':['no explicit cleanup/cancellation evidence'],'risk_flags':['resource leak risk'],'evidence_score':{'direct_behavioral':0,'repo_tests':0,'candidate_tests':0,'source_inference':1,'requirement_coverage':6,'risk_penalty':10,'final':-3},'selection_status':'','final_selection_reason':''},
    ]
    matrix=_finalize_evidence_reasons(matrix, 'winner')
    text=write_selection_report(tmp_path/'selection_report.md', matrix, 'winner').read_text()
    assert 'interruption shutdown closes resources' in text
    assert 'run-tests checks/cleanup_case' in text
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
    (da/'commands.jsonl').write_text(json.dumps({'command':'validate --case /tmp/generated_case','exitCode':0})+'\n')
    (db/'commands.jsonl').write_text(json.dumps({'command':'run-tests checks/cleanup_case','exitCode':0})+'\n')
    a=SimpleNamespace(candidate_id='A', debug_dir=da, changed_files=['checks/generated_case'], verifier_result={'result':1,'confidence':.99,'successEvidence':['implemented code added']})
    b=SimpleNamespace(candidate_id='B', debug_dir=db, changed_files=['src/app'], verifier_result={'result':1,'confidence':.1,'successEvidence':['runtime validation observed cleanup behavior']})
    assert rank_candidates_by_evidence([a,b])[0]['candidate_id'] == 'B'


def test_evidence_matrix_records_gaps_when_no_artifact_commands():
    from villani_ops.orchestrator.selection import build_candidate_evidence_matrix
    row=build_candidate_evidence_matrix([{'candidateId':'c','result':1,'successEvidence':['implemented code']}])[0]
    assert row['commands_run'] == []
    assert row['tests_run'] == []
    assert any('no command log evidence' in r for r in row['risk_flags'])
    assert not any(t.get('source') == 'repo' for t in row['tests_run'])
