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
    for st in getattr(state,'subtasks',[]) or []:
        yield from st.attempts
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


def _complete(a): return _status(a) in {'completed','failed','reviewed','rejected','accepted'}
def _validation_missing(a): return _complete(a) and bool(_patch(a)) and bool(_changed(a)) and not _validation(a)
def _review_missing(a): return _complete(a) and bool(_patch(a)) and bool(_changed(a)) and not _review(a) and not _failure(a)
def _obs_for(state, aid): return next((o for o in getattr(state,'attempt_observations',[]) or [] if getattr(o,'attempt_id',None)==aid), None)
def _obs_fresh(a, o):
    if not o: return False
    val=f"{getattr(a,'validation_status',None)}:{len(getattr(a,'validation_results',[]) or [])}"
    rev=f"{getattr(a,'review_status',None)}:{getattr(a,'review_retry_count',0)}:{bool(getattr(a,'review',None))}"
    return getattr(o,'validation_snapshot_id',None)==val and getattr(o,'review_snapshot_id',None)==rev

def _exhausted_failure_summary(state, observations):
    latest=observations[-1] if observations else None
    latest_attempt=_find_attempt_by_id(state, latest.attempt_id) if latest else (state.candidates[-1] if state.candidates else None)
    validation=[]; review=[]; patch=[]
    for o in observations:
        if o.validation_status not in {None,'passed','not_run'}: validation += list(o.evidence or []) or [o.validation_status]
        if o.review_status not in {None,'passed','not_run'}: review += list(o.blockers or [])
        patch += [b for b in (o.blockers or []) if 'patch' in b or 'scope' in b or 'changed_files' in b]
    backend={k:v.get('capability_signal') for k,v in (getattr(state,'backend_assessments',{}) or {}).items() if isinstance(v,dict)}
    manual='Inspect the latest attempt worktree/patch, address listed validation or review blockers, then rerun a focused manual validation command.'
    return {
        'attempt_count':len(state.candidates),
        'latest_outcome':getattr(latest,'outcome',None),
        'changed_files':sorted({f for o in observations for f in (o.changed_files or [])} or set(_changed(latest_attempt) or [])),
        'validation_failure_summary':validation[:6],
        'review_blocker_summary':review[:6],
        'patch_scope_blockers':sorted(set(patch))[:6],
        'backend_runner_capability_signal':backend or getattr(state,'runner_assessment',{}),
        'recommended_next_manual_action':manual,
        'attempt_observations':[o.model_dump() for o in observations],
    }

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

def _subtask_commit_ready(st):
    for a in reversed(getattr(st,'attempts',[]) or []):
        val=(_validation(a) or {}).get('decision') or {}
        if (val.get('status')=='passed' or val.get('status')=='inconclusive') and _review_status(a)=='passed':
            return a
    return None

def recommend_next_agentic_action(state):
    if has_valid_selected_winner(state):
        aid=(state.selection or {}).get('selected_attempt_id')
        a=_find_attempt_by_id(state, aid)
        return RecoveryRecommendation(action='finalize_selected_winner',tool_name='ops_finalize_run',tool_input={'decision':'accepted','summary':build_evidence_based_acceptance_summary(state),'selected_attempt_id':aid,'selected_patch_path':_patch(a),'blockers':[]},reason='A selected result is centrally acceptance eligible and should be finalized.',can_execute_deterministically=True)
    if (state.plan or {}).get('strategy')=='single_task' and state.execution_path=='unknown':
        return RecoveryRecommendation(action='select_single_task_execution_path',tool_name='ops_select_execution_path',tool_input={'path':'single_task','reason':'Planner selected single_task; run sequential attempts with validation/review after each attempt.'},reason='single_task plan has no selected execution path',can_execute_deterministically=True)
    if state.execution_path=='single_task' and not state.candidates:
        return RecoveryRecommendation(action='run_next_candidate_attempt',tool_name='ops_run_next_candidate_attempt',tool_input={'reason':'Run the first adaptive single-task candidate attempt.'},reason='single_task execution path selected but no attempts have launched',can_execute_deterministically=True)
    if state.decomposition_validated and state.decomposition_accepted is True and state.execution_path=='unknown' and state.subtasks:
        return RecoveryRecommendation(action='select_decomposed_execution_path',tool_name='ops_select_execution_path',tool_input={'path':'decomposed_subtasks','reason':'Decomposition has been validated and accepted.'},reason='Accepted decomposition has no selected execution path.',can_execute_deterministically=True)
    if state.execution_path=='decomposed_subtasks' and state.decomposition_accepted is True and state.subtasks and not _has_active_subtask_attempts(state) and all(s.status=='pending' and not s.attempts for s in state.subtasks):
        ready=_ready_subtask_ids(state)
        if ready:
            return RecoveryRecommendation(action='run_next_subtask_attempt',tool_name='ops_run_next_subtask_attempt',tool_input={'subtask_id':ready[0],'reason':'Run the first adaptive subtask attempt.'},reason='Accepted decomposition execution path selected; run one ready subtask attempt.',can_execute_deterministically=True)
    dead=detect_decomposition_deadlock(state)
    if dead and state.fallback_execution_path!='parallel_candidates_after_decomposition_deadlock' and not state.candidates:
        return RecoveryRecommendation(action='start_candidate_fallback',tool_name='ops_start_candidate_fallback',tool_input={'reason':'required subtask failed and dependent subtasks are blocked'},reason='decomposition deadlock detected; full-task candidate fallback is available',can_execute_deterministically=True)
    if state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock' and not state.candidates:
        return RecoveryRecommendation(action='run_next_fallback_candidate_attempt',tool_name='ops_run_next_fallback_candidate_attempt',tool_input={'reason':'Run the first adaptive fallback candidate after decomposition deadlock with decomposition learnings.'},reason='fallback mode is active; run exactly one adaptive fallback candidate',can_execute_deterministically=True)
    if state.execution_path=='decomposed_subtasks' and state.integration and (state.integration.get('validation') or {}).get('passed') is False:
        return RecoveryRecommendation(action='run_next_integration_repair_attempt',tool_name='ops_run_next_integration_repair_attempt',tool_input={'reason':'Full integration validation failed; run one adaptive integration repair with accepted subtask context.'},reason='integration validation failed after accepted subtasks',can_execute_deterministically=True)
    if state.execution_path=='decomposed_subtasks':
        for st in state.subtasks:
            if st.status!='accepted' and _subtask_commit_ready(st) is not None:
                a=_subtask_commit_ready(st)
                return RecoveryRecommendation(action='commit_ready_subtask_acceptance',tool_name='ops_run_next_subtask_attempt',tool_input={'subtask_id':st.subtask_id,'base_attempt_id':_aid(a),'reason':'Commit review-accepted focused-passing subtask before considering retries.'},reason='subtask has passed authoritative/inconclusive validation with accepted review; commit acceptance/apply patch instead of retrying',can_execute_deterministically=True)
        for st in state.subtasks:
            if st.status=='pending' and st.attempts and len(st.attempts) < max(1,int(state.candidate_attempts or 1)):
                obs=[o for o in state.attempt_observations if o.scope=='subtask' and o.subtask_id==st.subtask_id]
                last=obs[-1] if obs else None
                if last and last.outcome!='accepted':
                    return RecoveryRecommendation(action=f'focused_subtask_retry_{last.outcome}',tool_name='ops_run_next_subtask_attempt',tool_input={'subtask_id':st.subtask_id,'base_attempt_id':last.attempt_id,'repair':bool(last.should_repair),'reason':f'Retry subtask adaptively using prior {last.outcome} observation feedback.'},reason='failed subtask has budget remaining; retry with curated observation feedback',can_execute_deterministically=True)
        ready=_ready_subtask_ids(state)
        if ready:
            return RecoveryRecommendation(action='run_next_ready_subtask_attempt',tool_name='ops_run_next_subtask_attempt',tool_input={'subtask_id':ready[0],'reason':'Run one ready adaptive subtask attempt.'},reason='ready subtask exists',can_execute_deterministically=True)
    if state.execution_path=='single_task':
        # Evidence-first recovery: never retry until current completed attempts have
        # validation/review and a fresh idempotent observation when those are possible.
        for a in state.candidates:
            if _validation_missing(a):
                return RecoveryRecommendation(action='run_validation',tool_name='ops_run_validation',tool_input={'target':'candidate','target_id':_aid(a),'commands':[{'cmd':'python -m pytest --tb=short -v','purpose':'Validate completed candidate before observation/retry','timeout_seconds':900}]},reason='completed candidate needs validation before observation or retry',can_execute_deterministically=False)
        for a in state.candidates:
            if _review_missing(a):
                return RecoveryRecommendation(action='review_candidate',tool_name='ops_review_attempt',tool_input={'attempt_id':_aid(a),'scope':'candidate'},reason='completed candidate needs review before observation or retry',can_execute_deterministically=True)
        for a in state.candidates:
            if _complete(a) and not _obs_fresh(a, _obs_for(state,_aid(a))):
                return RecoveryRecommendation(action='create_or_refresh_observation',tool_name='ops_observe_completed_attempt',tool_input={'attempt_id':_aid(a),'reason':'Create or refresh AttemptObservation from current validation/review evidence before deciding whether to retry.'},reason='completed attempt lacks a current observation',can_execute_deterministically=True)
    for a in _attempts(state):
        if (a.get('scope') if isinstance(a,dict) else getattr(a,'scope',None))=='subtask':
            continue
        aid=_aid(a)
        if not aid: continue
        eligible, blockers=is_attempt_acceptance_eligible(a,state=state)
        if eligible:
            return RecoveryRecommendation(action='select_winner',tool_name='ops_select_winner',tool_input={'decision':'select','selected_attempt_id':aid,'summary':'Candidate passed review and validation and is centrally acceptance eligible.','reasons':['central acceptance gate passed'],'confidence':0.95},reason='eligible candidate/integration exists',can_execute_deterministically=True)
    if state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock' and state.candidates:
        budget=max(1,int(state.candidate_attempts or 1))
        for a in state.candidates:
            if _validation_missing(a):
                return RecoveryRecommendation(action='run_fallback_validation',tool_name='ops_run_validation',tool_input={'target':'candidate','target_id':_aid(a),'commands':[{'cmd':'python -m pytest --tb=short -v','purpose':'Validate fallback candidate before observation/retry','timeout_seconds':900}]},reason='completed fallback candidate needs validation before observation or retry',can_execute_deterministically=False)
        for a in state.candidates:
            if _review_missing(a):
                return RecoveryRecommendation(action='review_fallback_candidate',tool_name='ops_review_attempt',tool_input={'attempt_id':_aid(a),'scope':'candidate'},reason='completed fallback candidate needs review before observation or retry',can_execute_deterministically=True)
        for a in state.candidates:
            if _complete(a) and not _obs_fresh(a, _obs_for(state,_aid(a))):
                return RecoveryRecommendation(action='observe_fallback_candidate',tool_name='ops_observe_completed_attempt',tool_input={'attempt_id':_aid(a),'reason':'Create or refresh fallback AttemptObservation from current validation/review evidence.'},reason='fallback candidate lacks a current observation',can_execute_deterministically=True)
        observations=[o for o in getattr(state,'attempt_observations',[]) or [] if getattr(o,'scope',None)=='candidate']
        last=observations[-1] if observations else None
        if last and last.outcome=='accepted':
            return RecoveryRecommendation(action='select_winner',tool_name='ops_select_winner',tool_input={'decision':'select','selected_attempt_id':last.attempt_id,'summary':'Observed accepted fallback candidate is eligible for selection.','reasons':['fallback attempt observation accepted'],'confidence':0.95},reason='latest fallback observation is accepted',can_execute_deterministically=True)
        if last and len(state.candidates) < budget:
            return RecoveryRecommendation(action=f'focused_fallback_retry_{last.outcome}',tool_name='ops_run_next_fallback_candidate_attempt',tool_input={'reason':'Retry fallback adaptively using previous fallback attempt failure feedback: changed files, validation failures, review blockers, patch/scope/hygiene blockers, and commands to rerun.','base_attempt_id':last.attempt_id,'repair':bool(last.should_repair or last.outcome in {'validation_failed','review_failed','patch_failed','scope_failed'})},reason=f'latest fallback observation outcome={last.outcome}; retry one fallback candidate with curated feedback',can_execute_deterministically=True)
        if len(state.candidates) >= budget:
            structured=_exhausted_failure_summary(state, observations)
            return RecoveryRecommendation(action='finalize_failed',tool_name='ops_finalize_run',tool_input={'decision':'failed','summary':'Decomposed execution deadlocked and fallback candidate budget is exhausted.','blockers':['decomposition_deadlocked','fallback_candidate_budget_exhausted'],'failure_observations':structured},reason='fallback budget exhausted; fail with observations and blockers',can_execute_deterministically=True)

    if state.execution_path=='single_task' and state.candidates:
        budget=max(1,int(state.candidate_attempts or 1))
        observations=[o for o in getattr(state,'attempt_observations',[]) or [] if getattr(o,'scope',None)=='candidate']
        last=observations[-1] if observations else None
        if last and last.outcome=='accepted':
            return RecoveryRecommendation(action='select_winner',tool_name='ops_select_winner',tool_input={'decision':'select','selected_attempt_id':last.attempt_id,'summary':'Observed accepted candidate is eligible for selection.','reasons':['attempt observation accepted'],'confidence':0.95},reason='latest observation is accepted',can_execute_deterministically=True)
        if last and len(state.candidates) < budget:
            reason_map={
                'validation_failed':'Focused retry: fix failing validation using prior AttemptObservation evidence and rerun known failing commands.',
                'review_failed':'Focused retry: address review blockers from prior AttemptObservation and avoid repeating rejected strategy.',
                'partial_progress':'Focused retry/repair: build on changed files and address remaining blockers narrowly.',
                'no_patch':'Focused retry: inspect and edit relevant repository files; do not finish without a product-code patch.',
                'runner_failed':'Retry after runner failure only if safe; inspect repo and produce a concrete patch.',
                'infra_failed':'Retry infrastructure failure once if safe; otherwise escalate backend or fail clearly.',
                'patch_failed':'Focused retry: produce a clean git-applicable patch and do not repeat patch hygiene mistakes.',
                'scope_failed':'Focused retry: stay in scope and avoid unrelated files.',
                'unknown':'Focused retry using previous attempt evidence.'}
            backend_names=list((getattr(state,'backend_assessments',{}) or {}).keys())
            other=next((b for b in backend_names if b and b!=(last.backend_name or 'unknown')), None)
            inp={'reason':reason_map.get(last.outcome, reason_map['unknown']), 'base_attempt_id':last.attempt_id, 'repair':bool(last.should_repair or last.outcome in {'validation_failed','review_failed','patch_failed','scope_failed'})}
            if last.should_escalate_backend and other:
                inp['backend_name']=other
                return RecoveryRecommendation(action='escalate_backend_retry',tool_name='ops_run_next_candidate_attempt',tool_input=inp,reason='observation recommends backend escalation and an alternate backend is available',can_execute_deterministically=True)
            if last.should_decompose:
                state.adaptive_context['decomposition_warranted']=True
            return RecoveryRecommendation(action=f'focused_retry_{last.outcome}',tool_name='ops_run_next_candidate_attempt',tool_input=inp,reason=f'latest observation outcome={last.outcome}; run one focused adaptive retry with feedback',can_execute_deterministically=True)
        if len(state.candidates) >= budget:
            structured=_exhausted_failure_summary(state, observations)
            reasons=sorted(set(sum([list(o.blockers or [])+list(o.evidence or [])+list(o.next_attempt_directives or []) for o in observations], [])))[:12]
            return RecoveryRecommendation(action='reject_all',tool_name='ops_select_winner',tool_input={'decision':'reject_all','summary':'Candidate attempt budget exhausted. No accepted adaptive attempt is available.','reasons':reasons or ['candidate_attempt_budget_exhausted'],'rejected_attempts':[c.attempt_id for c in state.candidates],'confidence':0.8,'failure_observations':structured},reason='budget exhausted; report observations and blockers',can_execute_deterministically=True)
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
        if rec.tool_name=='ops_run_next_candidate_attempt':
            content='Call ops_run_next_candidate_attempt to run exactly one adaptive candidate attempt. Do not call bulk attempt tools.'
        if rec.tool_name=='ops_run_next_fallback_candidate_attempt':
            content='Call ops_run_next_fallback_candidate_attempt to run exactly one adaptive fallback candidate attempt. Do not call bulk launch tools.'
        if rec.tool_name=='ops_select_winner' and rec.tool_input and rec.tool_input.get('selected_attempt_id'):
            content=f"There is a reviewed and validated eligible candidate: {rec.tool_input['selected_attempt_id']}. Call ops_select_winner."
        if rec.tool_name=='ops_run_validation' and rec.tool_input:
            content=f"Validation is needed or was rejected. Call ops_run_validation with target=\"candidate\", target_id=\"{rec.tool_input.get('target_id')}\", command \"python -m pytest --tb=short -v\"."
        return RecoveryResult(message=_recovery_prompt(content), recommendation=rec)
    state.recovery_count += 1
    if state.recovery_count>max_recovery_attempts:
        return RecoveryResult(should_fail=True,message={'role':'user','content':'RECOVERY FAILED: agentic_orchestrator_no_progress'},recommendation=rec)
    return RecoveryResult(message=_recovery_prompt('The run is active but no valid progress occurred. Call ops_get_state if unsure. Do not respond in prose.'),recommendation=rec)
