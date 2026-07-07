import pytest
from villani_ops.verifier.types import CommandRecord, DebugRun, ToolCallRecord
from villani_ops.verifier.extract import extract_deliverables
from villani_ops.verifier.deterministic import _item, classify_validation_strength, build_packet, deterministic_result
from villani_ops.verifier.llm import compact_verifier_packet, normalize_read_tool_call, deterministic_fallback_result, _parse, calibrate, prove_critical_requirement_coverage


def cmd(command, stdout='', stderr='', exit=0):
    return CommandRecord(command=command, stdout=stdout, stderr=stderr, exitCode=exit, index=1)


def test_empty_output_validation_is_weak():
    run=DebugRun(debugDir='d', objective='Create /app/answer.txt', commands=[cmd('python check_something.py')])
    spec=extract_deliverables(run.objective)
    item=_item(run.commands[0], spec=spec)
    assert item.validationStrength == 'weak'
    assert 'empty output' in item.validationWeakness


def test_explicit_pass_output_is_strong_when_deliverable_exercised():
    run=DebugRun(debugDir='d', objective='Create /app/answer.txt', commands=[cmd('pytest /app/answer.txt', '3 tests passed')])
    spec=extract_deliverables(run.objective)
    assert classify_validation_strength(run.commands[0], spec)[0] == 'strong'


def test_crash_output_is_failure_even_when_exit_zero():
    run=DebugRun(debugDir='d', objective='Build the program', commands=[cmd('sh -c "./check || true"', 'Segmentation fault (core dumped)')])
    spec=extract_deliverables(run.objective)
    assert classify_validation_strength(run.commands[0], spec)[0] == 'failure'


def test_direct_test_like_helper_empty_output_not_strong():
    run=DebugRun(debugDir='d', objective='Make the filter reject alert payloads', commands=[cmd('python validation_helper.py')])
    spec=extract_deliverables(run.objective)
    assert _item(run.commands[0], spec=spec).validationStrength != 'strong'


def test_deliverable_extraction_ignores_source_content_paths():
    run=DebugRun(debugDir='d', objective='Create /app/output.json', toolCalls=[
        ToolCallRecord(toolName='write', status='ok', args={'file_path':'/app/main.py','content':'open("/app/not_required.json", "w")'}, index=1)
    ])
    spec=extract_deliverables(run.objective, run)
    assert '/app/output.json' in spec.required_files
    assert '/app/not_required.json' not in spec.required_files
    assert '/app/main.py' not in spec.required_files


def test_packet_compaction_caps_strings_and_lists_preserves_decisive_fields():
    long='x'*3000
    packet={'objective':long,'deliverableAssessment':{'missingDeliverables':['/app/a']},'deterministicChecks':{'activeFailureCount':1},'evidence':{'activeFailures':[{'text':long+str(i),'kind':'failure_signal'} for i in range(20)],'finalEndToEndValidation':[{'text':long+str(i),'validationStrength':'strong'} for i in range(20)]},'candidateEvidence':{'candidateFailures':[{'text':long+str(i)} for i in range(20)]}}
    compact=compact_verifier_packet(packet, max_text_chars=100)
    assert compact['deliverableAssessment']['missingDeliverables']==['/app/a']
    assert len(compact['evidence']['activeFailures']) <= 11
    assert len(compact['evidence']['activeFailures'][0]['text']) < 200
    assert any('truncated_count' in x for x in compact['evidence']['activeFailures'])


def test_evidence_ids_registry_and_compaction_are_stable():
    run=DebugRun(debugDir='d', objective='Fix project', status='completed', commands=[cmd('pytest','3 tests passed')])
    pkt1=build_packet(run); pkt2=build_packet(run)
    ids=[e['id'] for vals in pkt1['evidence'].values() for e in vals if isinstance(e,dict)]
    assert ids and ids[0] == 'ev-0001'
    assert ids == [e['id'] for vals in pkt2['evidence'].values() for e in vals if isinstance(e,dict)]
    assert pkt1['evidenceRegistry'][ids[0]]['id'] == ids[0]
    assert {'kind','provenance','category'} <= set(pkt1['evidenceRegistry'][ids[0]])
    compact=compact_verifier_packet(pkt1, max_text_chars=20)
    compact_ids=[e['id'] for vals in compact['evidence'].values() for e in vals if isinstance(e,dict) and 'id' in e]
    assert compact_ids
    assert all(eid in compact['evidenceRegistry'] for eid in compact_ids)


def test_nested_tool_call_is_normalized():
    obj,err=normalize_read_tool_call({'args':{'tool':'list_debug_files'},'reason':'need files'})
    assert err is None
    assert obj['tool']=='list_debug_files'
    assert obj['args']=={}


def test_invalid_tool_call_rejected_without_null_tool():
    obj,err=normalize_read_tool_call({'args':{}}, allowed_tools=['list_debug_files'])
    assert obj is None
    assert 'missing' in err


def test_deterministic_fallback_binary_for_missing_deliverables():
    det=deterministic_result(DebugRun(debugDir='d', objective='Create /app/a.txt'))
    fb=deterministic_fallback_result(det, 'llm failed')
    assert fb['result']==0
    assert fb['verdict']=='failure'
    assert 'llm failed' in fb['riskFlags']


def test_exit_code_only_output_is_weak():
    spec=extract_deliverables('Validate the deliverable')
    assert classify_validation_strength(cmd('python helper.py; echo "EXIT CODE: $?"','EXIT CODE: 0'), spec)[0]=='weak'


def test_transform_plus_display_is_weak():
    spec=extract_deliverables('Create output.html')
    assert classify_validation_strength(cmd('python helper.py output.html && cat output.html','<html>content</html>'), spec)[0]=='weak'


def test_print_only_inline_script_is_weak():
    spec=extract_deliverables('Create /app/result.txt')
    c=cmd("python <<'PY'\nprint(open('/app/result.txt').read())\nPY", 'contents')
    assert classify_validation_strength(c, spec)[0]=='weak'


def test_zero_tests_not_strong_and_zero_failed_not_failure():
    spec=extract_deliverables('Fix project')
    assert classify_validation_strength(cmd('pytest','collected 0 items\n0 failed'), spec)[0] != 'strong'
    assert classify_validation_strength(cmd('pytest','6 passed, 0 failed'), spec)[0] == 'strong'


def test_assertion_failure_is_strong_negative():
    spec=extract_deliverables('Fix project')
    assert classify_validation_strength(cmd('python check.py','AssertionError: mismatch', exit=0), spec)[0]=='failure'


def test_deterministic_fallback_never_returns_success_for_positive_evidence():
    run=DebugRun(debugDir='d', objective='Fix project', status='completed', commands=[cmd('pytest','3 tests passed')])
    det=deterministic_result(run)
    fb=deterministic_fallback_result(det,'llm failed')
    assert fb['result'] is None
    assert fb['recommendedAction']=='inspect_manually'
    assert fb['fallbackPolicy']=='failure_only'


def _det_with(kind='validation', provenance='command_output'):
    return {'evidenceByCategory': {'testValidation': [{'id':'ev-0001','category':'testValidation','evidenceKind':kind,'evidenceProvenance':provenance,'validationStrength':'strong','text':'passed'}]}, 'evidenceRegistry': {'ev-0001': {'id':'ev-0001','category':'testValidation','evidenceKind':kind,'evidenceProvenance':provenance,'validationStrength':'strong','summary':'passed'}}}


def _verdict(refs, result=1, action='accept', warnings=None):
    return {'result':result,'verdict':'success' if result==1 else 'failure','confidence':.9,'recommendedAction':action,'reason':'ok','criticalRequirement':'behavior','directEvidenceForCriticalRequirement':'passed','criticalRequirementCovered':True,'criticalRequirementEvidenceRefs':refs,'criticalRequirementEvidenceMatch':{r:{'matchesCriticalRequirement':True,'requirementCondition':'behavior','evidenceCondition':'behavior','whySameCondition':'same exercised behavior','limitations':[]} for r in refs},'requirementResults':[],'successEvidence':[],'failureEvidence':[],'recoveredFailures':[],'missingEvidence':[],'riskFlags':[],'toolsUsed':[],'warnings':list(warnings or [])}


def test_parse_critical_refs_and_harden_requirement_results():
    obj=_parse('{"type":"final_verdict","result":1,"verdict":"success","confidence":0.8,"recommendedAction":"accept","reason":"ok","criticalRequirementEvidenceRefs":["ev-0001"],"requirementResults":[{"id":"r","requirement":"x","status":"satisfied","evidence":[],"risks":[]},"bad"]}')
    assert obj['criticalRequirementEvidenceRefs'] == ['ev-0001']
    assert len(obj['requirementResults']) == 1
    assert 'invalid_requirement_results_item' in obj['warnings']
    obj2=_parse('{"type":"final_verdict","result":1,"verdict":"success","recommendedAction":"accept","reason":"ok","requirementResults":"bad"}')
    assert obj2['requirementResults'] == []
    assert 'invalid_requirement_results_shape' in obj2['warnings']


def test_coverage_refs_invalid_missing_and_mixed_warn():
    v=_verdict(['ev-9999'])
    assert prove_critical_requirement_coverage(_det_with(), v) is False
    assert v['criticalRequirementEvidenceRefs'] == []
    assert 'critical_requirement_evidence_refs_missing_or_invalid' in v['warnings']
    v=_verdict(['ev-0001','ev-9999'])
    assert prove_critical_requirement_coverage(_det_with(), v) is True
    assert v['criticalRequirementEvidenceRefs'] == ['ev-0001']
    assert 'critical_requirement_evidence_refs_missing_or_invalid' in v['warnings']


def test_coverage_gate_accepts_only_concrete_evidence_and_preserves_warnings():
    for kind, prov in [('validation','command_output'),('runtime_observation','tool_observation'),('behavioral_check','deterministic_analysis'),('artifact_check','file_content')]:
        out=calibrate(_det_with(kind, prov), _verdict(['ev-0001'], warnings=['existing']))
        assert out['recommendedAction'] == 'accept'
        assert out['criticalRequirementCoverageProven'] is True
        assert 'existing' in out['warnings']
    for kind, prov in [('source_inspection','source_diff'),('mutation','source_diff'),('diagnostic','command_output')]:
        out=calibrate(_det_with(kind, prov), _verdict(['ev-0001'], warnings=['existing']))
        assert out['recommendedAction'] == 'inspect_manually'
        assert out['criticalRequirementCoverageProven'] is False
        assert 'critical_requirement_evidence_refs_not_concrete' in out['warnings']
        assert 'accept_downgraded_without_evidence_equivalent_critical_requirement_coverage' in out['warnings']
        assert 'existing' in out['warnings']


def test_coverage_gate_missing_invalid_and_failure_result_behavior():
    out=calibrate(_det_with(), _verdict([]))
    assert out['recommendedAction'] == 'inspect_manually'
    assert 'critical_requirement_evidence_refs_missing_or_invalid' in out['warnings']
    out=calibrate(_det_with(), _verdict(['ev-9999']))
    assert out['recommendedAction'] == 'inspect_manually'
    out=calibrate(_det_with(), _verdict([], result=0, action='reject'))
    assert out['result'] == 0
    assert out['recommendedAction'] == 'reject'


def test_deterministic_fallback_failure_for_active_crash():
    run=DebugRun(debugDir='d', objective='Fix project', commands=[cmd('./check','Segmentation fault (core dumped)', exit=0)])
    fb=deterministic_fallback_result(deterministic_result(run),'llm failed')
    assert fb['result']==0


def test_failed_validation_followed_only_by_inspection_remains_active():
    run=DebugRun(debugDir='d', objective='Fix project', commands=[cmd('pytest','AssertionError: bad',0), CommandRecord(command='cat log.txt', stdout='log', exitCode=0, index=2)])
    pkt=build_packet(run)
    assert pkt['deterministicChecks']['activeFailureCount'] >= 1


def test_failed_validation_before_mutation_and_later_strong_pass_can_recover():
    run=DebugRun(debugDir='d', objective='Fix project', commands=[cmd('pytest','AssertionError: bad',0), CommandRecord(command='cat > fixed.txt', stdout='', exitCode=0, index=2), CommandRecord(command='pytest', stdout='3 tests passed', exitCode=0, index=3)])
    pkt=build_packet(run)
    assert pkt['deterministicChecks']['recoveredFailureCount'] >= 1 or pkt['deterministicChecks']['activeFailureCount'] == 0


def test_memory_diagnostics_still_reachable_not_failure():
    spec=extract_deliverables('Fix memory behavior')
    out='LEAK SUMMARY:\n still reachable: 4,096 bytes in 1 blocks\n definitely lost: 0 bytes\n possibly lost: 0 bytes\nERROR SUMMARY: 0 errors'
    assert classify_validation_strength(cmd('valgrind ./app', out), spec)[0] != 'failure'


def test_memory_diagnostics_lost_and_error_summary_are_failure():
    spec=extract_deliverables('Fix memory behavior')
    assert classify_validation_strength(cmd('valgrind ./app','definitely lost: 12 bytes in 1 blocks'), spec)[0]=='failure'
    assert classify_validation_strength(cmd('valgrind ./app','possibly lost: 12 bytes in 1 blocks'), spec)[0]=='failure'
    assert classify_validation_strength(cmd('valgrind ./app','ERROR SUMMARY: 3 errors from 3 contexts'), spec)[0]=='failure'
    assert classify_validation_strength(cmd('valgrind ./app','ERROR SUMMARY: 0 errors from 0 contexts'), spec)[0] != 'failure'


def test_deliverable_extraction_ignores_command_args_flags_stdout_stderr():
    run=DebugRun(debugDir='d', objective='Build the project', commands=[cmd('g++ /app/main.cpp -L/usr/lib -I/include -o /app/release && /app/release','wrote /app/from_stdout.txt','error references /app/from_stderr.txt')], finalSummary={'changed_files':['/app/main.cpp']})
    spec=extract_deliverables(run.objective, run)
    assert spec.required_files == []
    pkt=build_packet(run)
    assert any('/app/main.cpp' in e['text'] for e in pkt['evidence']['fileMutation'])
    assert pkt['deliverableAssessment']['requiredDeliverables'] == []


def test_objective_named_deliverables_preserved():
    spec=extract_deliverables('Create /app/compress.py and /app/decompress.py')
    assert '/app/compress.py' in spec.required_files
    assert '/app/decompress.py' in spec.required_files


def test_compact_tool_result_text_truncates_and_repeated_summary_short():
    from villani_ops.verifier.llm import compact_tool_result_text, _tool_cache_key
    assert len(compact_tool_result_text('x'*5000, 1000)) < 1200
    assert _tool_cache_key('read_debug_file', {'path':'a'}) == _tool_cache_key('read_debug_file', {'path':'a'})


def test_evidence_equivalence_missing_false_and_material_limitations_downgrade():
    for match in [{}, {'ev-0001': {'matchesCriticalRequirement': False, 'requirementCondition':'edge','evidenceCondition':'normal','whySameCondition':'','limitations':['weaker nearby']}}, {'ev-0001': {'matchesCriticalRequirement': True, 'requirementCondition':'edge','evidenceCondition':'edge','whySameCondition':'same','limitations':['partial only']}}]:
        v=_verdict(['ev-0001'])
        v['criticalRequirementEvidenceMatch']=match
        out=calibrate(_det_with(), v)
        assert out['recommendedAction']=='inspect_manually'
        assert out['criticalRequirementCoverageProven'] is False


def test_nearby_condition_concrete_validation_downgrades_but_same_condition_accepts():
    v=_verdict(['ev-0001'])
    v['criticalRequirement']='abnormal edge condition'
    v['criticalRequirementEvidenceMatch']={'ev-0001': {'matchesCriticalRequirement': False, 'requirementCondition':'abnormal edge condition','evidenceCondition':'normal nearby condition','whySameCondition':'','limitations':['tests weaker nearby condition']}}
    out=calibrate(_det_with(), v)
    assert out['recommendedAction']=='inspect_manually'
    v=_verdict(['ev-0001'])
    v['criticalRequirementEvidenceMatch']={'ev-0001': {'matchesCriticalRequirement': True, 'requirementCondition':'abnormal edge condition','evidenceCondition':'abnormal edge condition','whySameCondition':'validation exercises the same edge condition','limitations':[]}}
    out=calibrate(_det_with(), v)
    assert out['recommendedAction']=='accept'


def test_parser_hardens_critical_requirement_evidence_match_shapes():
    obj=_parse('{"type":"final_verdict","result":1,"verdict":"success","recommendedAction":"accept","reason":"ok","criticalRequirementEvidenceMatch":"bad","requirementResults":[]}')
    assert obj['criticalRequirementEvidenceMatch']=={}
    assert 'invalid_critical_requirement_evidence_match_shape' in obj['warnings']
    obj=_parse('{"type":"final_verdict","result":1,"verdict":"success","recommendedAction":"accept","reason":"ok","criticalRequirementEvidenceMatch":{"ev-1":"bad","ev-2":{"matchesCriticalRequirement":"true","requirementCondition":"r","evidenceCondition":"e","whySameCondition":"w","limitations":"partial"}},"requirementResults":[]}')
    assert 'ev-1' not in obj['criticalRequirementEvidenceMatch']
    assert obj['criticalRequirementEvidenceMatch']['ev-2']['matchesCriticalRequirement'] is False
    assert obj['criticalRequirementEvidenceMatch']['ev-2']['limitations']==['partial']


def test_packet_compaction_preserves_registry_ids_summaries_and_caps_raw():
    long='x'*3000
    packet={'evidence':{'testValidation':[{'id':'ev-0001','text':long,'summary':'edge condition validation','condition':'edge'}]},'evidenceRegistry':{'ev-0001':{'id':'ev-0001','summary':'edge condition validation','condition':'edge','text':long}}}
    compact=compact_verifier_packet(packet, max_text_chars=80)
    assert 'ev-0001' in compact['evidenceRegistry']
    assert compact['evidenceRegistry']['ev-0001']['summary']=='edge condition validation'
    assert compact['evidenceRegistry']['ev-0001']['condition']=='edge'
    assert len(compact['evidenceRegistry']['ev-0001']['text']) < 200


def test_deterministic_fallback_inconclusive_for_diagnostic_and_tool_loop_only():
    det={'evidenceByCategory': {'activeFailures':[{'id':'d','kind':'diagnostic','diagnosticOnly':True}]}, 'deliverableAssessment': {}}
    fb=deterministic_fallback_result(det,'tool loop failed')
    assert fb['result'] is None
    assert fb['recommendedAction']=='inspect_manually'
    fb=deterministic_fallback_result({'evidenceByCategory': {}, 'deliverableAssessment': {}}, 'tool loop failed')
    assert fb['result'] is None
