from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from villani_ops.core.backend import Backend


class VillaniCodeTelemetry(BaseModel):
    debug_dir: str
    summary_path: str | None = None
    final_summary_path: str | None = None
    run_id: str | None = None
    status: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    turn_count: int = 0

    model_requests: int = 0
    model_failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    total_tool_calls: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    tool_failures_by_name: dict[str, int] = Field(default_factory=dict)
    total_file_reads: int = 0
    total_file_writes: int = 0
    total_file_patches_applied: int = 0
    total_file_patch_failures: int = 0
    commands_executed: int = 0
    commands_failed: int = 0

    files_touched: int = 0
    unique_files_read: int = 0
    unique_files_written: int = 0
    changed_files_from_debug: list[str] = Field(default_factory=list)

    first_tool_call_index: int | None = None
    first_tool_call_seconds: float | None = None
    first_substantive_file_read_tool_index: int | None = None
    first_substantive_file_read_seconds: float | None = None
    first_file_mutation_tool_index: int | None = None
    first_file_mutation_seconds: float | None = None
    first_command_tool_index: int | None = None
    first_command_seconds: float | None = None

    token_accounting_status: str = "missing"
    token_accounting_warnings: list[str] = Field(default_factory=list)
    raw_summary: dict[str, Any] = Field(default_factory=dict)


def _read_json(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        warnings.append(f"Could not parse {path.name}: {e}")
        return None


def _jsonl(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    rows=[]
    if not path.exists():
        warnings.append(f"Missing optional artifact: {path.name}")
        return rows
    for i,line in enumerate(path.read_text(errors='replace').splitlines(),1):
        if not line.strip():
            continue
        try:
            v=json.loads(line)
            if isinstance(v, dict): rows.append(v)
        except Exception as e:
            warnings.append(f"Malformed JSONL in {path.name} line {i}: {e}")
    return rows


def _parse_ts(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z','+00:00'))
    except ValueError:
        return None


def _normalized_ts(value: Any) -> datetime | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _sort_timestamp(value: Any, warnings: list[str] | None = None) -> float:
    ts = _normalized_ts(value)
    if ts is None:
        if isinstance(value, str) and value:
            msg = f"Malformed tool call timestamp: {value}"
            if warnings is not None and msg not in warnings:
                warnings.append(msg)
        return float('inf')
    return ts.timestamp()


def _tool_name(t: dict[str, Any]) -> str:
    return str(t.get('tool_name') or t.get('name') or t.get('tool') or '')


def _args(t: dict[str, Any]) -> dict[str, Any]:
    a=t.get('normalized_args_summary') or t.get('args_summary') or t.get('args') or {}
    return a if isinstance(a, dict) else {}


def _is_command(t: dict[str, Any]) -> bool:
    name=_tool_name(t).lower(); args=_args(t)
    return bool(args.get('command')) or t.get('tool_category')=='command' or name in {'bash','shell','exec','runcommand','terminal','exec_command'} or 'shell' in name


def parse_villani_code_debug_artifact(debug_dir: Path) -> VillaniCodeTelemetry:
    warnings: list[str]=[]; debug_dir=Path(debug_dir)
    final=debug_dir/'final_summary.json'; summ=debug_dir/'summary.json'
    summary_path = final if final.exists() else (summ if summ.exists() else None)
    summary = _read_json(summary_path, warnings) if summary_path else None
    tel=VillaniCodeTelemetry(debug_dir=str(debug_dir), token_accounting_warnings=warnings)
    if final.exists(): tel.final_summary_path=str(final)
    if summ.exists(): tel.summary_path=str(summ)
    if not summary:
        tel.token_accounting_status='missing'; warnings.append('No final_summary.json or summary.json found.'); tel.token_accounting_warnings=warnings
        return tel
    tel.raw_summary=summary
    for k in ['run_id','status','started_at','ended_at']:
        setattr(tel,k,summary.get(k))
    for k in ['duration_ms','turn_count','model_requests','model_failures','total_tool_calls','total_file_reads','total_file_writes','total_file_patches_applied','total_file_patch_failures','commands_executed','commands_failed','files_touched','unique_files_read','unique_files_written']:
        if summary.get(k) is not None: setattr(tel,k,int(summary.get(k) or 0))
    tel.input_tokens=int(summary.get('tokens_input') or summary.get('input_tokens') or 0)
    tel.output_tokens=int(summary.get('tokens_output') or summary.get('output_tokens') or 0)
    tel.total_tokens=tel.input_tokens+tel.output_tokens
    tel.tool_calls_by_name=dict(summary.get('tool_calls_by_name') or {})
    tel.tool_failures_by_name=dict(summary.get('tool_failures_by_name') or {})
    tel.changed_files_from_debug=list(summary.get('changed_files_from_debug') or summary.get('changed_files') or [])

    responses_path=debug_dir/'model_responses.jsonl'
    if responses_path.exists():
        rin=rout=rtot=0; count=0
        for r in _jsonl(responses_path, warnings):
            u=r.get('usage') or {}
            if isinstance(u, dict):
                rin += int(u.get('prompt_tokens') or 0); rout += int(u.get('completion_tokens') or 0); rtot += int(u.get('total_tokens') or ((u.get('prompt_tokens') or 0)+(u.get('completion_tokens') or 0))); count+=1
        if count and (rin,rout,rtot)==(tel.input_tokens,tel.output_tokens,tel.total_tokens): tel.token_accounting_status='verified'
        else:
            tel.token_accounting_status='mismatch'; warnings.append(f"Token totals mismatch: summary input/output/total={tel.input_tokens}/{tel.output_tokens}/{tel.total_tokens}; model_responses input/output/total={rin}/{rout}/{rtot}.")
    else:
        tel.token_accounting_status='summary_only'; warnings.append('Missing optional artifact: model_responses.jsonl')

    tools=_jsonl(debug_dir/'tool_calls.jsonl', warnings) if (debug_dir/'tool_calls.jsonl').exists() else []
    if tools:
        sorted_tools=sorted(enumerate(tools), key=lambda it: (_sort_timestamp(it[1].get('started_at'), warnings), it[1].get('turn_index') if it[1].get('turn_index') is not None else 10**9, it[0]))
        tel.total_tool_calls=len(sorted_tools)
        tel.tool_calls_by_name=dict(Counter(_tool_name(t) for _,t in sorted_tools if _tool_name(t))) or tel.tool_calls_by_name
        start=_normalized_ts(tel.started_at)
        for idx,(_,t) in enumerate(sorted_tools,1):
            ts=_normalized_ts(t.get('started_at'))
            sec=(ts-start).total_seconds() if ts and start else None
            name=_tool_name(t); cat=t.get('tool_category'); args=_args(t)
            if tel.first_tool_call_index is None:
                tel.first_tool_call_index=idx; tel.first_tool_call_seconds=sec
            if tel.first_substantive_file_read_tool_index is None and (name=='Read' or (cat=='file_read' and args.get('file_path'))):
                tel.first_substantive_file_read_tool_index=idx; tel.first_substantive_file_read_seconds=sec
            if tel.first_file_mutation_tool_index is None and (cat=='file_mutation' or name in {'Write','Edit','MultiEdit','Patch','ApplyPatch'}):
                tel.first_file_mutation_tool_index=idx; tel.first_file_mutation_seconds=sec
            if tel.first_command_tool_index is None and _is_command(t):
                tel.first_command_tool_index=idx; tel.first_command_seconds=sec
    else:
        warnings.append('Missing optional artifact: tool_calls.jsonl')
    tel.token_accounting_warnings=warnings
    return tel


def write_runner_telemetry(debug_dir: Path, out_path: Path, backend: Backend | None = None) -> VillaniCodeTelemetry:
    tel=parse_villani_code_debug_artifact(debug_dir)
    data=tel.model_dump(mode='json')
    if backend is not None:
        data['backend']={'name':backend.name,'provider':backend.provider,'model':backend.model}
    Path(out_path).write_text(json.dumps(data, indent=2))
    return tel
