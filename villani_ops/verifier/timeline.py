from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any

@dataclass
class TimelineEvent:
    order:int
    kind:str
    source:str
    command_index:int|None
    tool_call_index:int|None
    tool_call_id:str|None
    turn_index:int|None
    timestamp:str|None
    status:str|None
    text:str
    raw:Any

def _parse_ts(s):
    if not s: return None
    try: return datetime.fromisoformat(str(s).replace('Z','+00:00')).timestamp()
    except Exception: return None

def build_timeline(run):
    pending=[]
    tool_pos={}
    for t in run.toolCalls:
        ts=t.startedAt or t.endedAt
        txt=f"tool[{t.toolCallId}] {t.toolName or ''} status={t.status or ''} {t.error or t.resultSummary or ''}"
        pending.append({'kind':'tool_call','source':'tool_calls','command_index':None,'tool_call_index':t.index,'tool_call_id':t.toolCallId,'turn_index':t.turnIndex,'timestamp':ts,'status':t.status,'text':txt,'raw':t,'ts':_parse_ts(ts),'seq':len(pending)})
    for c in run.commands:
        txt=f"command[{c.index}] exit={c.exitCode}: {c.command or ''} :: {((c.stdout or '')+' '+(c.stderr or '')).strip()[:500]}"
        pending.append({'kind':'command','source':'commands','command_index':c.index,'tool_call_index':None,'tool_call_id':c.toolCallId,'turn_index':None,'timestamp':c.ts,'status':str(c.exitCode) if c.exitCode is not None else None,'text':txt,'raw':c,'ts':_parse_ts(c.ts),'seq':len(pending)})
    for p in run.patches:
        pending.append({'kind':'patch','source':'patches','command_index':None,'tool_call_index':None,'tool_call_id':None,'turn_index':None,'timestamp':None,'status':str(p.ok),'text':f'patch[{p.index}] {p.filePath} ok={p.ok}','raw':p,'ts':None,'seq':len(pending)})
    for m in run.modelResponses:
        pending.append({'kind':'model_response','source':'model_responses','command_index':None,'tool_call_index':None,'tool_call_id':None,'turn_index':None,'timestamp':None,'status':None,'text':(m.text or '')[:500],'raw':m,'ts':None,'seq':len(pending)})

    # Sort by timestamp when present. Without timestamps, keep artifact order, but place
    # commands with tool_call_id immediately after their linked tool call. turn_index is
    # only compared with other tool-call metadata, never with command indexes.
    def base_key(e):
        if e['ts'] is not None: return (0,e['ts'],e['seq'])
        if e['kind']=='tool_call': return (1, e['turn_index'] if e['turn_index'] is not None else 10**9, e['tool_call_index'] or 0, e['seq'])
        return (2, e['command_index'] if e['command_index'] is not None else e['seq'], e['seq'])
    ordered=sorted(pending,key=base_key)
    pos={e['tool_call_id']:i for i,e in enumerate(ordered) if e['kind']=='tool_call' and e['tool_call_id']}
    
    def linked_key(e):
        if e['kind']=='command' and e['tool_call_id'] in pos and e['ts'] is None:
            return (1, pos[e['tool_call_id']], 1, e['command_index'] if e['command_index'] is not None else e['seq'])
        k=base_key(e)
        return (k[0],) + tuple(k[1:])
    ordered=sorted(ordered,key=linked_key)
    return [TimelineEvent(i,e['kind'],e['source'],e['command_index'],e['tool_call_index'],e['tool_call_id'],e['turn_index'],e['timestamp'],e['status'],e['text'],e['raw']) for i,e in enumerate(ordered)]
