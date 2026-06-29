from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from .tools import OPS_TOOLS
from .state import detect_decomposition_deadlock
@dataclass
class OpsToolContext:
    run_dir:Path; recorder:any; transcript:list[dict]; recent_events:list[dict]|None=None; runner_adapter:any=None; reviewer:any=None; backend:any=None; backend_name:str|None=None; coding_backend:any=None; coding_backend_name:str|None=None; review_backend:any=None; review_backend_name:str|None=None; backends:any=None; usage_recorder:any=None; timeout_seconds:int|None=None; max_parallel:int=1; production:bool=True; allow_fake_dependencies:bool=False
class OpsToolResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    tool_use_id:str; tool_name:str; content:dict|str; is_error:bool=False

def _allowed(state, name, data):
    adaptive_context=getattr(state,'adaptive_context',{}) or {}
    if getattr(state, 'orchestrator', None) == 'adaptive':
        blocked={'ops_submit_decomposition','ops_validate_decomposition','ops_launch_candidates','ops_run_next_fallback_candidate_attempt','ops_run_next_subtask_attempt','ops_run_next_integration_repair_attempt','ops_start_candidate_fallback','ops_launch_subtasks','ops_integrate_subtasks'}
        if name in blocked:
            return False, f'{name} is not allowed in adaptive orchestrator; adaptive is constrained to execution_path=single_task'
        if name=='ops_select_execution_path' and data.get('path')!='single_task':
            return True, None

    if name in {'ops_get_state','ops_inspect_repo','ops_submit_classification','ops_derive_behavioral_oracle','ops_materialize_validation_probes'}: return True, None
    if name=='ops_observe_completed_attempt': return True, None
    if name=='ops_run_next_candidate_attempt' and state.execution_path=='single_task': return True, None
    if name=='ops_run_next_fallback_candidate_attempt' and state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock': return True, None
    if name=='ops_run_next_subtask_attempt' and state.execution_path=='decomposed_subtasks': return True, None
    if name=='ops_launch_candidates' and state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock' and not adaptive_context.get('legacy_ops_launch_candidates_enabled'):
        return False,'ops_launch_candidates is legacy/batch execution and is disabled during adaptive fallback. Use ops_run_next_fallback_candidate_attempt.'
    if name=='ops_launch_subtasks' and not adaptive_context.get('legacy_ops_launch_subtasks_enabled'):
        return False,'ops_launch_subtasks is a legacy/internal compatibility tool and is blocked in normal agentic decomposed orchestration; call ops_run_next_subtask_attempt for exactly one adaptive subtask attempt'
    legacy_subtasks = name=='ops_launch_subtasks' and adaptive_context.get('legacy_ops_launch_subtasks_enabled')
    legacy_fallback_candidates = name=='ops_launch_candidates' and state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock' and adaptive_context.get('legacy_ops_launch_candidates_enabled')
    if name not in state.allowed_next_actions() and name!='ops_finalize_run' and not legacy_subtasks and not legacy_fallback_candidates: return False, f'{name} is not allowed in phase {state.phase}; allowed={state.allowed_next_actions()}'
    if name=='ops_submit_decomposition' and not (state.plan or {}).get('should_decompose'): return False,'plan did not request decomposition'
    if name=='ops_validate_decomposition' and not state.decomposition: return False,'no decomposition exists'
    if name=='ops_select_execution_path':
        p=data.get('path')
        if p=='decomposed_subtasks' and not (state.decomposition_validated and state.decomposition_accepted and len(state.subtasks)>=2): return False,'decomposed_subtasks requires accepted validation and at least 2 subtasks'
        if p=='parallel_candidates' and not state.plan: return False,'parallel_candidates requires plan'
        if p=='parallel_candidates' and (state.plan or {}).get('strategy')=='single_task': return False,'plan strategy is single_task; use execution_path=single_task for sequential attempts, not parallel_candidates'
    if name=='ops_start_candidate_fallback':
        if state.decomposed_execution_status not in {'blocked','failed'} or not detect_decomposition_deadlock(state): return False,'fallback requires decomposition deadlock'
    if name=='ops_launch_candidates' and state.execution_path=='single_task': return False,'single_task execution uses adaptive sequential attempts; call ops_run_next_candidate_attempt'
    if name=='ops_launch_candidates' and state.execution_path!='parallel_candidates' and state.fallback_execution_path!='parallel_candidates_after_decomposition_deadlock': return False,'candidates require parallel_candidates execution path or fallback'
    if name=='ops_run_next_candidate_attempt' and state.execution_path!='single_task': return False,'ops_run_next_candidate_attempt requires execution_path=single_task'
    if name=='ops_run_next_fallback_candidate_attempt' and state.fallback_execution_path!='parallel_candidates_after_decomposition_deadlock': return False,'ops_run_next_fallback_candidate_attempt requires decomposition-deadlock fallback mode'
    if name=='ops_run_single_task_attempts': return False,'ops_run_single_task_attempts is a legacy compatibility tool and is not available in normal agentic flow; call ops_run_next_candidate_attempt'
    if name=='ops_launch_subtasks' and not (getattr(state,'adaptive_context',{}) or {}).get('legacy_ops_launch_subtasks_enabled'):
        return False,'ops_launch_subtasks is a legacy/internal compatibility tool and is blocked in normal agentic decomposed orchestration; call ops_run_next_subtask_attempt for exactly one adaptive subtask attempt'
    if name=='ops_launch_subtasks' and state.execution_path!='decomposed_subtasks': return False,'subtasks require decomposed_subtasks execution path'
    if name=='ops_run_next_subtask_attempt' and state.execution_path!='decomposed_subtasks': return False,'ops_run_next_subtask_attempt requires execution_path=decomposed_subtasks'
    if name=='ops_integrate_subtasks':
        if state.decomposed_execution_status in {'blocked','failed'}: return False,'cannot integrate blocked decomposed execution'
        if any(s.status=='running' for s in state.subtasks): return False,'subtasks still running'
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
        if data.get('decision')=='failed':
            accepted_pending=state.decomposition_validated and state.decomposition_accepted is True and state.execution_path=='unknown' and any(s.status=='pending' for s in state.subtasks)
            selected_pending=state.execution_path=='decomposed_subtasks' and state.decomposition_accepted is True and any(s.status=='pending' and not s.attempts for s in state.subtasks) and not any(a.status in {'scheduled','running'} for s in state.subtasks for a in s.attempts)
            fatal=bool(data.get('blockers') and any('fatal' in str(b) or 'deadlock' in str(b) or 'required_subtask_failed' in str(b) for b in data.get('blockers')))
            if (accepted_pending or selected_pending) and not fatal:
                return False,'failed finalization blocked while accepted decomposition has a deterministic next action'
            if not (state.last_error or state.recovery_count>0 or data.get('blockers')):
                return False,'failed finalization requires fatal error or blocker'
    return True,None

def execute_tool_with_policy(state, tool_name:str, tool_input:dict, tool_use_id:str, context:OpsToolContext)->OpsToolResult:
    rec=context.recorder
    rec.record('tool_call_received',tool_name=tool_name,payload={'input':tool_input})
    spec=OPS_TOOLS.get(tool_name)
    if not spec:
        state.recovery_count += 1; rec.record('tool_failed',tool_name=tool_name,payload={'error':'unknown tool'}); return OpsToolResult(tool_use_id=tool_use_id,tool_name=tool_name,content='unknown tool',is_error=True)
    try:
        parsed=spec.input_model.model_validate(tool_input)
        ok,err=_allowed(state,tool_name,tool_input)
        if not ok: raise ValueError(err)
        rec.record('tool_started',tool_name=tool_name)
        out=spec.handler(state,parsed,context)
        state.last_tool_name=tool_name; state.last_tool_input=tool_input; state.last_error=None
        state.save(Path(state.run_dir)/'state.json')
        rec.record('state_saved',tool_name=tool_name); rec.record('state_updated',tool_name=tool_name)
        event_map={'ops_submit_classification':'classification_submitted','ops_submit_investigation':'investigation_submitted','ops_submit_plan':'plan_submitted','ops_submit_decomposition':'decomposition_submitted','ops_validate_decomposition':'decomposition_validation_completed','ops_select_execution_path':'execution_path_selected'}
        if tool_name in event_map: rec.record(event_map[tool_name],tool_name=tool_name,payload=out if isinstance(out,dict) else {'result':out})
        if tool_name=='ops_integrate_subtasks': rec.record('integration_completed' if isinstance(out,dict) and out.get('status')=='completed' else 'integration_failed', tool_name=tool_name, payload=out if isinstance(out,dict) else {'result':out})
        rec.record('tool_finished',tool_name=tool_name,payload={'result':out})
        return OpsToolResult(tool_use_id=tool_use_id,tool_name=tool_name,content=out)
    except Exception as e:
        state.recovery_count += 1
        state.last_error=str(e); state.last_tool_name=tool_name; state.last_tool_input=tool_input
        state.save(Path(state.run_dir)/'state.json')
        rec.record('tool_failed',tool_name=tool_name,payload={'error':str(e)})
        return OpsToolResult(tool_use_id=tool_use_id,tool_name=tool_name,content=str(e),is_error=True)
