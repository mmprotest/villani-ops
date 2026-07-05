import pytest
from villani_ops.verifier.types import CommandRecord, DebugRun, ToolCallRecord
from villani_ops.verifier.extract import extract_deliverables
from villani_ops.verifier.deterministic import _item, classify_validation_strength, build_packet, deterministic_result
from villani_ops.verifier.llm import compact_verifier_packet, normalize_read_tool_call, deterministic_fallback_result


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
