from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from .tools import OPS_TOOLS
@dataclass
class OpsToolContext:
    run_dir:Path; recorder:any; transcript:list[dict]; recent_events:list[dict]|None=None; runner_adapter:any=None; reviewer:any=None; backend:any=None; backend_name:str|None=None; timeout_seconds:int|None=None; max_parallel:int=1
class OpsToolResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    tool_use_id:str; tool_name:str; content:dict|str; is_error:bool=False

def _allowed(state, name, data):
    if name in {'ops_get_state','ops_inspect_repo','ops_submit_classification'}: return True, None
    if name not in state.allowed_next_actions(): return False, f'{name} is not allowed in phase {state.phase}; allowed={state.allowed_next_actions()}'
    if name=='ops_submit_decomposition' and not (state.plan or {}).get('should_decompose'): return False,'plan did not request decomposition'
    if name=='ops_validate_decomposition' and not state.decomposition: return False,'no decomposition exists'
    if name=='ops_select_execution_path':
        p=data.get('path')
        if p=='decomposed_subtasks' and not (state.decomposition_validated and state.decomposition_accepted and len(state.subtasks)>=2): return False,'decomposed_subtasks requires accepted validation and at least 2 subtasks'
        if p=='parallel_candidates' and not state.plan: return False,'parallel_candidates requires plan'
    if name=='ops_launch_candidates' and state.execution_path!='parallel_candidates': return False,'candidates require parallel_candidates execution path'
    if name=='ops_launch_subtasks' and state.execution_path!='decomposed_subtasks': return False,'subtasks require decomposed_subtasks execution path'
    if name=='ops_integrate_subtasks' and any(s.status=='running' for s in state.subtasks): return False,'subtasks still running'
    if name=='ops_select_winner':
        if data.get('decision')=='reject_all' and not data.get('reasons'):
            return False,'reject_all requires reasons'
    if name=='ops_finalize_run':
        running=any(c.status=='running' for c in state.candidates) or any(s.status=='running' for s in state.subtasks) or (state.integration or {}).get('status')=='running'
        if running: return False,'cannot finalize while work is running'
        if data.get('decision')=='accepted' and not state.selection:
            return False,'accepted finalization requires valid selection'
        if data.get('decision')=='rejected' and not (state.candidates or any(s.attempts for s in state.subtasks) or state.last_error or data.get('blockers')):
            return False,'rejected finalization requires attempted work or hard blocker'
        if data.get('decision')=='failed' and not (state.last_error or state.recovery_count>0 or data.get('blockers')):
            return False,'failed finalization requires fatal error or blocker'
    return True,None

def execute_tool_with_policy(state, tool_name:str, tool_input:dict, tool_use_id:str, context:OpsToolContext)->OpsToolResult:
    rec=context.recorder
    rec.record('tool_call_received',tool_name=tool_name,payload={'input':tool_input})
    spec=OPS_TOOLS.get(tool_name)
    if not spec:
        rec.record('tool_failed',tool_name=tool_name,payload={'error':'unknown tool'}); return OpsToolResult(tool_use_id=tool_use_id,tool_name=tool_name,content='unknown tool',is_error=True)
    try:
        parsed=spec.input_model.model_validate(tool_input)
        ok,err=_allowed(state,tool_name,tool_input)
        if not ok: raise ValueError(err)
        rec.record('tool_started',tool_name=tool_name)
        out=spec.handler(state,parsed,context)
        state.last_tool_name=tool_name; state.last_tool_input=tool_input; state.last_error=None
        state.save(Path(state.run_dir)/'state.json')
        rec.record('state_saved',tool_name=tool_name); rec.record('state_updated',tool_name=tool_name)
        event_map={'ops_submit_classification':'classification_submitted','ops_submit_investigation':'investigation_submitted','ops_submit_plan':'plan_submitted','ops_submit_decomposition':'decomposition_submitted','ops_validate_decomposition':'decomposition_validation_completed','ops_select_execution_path':'execution_path_selected','ops_integrate_subtasks':'integration_completed','ops_select_winner':'selection_completed','ops_finalize_run':'run_finalized'}
        if tool_name in event_map: rec.record(event_map[tool_name],tool_name=tool_name,payload=out if isinstance(out,dict) else {'result':out})
        rec.record('tool_finished',tool_name=tool_name,payload={'result':out})
        return OpsToolResult(tool_use_id=tool_use_id,tool_name=tool_name,content=out)
    except Exception as e:
        state.last_error=str(e); state.last_tool_name=tool_name; state.last_tool_input=tool_input
        state.save(Path(state.run_dir)/'state.json')
        rec.record('tool_failed',tool_name=tool_name,payload={'error':str(e)})
        return OpsToolResult(tool_use_id=tool_use_id,tool_name=tool_name,content=str(e),is_error=True)
