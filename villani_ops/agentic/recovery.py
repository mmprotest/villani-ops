from pydantic import BaseModel
from villani_ops.core.acceptance import is_attempt_acceptance_eligible

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
def _review_passed(a):
    r=(a.get('review') if isinstance(a,dict) else a.review) or {}
    return r.get('decision')=='pass' and r.get('recommended_action')=='accept' and not r.get('blockers')

def recommend_next_agentic_action(state):
    sel=state.selection or {}
    if sel.get('decision')=='select':
        aid=sel.get('selected_attempt_id')
        return RecoveryRecommendation(action='finalize_accepted',tool_name='ops_finalize_run',tool_input={'decision':'accepted','summary':'Selected result is centrally eligible and ready to finalize.','selected_attempt_id':aid},reason='selection exists and accepted finalization is legal',can_execute_deterministically=True)
    for a in _attempts(state):
        aid=_aid(a)
        if not aid: continue
        eligible, blockers=is_attempt_acceptance_eligible(a,state=state)
        if eligible:
            return RecoveryRecommendation(action='select_winner',tool_name='ops_select_winner',tool_input={'decision':'select','selected_attempt_id':aid,'summary':'Candidate passed review and validation and is centrally acceptance eligible.','reasons':['central acceptance gate passed'],'confidence':0.95},reason='eligible candidate/integration exists',can_execute_deterministically=True)
    for a in state.candidates:
        if _review_passed(a) and not _validation(a):
            return RecoveryRecommendation(action='run_validation',tool_name='ops_run_validation',tool_input={'target':'candidate','target_id':a.attempt_id,'commands':[{'cmd':'python -m pytest --tb=short -v','purpose':'cross-platform validation'}]},reason='reviewed candidate is missing validation',can_execute_deterministically=False)
        val=_validation(a) or {}
        if val.get('status')=='command_rejected':
            return RecoveryRecommendation(action='retry_validation',tool_name='ops_run_validation',tool_input={'target':'candidate','target_id':a.attempt_id,'commands':[{'cmd':'python -m pytest --tb=short -v','purpose':'cross-platform validation retry'}]},reason='validation command was rejected and should be retried safely',can_execute_deterministically=False)
    blockers=[]
    for a in _attempts(state):
        _, bs=is_attempt_acceptance_eligible(a,state=state); blockers.extend(bs)
    if blockers and (state.candidates or state.integration):
        return RecoveryRecommendation(action='reject_all',tool_name='ops_select_winner',tool_input={'decision':'reject_all','summary':'No centrally eligible result is available.','reasons':sorted(set(blockers)),'rejected_attempts':[_aid(a) for a in _attempts(state) if _aid(a)],'confidence':0.8},reason='all attempted results are ineligible',can_execute_deterministically=True)
    return RecoveryRecommendation(action='ask_model',reason='no deterministic recovery action available')

def handle_no_tool_call(state, reason='no_tool_call', max_recovery_attempts:int=2):
    state.recovery_count += 1
    rec=recommend_next_agentic_action(state)
    if rec.tool_name:
        content=f"RECOVERY MODE:\nCall {rec.tool_name} with this input: {rec.tool_input}. Reason: {rec.reason}"
        if rec.tool_name=='ops_select_winner' and rec.tool_input and rec.tool_input.get('selected_attempt_id'):
            content=f"There is a reviewed and validated eligible candidate: {rec.tool_input['selected_attempt_id']}. Call ops_select_winner."
        if rec.tool_name=='ops_run_validation' and rec.tool_input:
            content=f"Validation is needed or was rejected. Call ops_run_validation with target=\"candidate\", target_id=\"{rec.tool_input.get('target_id')}\", command \"python -m pytest --tb=short -v\"."
        return RecoveryResult(message={'role':'user','content':content}, recommendation=rec)
    if state.recovery_count>max_recovery_attempts:
        return RecoveryResult(should_fail=True,message={'role':'user','content':'RECOVERY FAILED: agentic_orchestrator_no_progress'},recommendation=rec)
    return RecoveryResult(message={'role':'user','content':'RECOVERY MODE:\nThe run is active but no valid progress occurred. You must call exactly one valid tool. Call ops_get_state if unsure. Do not respond in prose.'},recommendation=rec)
