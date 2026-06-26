from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

from villani_ops.core.backend import Backend
from villani_ops.telemetry.usage import (
    UsageRecorder, UsageRecord, calculate_usage_cost, extract_usage_tokens,
    usage_record_from_runner,
)


def backend(input_cost=0.14, output_cost=1.0):
    return Backend(name='b', provider='openai-compatible', base_url='http://x', model='m', input_cost_per_million=input_cost, output_cost_per_million=output_cost)


def test_cost_calculation_per_million():
    c=calculate_usage_cost(1_000_000, 500_000, backend())
    assert c.input_cost == 0.14
    assert c.output_cost == 0.5
    assert c.total_cost == 0.64


def test_cost_missing_tokens_or_price_is_unavailable():
    assert calculate_usage_cost(None, 10, backend()).input_cost is None
    assert calculate_usage_cost(10, 10, None).total_cost is None


def test_extract_usage_variants_and_missing():
    assert extract_usage_tokens({'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15}}) == {'input_tokens':12,'output_tokens':3,'total_tokens':15}
    assert extract_usage_tokens({'usage': {'input_tokens': 2, 'output_tokens': 4}}) == {'input_tokens':2,'output_tokens':4,'total_tokens':6}
    assert extract_usage_tokens({'usage': {'input_tokens': 7, 'output_tokens': 8}}) == {'input_tokens':7,'output_tokens':8,'total_tokens':15}
    assert extract_usage_tokens({'choices': []}) is None


def test_usage_recorder_writes_artifacts_thread_safe(tmp_path):
    rec=UsageRecorder(tmp_path, 'r1')
    def add(i):
        rec.record(UsageRecord(run_id='r1', phase='p', role='coding', attempt_id=f'a{i%2}', backend_name='b', model='m', input_tokens=10, output_tokens=5, total_tokens=15, input_cost=0.1, output_cost=0.2, total_cost=0.3, usage_source='runner_result'))
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(add, range(20)))
    rec.write_artifacts()
    assert len((tmp_path/'usage.jsonl').read_text(encoding='utf-8').splitlines()) == 20
    usage=json.loads((tmp_path/'usage.json').read_text(encoding='utf-8'))
    summary=json.loads((tmp_path/'cost_summary.json').read_text(encoding='utf-8'))
    assert usage['summary']['total_tokens'] == 300
    assert summary['by_role']['coding']['calls_count'] == 20
    assert summary['by_attempt']['a0']['total_tokens'] == 150


def test_runner_missing_usage_records_unavailable():
    r=usage_record_from_runner(run_id='r', phase='candidate_attempt', role='coding', backend=backend(), result=SimpleNamespace(input_tokens=0, output_tokens=0, token_accounting_status='missing'), attempt_id='candidate_001')
    assert r.usage_source == 'unavailable'
    assert r.unavailable_reason == 'runner_result_missing_usage'
    assert r.total_cost is None
