from pydantic import BaseModel
from villani_ops.core.acceptance import is_attempt_acceptance_eligible
from .state import detect_decomposition_deadlock

class RecoveryRecommendation(BaseModel):
    action:str
    tool_name:str|None=None
    tool_input:dict|None=None
    reason:str
    can_execute_deterministically:bool=False
class RecoveryResult(BaseModel):
    should_fail:bool=False
    message:dict
    recommendation:RecoveryRecommendation|None=None

def _attempts(state):
    yield from state.candidates
    if state.integration: yield state.integration

def _aid(a): return a.get('attempt_id') if isinstance(a,dict) else a.attempt_id
def _validation(a): return a.get('validation') if isinstance(a,dict) else a.validation
def _review(a): return a.get('review') if isinstance(a,dict) else a.review
def _review_status(a): return a.get('review_status') if isinstance(a,dict) else getattr(a,'review_status',None)
def _review_retry_count(a): return a.get('review_retry_count',0) if isinstance(a,dict) else getattr(a,'review_retry_count',0)
def _patch(a): return a.get('patch_path') if isinstance(a,dict) else a.patch_path
def _changed(a): return a.get('changed_files') if isinstance(a,dict) else a.changed_files
def _failure(a): return a.get('failure_reason') if isinstance(a,dict) else a.failure_reason
def _status(a): return a.get('status') if isinstance(a,dict) else a.status

def _find_attempt_by_id(state, aid):
    for a in _attempts(state):
        if _aid(a)==aid: return a
    return None

def has_valid_selected_winner(state):
    sel=state.selection or {}
    aid=sel.get('selected_attempt_id')
    if sel.get('decision')!='select' or not aid or state.is_terminal(): return False
    a=_find_attempt_by_id(state, aid)
    if not a: return False
    try:
        return is_attempt_acceptance_eligible(a,state=state)[0]
    except Exception:
        return False

def build_evidence_based_acceptance_summary(state):
    aid=(state.selection or {}).get('selected_attempt_id')
    a=_find_attempt_by_id(state, aid) if aid else None
    changed=_changed(a) if a else []
    val=(_validation(a) or {}).get('status') if a else None
    return f"Selected {aid} is centrally acceptance eligible; changed {', '.join(changed or []) or 'no files'}; validation {val or 'not_run'}."

def _review_passed(a):
    r=(a.get('review') if isinstance(a,dict) else a.review) or {}
    return r.get('decision')=='pass' and r.get('recommended_action')=='accept' and not r.get('blockers')


def _ready_subtask_ids(state):
    by={s.subtask_id:s for s in state.subtasks}
    return [s.subtask_id for s in state.subtasks if s.status=='pending' and all(by[d].status=='accepted' for d in s.dependencies)]

def _has_active_subtask_attempts(state):
    return any(a.status in {'scheduled','running'} for s in state.subtasks for a in s.attempts)

def recommend_next_agentic_action(state):
    if has_valid_selected_winner(state):
        aid=(state.selection or {}).get('selected_attempt_id')
        a=_find_attempt_by_id(state, aid)
        return RecoveryRecommendation(action='finalize_selected_winner',tool_name='ops_finalize_run',tool_input={'decision':'accepted','summary':build_evidence_based_acceptance_summary(state),'selected_attempt_id':aid,'selected_patch_path':_patch(a),'blockers':[]},reason='A selected result is centrally acceptance eligible and should be finalized.',can_execute_deterministically=True)
    if (state.plan or {}).get('strategy')=='single_task' and state.execution_path=='unknown':
        return RecoveryRecommendation(action='select_single_task_execution_path',tool_name='ops_select_execution_path',tool_input={'path':'single_task','reason':'Planner selected single_task; run sequential attempts with validation/review after each attempt.'},reason='single_task plan has no selected execution path',can_execute_deterministically=True)
    if state.execution_path=='single_task' and not state.candidates:
        return RecoveryRecommendation(action='run_single_task_attempts',tool_name='ops_run_single_task_attempts',tool_input={'attempts':state.candidate_attempts,'reason':'Run sequential single-task attempts, stopping once an attempt passes validation and review.'},reason='single_task execution path selected but no attempts have launched',can_execute_deterministically=True)
    if state.decomposition_validated and state.decomposition_accepted is True and state.execution_path=='unknown' and state.subtasks:
        return RecoveryRecommendation(action='select_decomposed_execution_path',tool_name='ops_select_execution_path',tool_input={'path':'decomposed_subtasks','reason':'Decomposition has been validated and accepted.'},reason='Accepted decomposition has no selected execution path.',can_execute_deterministically=True)
    if state.execution_path=='decomposed_subtasks' and state.decomposition_accepted is True and state.subtasks and not _has_active_subtask_attempts(state) and all(s.status=='pending' and not s.attempts for s in state.subtasks):
        ready=_ready_subtask_ids(state)
        if ready:
            return RecoveryRecommendation(action='launch_decomposition_subtasks',tool_name='ops_launch_subtasks',tool_input={'subtask_ids':ready,'attempts_per_subtask':state.candidate_attempts,'reason':'Launch accepted decomposition subtasks.'},reason='Accepted decomposition execution path selected but no subtasks have launched.',can_execute_deterministically=True)
    dead=detect_decomposition_deadlock(state)
    if dead and state.fallback_execution_path!='parallel_candidates_after_decomposition_deadlock' and not state.candidates:
        return RecoveryRecommendation(action='start_candidate_fallback',tool_name='ops_start_candidate_fallback',tool_input={'reason':'required subtask failed and dependent subtasks are blocked'},reason='decomposition deadlock detected; full-task candidate fallback is available',can_execute_deterministically=True)
    if state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock' and not state.candidates:
        return RecoveryRecommendation(action='launch_fallback_candidates',tool_name='ops_launch_candidates',tool_input={'attempts':state.candidate_attempts,'reason':'fallback after decomposition deadlock'},reason='candidate fallback is started but no fallback candidates have launched',can_execute_deterministically=False)
    for a in _attempts(state):
        aid=_aid(a)
        if not aid: continue
        eligible, blockers=is_attempt_acceptance_eligible(a,state=state)
        if eligible:
            return RecoveryRecommendation(action='select_winner',tool_name='ops_select_winner',tool_input={'decision':'select','selected_attempt_id':aid,'summary':'Candidate passed review and validation and is centrally acceptance eligible.','reasons':['central acceptance gate passed'],'confidence':0.95},reason='eligible candidate/integration exists',can_execute_deterministically=True)
    for a in state.candidates:
        val=_validation(a) or {}
        if val.get('passed') is True and _patch(a) and _changed(a) and _review_status(a) in {'unavailable','malformed','provider_error'} and _review_retry_count(a) < 3:
            return RecoveryRecommendation(action='retry_review_infrastructure',tool_name='ops_review_attempt',tool_input={'attempt_id':_aid(a),'scope':'candidate'},reason='candidate passed validation and patch checks but structured review infrastructure failed; retry review with compact/minimal payload',can_execute_deterministically=True)
        if _status(a) in {'completed','reviewed'} and not _review(a) and not _failure(a) and _patch(a) and _changed(a):
            return RecoveryRecommendation(action='review_candidate',tool_name='ops_review_attempt',tool_input={'attempt_id':a.attempt_id,'scope':'candidate'},reason='completed candidate has patch evidence but no review',can_execute_deterministically=True)
    for a in state.candidates:
        if _review_passed(a) and not _validation(a):
            return RecoveryRecommendation(action='run_validation',tool_name='ops_run_validation',tool_input={'target':'candidate','target_id':a.attempt_id,'commands':[{'cmd':'python -m pytest --tb=short -v','purpose':'Validate reviewed candidate in its worktree','timeout_seconds':900}]},reason='reviewed candidate is missing validation',can_execute_deterministically=False)
        val=_validation(a) or {}
        if val.get('status')=='command_rejected':
            return RecoveryRecommendation(action='retry_validation',tool_name='ops_run_validation',tool_input={'target':'candidate','target_id':a.attempt_id,'commands':[{'cmd':'python -m pytest --tb=short -v','purpose':'cross-platform validation retry'}]},reason='validation command was rejected and should be retried safely',can_execute_deterministically=False)
    blockers=[]
    for a in _attempts(state):
        _, bs=is_attempt_acceptance_eligible(a,state=state); blockers.extend(bs)
    if blockers and (state.candidates or state.integration):
        return RecoveryRecommendation(action='finalize_failed' if detect_decomposition_deadlock(state) else 'reject_all',tool_name=('ops_finalize_run' if detect_decomposition_deadlock(state) else 'ops_select_winner'),tool_input=({'decision':'failed','summary':'Decomposed execution deadlocked and no centrally eligible fallback candidate is available.','blockers':['required_subtask_failed','decomposition_deadlocked','candidate_fallback_unavailable']} if detect_decomposition_deadlock(state) else {'decision':'reject_all','summary':'No centrally eligible result is available.','reasons':sorted(set(blockers)),'rejected_attempts':[_aid(a) for a in _attempts(state) if _aid(a)],'confidence':0.8}),reason='all attempted results are ineligible',can_execute_deterministically=True)
    return RecoveryRecommendation(action='ask_model',reason='no deterministic recovery action available')

def _recovery_prompt(content):
    return {'role':'user','content':"You returned no real tool call.\n\nDo not write XML-style tool calls such as <tool_call> or <function=...>. Use the provider's actual tool-calling interface.\n\n"+content+"\n\nCall exactly one available tool."}

def handle_no_tool_call(state, reason='no_tool_call', max_recovery_attempts:int=2):
    rec=recommend_next_agentic_action(state)
    if rec.tool_name:
        content=f"Call {rec.tool_name} with this input: {rec.tool_input}. Reason: {rec.reason}"
        if rec.tool_name=='ops_select_execution_path' and rec.tool_input and rec.tool_input.get('path')=='decomposed_subtasks':
            content='Call ops_select_execution_path with path="decomposed_subtasks".'
        if rec.tool_name=='ops_select_execution_path' and rec.tool_input and rec.tool_input.get('path')=='single_task':
            content='Call ops_select_execution_path with path="single_task".'
        if rec.tool_name=='ops_run_single_task_attempts':
            content='Call ops_run_single_task_attempts to run sequential single-task attempts. Do not call ops_launch_candidates.'
        if rec.tool_name=='ops_select_winner' and rec.tool_input and rec.tool_input.get('selected_attempt_id'):
            content=f"There is a reviewed and validated eligible candidate: {rec.tool_input['selected_attempt_id']}. Call ops_select_winner."
        if rec.tool_name=='ops_run_validation' and rec.tool_input:
            content=f"Validation is needed or was rejected. Call ops_run_validation with target=\"candidate\", target_id=\"{rec.tool_input.get('target_id')}\", command \"python -m pytest --tb=short -v\"."
        return RecoveryResult(message=_recovery_prompt(content), recommendation=rec)
    state.recovery_count += 1
    if state.recovery_count>max_recovery_attempts:
        return RecoveryResult(should_fail=True,message={'role':'user','content':'RECOVERY FAILED: agentic_orchestrator_no_progress'},recommendation=rec)
    return RecoveryResult(message=_recovery_prompt('The run is active but no valid progress occurred. Call ops_get_state if unsure. Do not respond in prose.'),recommendation=rec)
