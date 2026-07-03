from __future__ import annotations
import json
from pathlib import Path
from .parse_jsonl import parse_jsonl
from .types import *
def _read_json(path:Path, missing:list[str]):
    if not path.exists(): missing.append(path.name); return None
    return json.loads(path.read_text(encoding='utf-8'))
def _cmd(r,i): return CommandRecord(ts=r.get('ts'),toolCallId=r.get('tool_call_id'),command=r.get('command'),cwd=r.get('cwd'),exitCode=r.get('exit_code'),stdout=r.get('stdout'),stderr=r.get('stderr'),truncated=bool(r.get('truncated')),event=r.get('event'),raw=r,index=i)
def _tool(r,i): return ToolCallRecord(toolCallId=r.get('tool_call_id') or r.get('toolCallId'),turnIndex=r.get('turn_index') or r.get('turnIndex'),toolName=r.get('tool_name') or r.get('toolName') or r.get('name'),toolCategory=r.get('tool_category') or r.get('toolCategory'),startedAt=r.get('started_at') or r.get('startedAt'),endedAt=r.get('ended_at') or r.get('endedAt'),durationMs=r.get('duration_ms') or r.get('durationMs'),status=r.get('status'),args=r.get('args') or r.get('arguments') or r.get('normalized_args_summary'),resultSummary=r.get('result_summary') or r.get('resultSummary'),error=r.get('error'),raw=r,index=i)
def load_debug_run(debug_dir:str|Path)->DebugRun:
    d=Path(debug_dir)
    if not d.exists() or not d.is_dir(): raise FileNotFoundError(f'debug directory does not exist: {d}')
    missing=[]; warnings=[]
    meta=_read_json(d/'session_meta.json', missing)
    if meta is None: raise FileNotFoundError('session_meta.json is required')
    summary=_read_json(d/'summary.json', missing); final=_read_json(d/'final_summary.json', missing)
    run=DebugRun(debugDir=str(d), sessionMeta=meta, summary=summary, finalSummary=final)
    run.runId=meta.get('run_id'); run.objective=meta.get('objective'); run.repoFromMetadata=meta.get('repo'); run.model=meta.get('model'); run.provider=meta.get('provider'); run.startedAt=meta.get('created_at')
    for obj in (final,summary):
        if isinstance(obj,dict):
            run.status=run.status or obj.get('status'); run.durationMs=run.durationMs or obj.get('duration_ms')
    for name, mapper, attr, optional in [('commands.jsonl',_cmd,'commands',False),('tool_calls.jsonl',_tool,'toolCalls',False),('patches.jsonl',lambda r,i:PatchRecord(r.get('file_path'),r.get('ok'),r,i),'patches',False),('model_responses.jsonl',lambda r,i:ModelResponseRecord(r.get('text') or r.get('content') or r.get('message'),r,i),'modelResponses',False),('validations.jsonl',lambda r,i:ValidationRecord(r,i),'validations',True)]:
        try:
            recs,w,present=parse_jsonl(d/name, optional=optional); warnings+=w
            if not present: missing.append(name)
            setattr(run, attr, [mapper(r,i) for i,r in enumerate(recs)])
        except FileNotFoundError:
            missing.append(name)
    run.toolCalls.sort(key=lambda t: (t.turnIndex if t.turnIndex is not None else 10**9, t.startedAt or '', t.index))
    run.parseWarnings=warnings; run.missingArtifacts=missing
    return run
