from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
import json, threading, time, uuid

from pydantic import BaseModel, Field, ConfigDict
from villani_ops.core.durable_io import durable_write_json

UsageSource = Literal['provider_response','runner_result','debug_trace','unavailable','estimated']

class CostResult(BaseModel):
    input_cost: float | None = None
    output_cost: float | None = None
    total_cost: float | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None

class UsageRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str | None = None
    attempt_id: str | None = None
    subtask_id: str | None = None
    node_id: str | None = None
    phase: str
    role: str
    backend_name: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    total_cost: float | None = None
    currency: str = 'USD'
    usage_source: UsageSource
    estimated: bool = False
    unavailable_reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None

class UsageBucket(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    total_cost: float = 0.0
    calls_count: int = 0
    unavailable_calls_count: int = 0
    estimated_calls_count: int = 0

class UsageSummary(UsageBucket):
    by_role: dict[str, UsageBucket] = Field(default_factory=dict)
    by_backend: dict[str, UsageBucket] = Field(default_factory=dict)
    by_model: dict[str, UsageBucket] = Field(default_factory=dict)
    by_attempt: dict[str, UsageBucket] = Field(default_factory=dict)
    by_phase: dict[str, UsageBucket] = Field(default_factory=dict)


def _maybe_int(v: Any) -> int | None:
    if v is None: return None
    try: return int(v)
    except (TypeError, ValueError): return None


def extract_usage_tokens(response: Any) -> dict[str, int | None] | None:
    data = response
    if not isinstance(data, dict):
        data = getattr(response, 'raw_response', None) or getattr(response, 'usage', None)
    usage = data.get('usage') if isinstance(data, dict) else None
    if usage is None and isinstance(data, dict) and any(k in data for k in ('prompt_tokens','completion_tokens','input_tokens','output_tokens','total_tokens')):
        usage = data
    if not isinstance(usage, dict): return None
    inp = _maybe_int(usage.get('prompt_tokens', usage.get('input_tokens')))
    out = _maybe_int(usage.get('completion_tokens', usage.get('output_tokens')))
    total = _maybe_int(usage.get('total_tokens'))
    if total is None and (inp is not None or out is not None): total = (inp or 0) + (out or 0)
    if inp is None and out is None and total is None: return None
    return {'input_tokens': inp, 'output_tokens': out, 'total_tokens': total}


def calculate_usage_cost(input_tokens: int | None, output_tokens: int | None, backend: Any | None) -> CostResult:
    inp_price = getattr(backend, 'input_cost_per_million', None) if backend is not None else None
    out_price = getattr(backend, 'output_cost_per_million', None) if backend is not None else None
    input_cost = (input_tokens / 1_000_000 * inp_price) if input_tokens is not None and inp_price is not None else None
    output_cost = (output_tokens / 1_000_000 * out_price) if output_tokens is not None and out_price is not None else None
    total_cost = input_cost + output_cost if input_cost is not None and output_cost is not None else None
    return CostResult(input_cost=input_cost, output_cost=output_cost, total_cost=total_cost, input_cost_per_million=inp_price, output_cost_per_million=out_price)


def usage_record_from_response(*, run_id: str, phase: str, role: str, backend: Any | None, response: Any, usage_source: UsageSource='provider_response', attempt_id: str | None=None, subtask_id: str | None=None, unavailable_reason: str | None=None) -> UsageRecord:
    tokens = extract_usage_tokens(getattr(response, 'raw_response', None) or response)
    if tokens is None and isinstance(getattr(response, 'usage', None), dict): tokens = extract_usage_tokens(getattr(response, 'usage'))
    source = usage_source if tokens is not None else 'unavailable'
    cost = calculate_usage_cost(tokens.get('input_tokens') if tokens else None, tokens.get('output_tokens') if tokens else None, backend)
    return UsageRecord(run_id=run_id, phase=phase, role=role, attempt_id=attempt_id, subtask_id=subtask_id, backend_name=getattr(backend,'name',None), provider=getattr(backend,'provider',None), model=getattr(response,'model',None) or getattr(backend,'model',None), input_tokens=tokens.get('input_tokens') if tokens else None, output_tokens=tokens.get('output_tokens') if tokens else None, total_tokens=tokens.get('total_tokens') if tokens else None, input_cost_per_million=cost.input_cost_per_million, output_cost_per_million=cost.output_cost_per_million, input_cost=cost.input_cost, output_cost=cost.output_cost, total_cost=cost.total_cost, usage_source=source, unavailable_reason=None if tokens else (unavailable_reason or 'provider_response_missing_usage'))


def usage_record_from_runner(*, run_id: str, phase: str, role: str, backend: Any | None, result: Any, attempt_id: str, subtask_id: str | None=None) -> UsageRecord:
    inp = _maybe_int(getattr(result, 'input_tokens', None)); out = _maybe_int(getattr(result, 'output_tokens', None))
    total = _maybe_int(getattr(result, 'total_tokens', None))
    status = getattr(result, 'token_accounting_status', None)
    has_usage = (inp is not None and inp > 0) or (out is not None and out > 0) or (total is not None and total > 0) or status in {'verified','summary_only','mismatch'}
    if total is None and (inp is not None or out is not None): total=(inp or 0)+(out or 0)
    cost = calculate_usage_cost(inp, out, backend)
    return UsageRecord(run_id=run_id, phase=phase, role=role, attempt_id=attempt_id, subtask_id=subtask_id, backend_name=getattr(backend,'name',None), provider=getattr(backend,'provider',None), model=getattr(backend,'model',None), input_tokens=inp if has_usage else None, output_tokens=out if has_usage else None, total_tokens=total if has_usage else None, input_cost_per_million=cost.input_cost_per_million, output_cost_per_million=cost.output_cost_per_million, input_cost=cost.input_cost if has_usage else None, output_cost=cost.output_cost if has_usage else None, total_cost=cost.total_cost if has_usage else None, usage_source='runner_result' if has_usage else 'unavailable', unavailable_reason=None if has_usage else 'runner_result_missing_usage')

class UsageRecorder:
    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir=Path(run_dir); self.run_id=run_id; self.path=self.run_dir/'usage.jsonl'; self.run_dir.mkdir(parents=True, exist_ok=True); self._lock=threading.Lock(); self.records:list[UsageRecord]=[]; self._summary_write_interval_seconds=3.0; self._last_summary_write_at=0.0
    def record(self, usage: UsageRecord) -> None:
        if usage.run_id is None: usage.run_id=self.run_id
        line=json.dumps(usage.model_dump(mode='json'), ensure_ascii=False, default=str)+'\n'
        with self._lock:
            self.records.append(usage)
            with self.path.open('a', encoding='utf-8', newline='\n') as f: f.write(line); f.flush()
            now=time.monotonic()
            if now - self._last_summary_write_at >= self._summary_write_interval_seconds:
                try:
                    self._write_artifacts_unlocked()
                    self._last_summary_write_at=now
                except Exception as e:
                    print('[agentic] Warning: usage summary write failed; usage.jsonl remains available')
    def summarize(self) -> UsageSummary:
        with self._lock:
            records=list(self.records)
        return self._summarize_records(records)
    def _summarize_records(self, records: list[UsageRecord]) -> UsageSummary:
        s=UsageSummary()
        for r in records:
            _add(s,r)
            for key, val, attr in [('by_role',r.role,'by_role'),('by_backend',r.backend_name,'by_backend'),('by_model',r.model,'by_model'),('by_attempt',r.attempt_id,'by_attempt'),('by_phase',r.phase,'by_phase')]:
                if val:
                    d=getattr(s, attr); d.setdefault(str(val), UsageBucket()); _add(d[str(val)], r)
        return s
    def write_artifacts(self) -> None:
        with self._lock:
            self._write_artifacts_unlocked()
            self._last_summary_write_at=time.monotonic()
    def _write_artifacts_unlocked(self) -> None:
        records=[r.model_dump(mode='json') for r in self.records]
        summary=self._summarize_records(list(self.records)).model_dump(mode='json')
        _atomic_json(self.run_dir/'usage.json', {'records':records,'summary':summary})
        unavailable=[r for r in records if r.get('usage_source')=='unavailable']
        compact={k:summary[k] for k in ['total_cost','input_cost','output_cost','total_tokens','input_tokens','output_tokens','calls_count','by_role','by_backend','by_model','by_attempt']}
        compact['unavailable_usage']=unavailable
        compact['unavailable_calls_count']=summary['unavailable_calls_count']
        _atomic_json(self.run_dir/'cost_summary.json', compact)

def _add(b: UsageBucket, r: UsageRecord) -> None:
    b.calls_count += 1
    if r.usage_source == 'unavailable': b.unavailable_calls_count += 1
    if r.estimated: b.estimated_calls_count += 1
    b.input_tokens += r.input_tokens or 0; b.output_tokens += r.output_tokens or 0; b.total_tokens += r.total_tokens or 0
    b.input_cost += r.input_cost or 0.0; b.output_cost += r.output_cost or 0.0; b.total_cost += r.total_cost or 0.0

def _atomic_json(path: Path, data: Any) -> None:
    durable_write_json(Path(path), data, indent=2)
