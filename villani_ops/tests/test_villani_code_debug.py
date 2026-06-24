import json
from pathlib import Path

from villani_ops.runners.villani_code_debug import parse_villani_code_debug_artifact


def write_jsonl(path: Path, rows):
    path.write_text('\n'.join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + '\n')


def summary(**kw):
    data={"run_id":"20260622T191913_880358Z","status":"completed","started_at":"2026-06-22T19:19:13Z","duration_ms":103098,"turn_count":4,"total_tool_calls":3,"tool_calls_by_name":{"Ls":1,"Write":1,"Read":1},"total_file_reads":1,"total_file_writes":1,"model_requests":4,"model_failures":0,"tokens_input":11532,"tokens_output":7390,"commands_executed":1,"commands_failed":0}
    data.update(kw); return data


def test_parser_reads_summary_and_verifies_model_responses(tmp_path):
    (tmp_path/'final_summary.json').write_text(json.dumps(summary()))
    pairs=[(2205,131),(2257,6698),(3132,197),(3938,364)]
    write_jsonl(tmp_path/'model_responses.jsonl',[{"usage":{"prompt_tokens":i,"completion_tokens":o,"total_tokens":i+o}} for i,o in pairs])
    write_jsonl(tmp_path/'tool_calls.jsonl',[{"tool_name":"Ls"},{"tool_name":"Write","tool_category":"file_mutation"},{"tool_name":"Read","normalized_args_summary":{"file_path":"a.py"}}])
    t=parse_villani_code_debug_artifact(tmp_path)
    assert t.input_tokens == 11532
    assert t.output_tokens == 7390
    assert t.total_tokens == 18922
    assert t.token_accounting_status == "verified"
    assert t.model_requests == 4
    assert t.total_tool_calls == 3


def test_parser_detects_mismatch(tmp_path):
    (tmp_path/'final_summary.json').write_text(json.dumps(summary(tokens_input=10,tokens_output=20)))
    write_jsonl(tmp_path/'model_responses.jsonl',[{"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}])
    t=parse_villani_code_debug_artifact(tmp_path)
    assert t.token_accounting_status == "mismatch"
    assert t.token_accounting_warnings


def test_parser_handles_missing_files(tmp_path):
    t=parse_villani_code_debug_artifact(tmp_path)
    assert t.token_accounting_status == "missing"
    assert t.input_tokens == 0
    assert t.output_tokens == 0
    assert t.token_accounting_warnings


def test_ls_is_not_substantive_file_read(tmp_path):
    (tmp_path/'final_summary.json').write_text(json.dumps(summary()))
    write_jsonl(tmp_path/'model_responses.jsonl',[{"usage":{"prompt_tokens":11532,"completion_tokens":7390,"total_tokens":18922}}])
    write_jsonl(tmp_path/'tool_calls.jsonl',[{"tool_name":"Ls","started_at":"2026-06-22T19:19:14Z"},{"tool_name":"Write","started_at":"2026-06-22T19:19:15Z"},{"tool_name":"Read","started_at":"2026-06-22T19:19:16Z","normalized_args_summary":{"file_path":"x"}}])
    t=parse_villani_code_debug_artifact(tmp_path)
    assert t.first_tool_call_index == 1
    assert t.first_file_mutation_tool_index == 2
    assert t.first_substantive_file_read_tool_index == 3


def test_malformed_jsonl_does_not_crash(tmp_path):
    (tmp_path/'final_summary.json').write_text(json.dumps(summary(tokens_input=1,tokens_output=2)))
    write_jsonl(tmp_path/'model_responses.jsonl',['{bad', {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}])
    t=parse_villani_code_debug_artifact(tmp_path)
    assert t.input_tokens == 1
    assert t.token_accounting_warnings


def test_parser_sorts_mixed_timezone_and_missing_timestamps_safely(tmp_path):
    (tmp_path/'final_summary.json').write_text(json.dumps(summary(total_tool_calls=5, total_file_reads=1, total_file_writes=1)))
    write_jsonl(tmp_path/'model_responses.jsonl',[{"usage":{"prompt_tokens":11532,"completion_tokens":7390,"total_tokens":18922}}])
    write_jsonl(tmp_path/'tool_calls.jsonl',[
        {"tool_name":"Ls","turn_index":3},
        {"tool_name":"Read","started_at":"2026-06-22T19:19:13.100000+00:00","normalized_args_summary":{"file_path":"x.py"}},
        {"tool_name":"Write","tool_category":"file_mutation","started_at":"2026-06-22T19:19:14.100000"},
        {"tool_name":"Bash","tool_category":"command","turn_index":1,"args":{"command":"echo hi"}},
        {"tool_name":"Shell","tool_category":"command","turn_index":1,"args":{"command":"echo bye"}},
    ])
    t=parse_villani_code_debug_artifact(tmp_path)
    assert t.first_tool_call_index is not None
    assert t.first_file_mutation_tool_index is not None
    assert t.first_substantive_file_read_tool_index is not None
    assert t.first_substantive_file_read_tool_index == 1
    assert t.first_file_mutation_tool_index == 2
    assert t.first_command_tool_index == 3


def test_parser_treats_malformed_timestamps_like_missing_timestamps(tmp_path):
    (tmp_path/'final_summary.json').write_text(json.dumps(summary(total_tool_calls=3, total_file_reads=1, total_file_writes=1)))
    write_jsonl(tmp_path/'model_responses.jsonl',[{"usage":{"prompt_tokens":11532,"completion_tokens":7390,"total_tokens":18922}}])
    write_jsonl(tmp_path/'tool_calls.jsonl',[
        {"tool_name":"Read","started_at":"not-a-timestamp","turn_index":2,"normalized_args_summary":{"file_path":"x.py"}},
        {"tool_name":"Write","tool_category":"file_mutation","started_at":"2026-06-22T19:19:13.100000+00:00"},
        {"tool_name":"Bash","tool_category":"command","turn_index":1,"args":{"command":"echo hi"}},
    ])
    t=parse_villani_code_debug_artifact(tmp_path)
    assert t.first_file_mutation_tool_index == 1
    assert t.first_command_tool_index == 2
    assert t.first_substantive_file_read_tool_index == 3
    assert any('Malformed tool call timestamp' in w for w in t.token_accounting_warnings)


def test_parser_resolves_nested_trace_dir(tmp_path):
    trace=tmp_path/'villani_code_debug'/'20260624T034357_216114Z'
    trace.mkdir(parents=True)
    (trace/'final_summary.json').write_text(json.dumps(summary(tokens_input=7,tokens_output=3)))
    write_jsonl(trace/'model_responses.jsonl',[{"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}])
    write_jsonl(trace/'tool_calls.jsonl',[{"tool_name":"Read","normalized_args_summary":{"file_path":"x.py"}}])
    t=parse_villani_code_debug_artifact(tmp_path/'villani_code_debug')
    assert t.input_tokens > 0
    assert t.output_tokens > 0
    assert t.token_accounting_status == 'verified'
    assert t.resolved_trace_dir.endswith('20260624T034357_216114Z')


def test_parser_chooses_newest_nested_trace_dir(tmp_path):
    parent=tmp_path/'villani_code_debug'
    old=parent/'old'; new=parent/'new'
    old.mkdir(parents=True); new.mkdir()
    (old/'final_summary.json').write_text(json.dumps(summary(tokens_input=1,tokens_output=1)))
    (new/'final_summary.json').write_text(json.dumps(summary(tokens_input=9,tokens_output=2)))
    import os, time
    os.utime(old/'final_summary.json',(1,1)); os.utime(new/'final_summary.json',(2,2))
    write_jsonl(new/'model_responses.jsonl',[{"usage":{"prompt_tokens":9,"completion_tokens":2,"total_tokens":11}}])
    t=parse_villani_code_debug_artifact(parent)
    assert t.resolved_trace_dir.endswith('new')
    assert t.input_tokens == 9
