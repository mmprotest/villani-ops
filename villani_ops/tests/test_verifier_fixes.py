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
