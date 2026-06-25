from villani_ops.controller.state_machine import *
from villani_ops.policy_engine.engine import ExecutionStrategy
from villani_ops.review.reviewer import ReviewResult


def strat(): return ExecutionStrategy(profile='balanced', attempts=[])
def review(decision='pass', passed=True, rec='accept'):
    return ReviewResult(decision=decision, passed=passed, recommended_action=rec)
def attempt(exit_code=0, status='validated', r=None):
    return {'attempt_id':'attempt_001','exit_code':exit_code,'status':status,'patch_path':__file__,'changed_files':['hello.txt'],'review':(r or review()).model_dump(mode='json')}
def ctx(**kw):
    data=dict(run_id='r', attempt=attempt(), review=review(), strategy=strat(), attempts_remaining_for_backend=0, escalation_available=False, non_interactive=True)
    data.update(kw); return ControllerDecisionContext(**data)

def test_accepts_pass_eligible():
    d=decide_next_action(ctx()); assert d.action==ControllerAction.accept and d.acceptance_eligible

def test_blocks_pass_review_when_runner_nonzero():
    r=review(); d=decide_next_action(ctx(attempt=attempt(1,'failed',r), review=r)); assert d.action==ControllerAction.fail; assert d.acceptance_blockers

def test_retry_same_backend_when_recommended_and_attempts_remain():
    r=review('fail',False,'retry_same_backend'); d=decide_next_action(ctx(attempt=attempt(0,'candidate',r), review=r, attempts_remaining_for_backend=1)); assert d.action==ControllerAction.retry_same_backend

def test_escalates_when_recommended_and_backend_remains():
    r=review('fail',False,'escalate'); d=decide_next_action(ctx(attempt=attempt(0,'candidate',r), review=r, escalation_available=True)); assert d.action==ControllerAction.escalate

def test_asks_human_for_uncertain_interactive_review():
    r=review('uncertain',False,'ask_human'); r.requires_human_approval=True
    d=decide_next_action(ctx(attempt=attempt(0,'candidate',r), review=r, non_interactive=False)); assert d.action==ControllerAction.ask_human

def test_skips_human_noninteractive_and_retries_escalates_fails_safely():
    r=review('uncertain',False,'ask_human'); r.requires_human_approval=True
    assert decide_next_action(ctx(attempt=attempt(0,'candidate',r), review=r, attempts_remaining_for_backend=1)).action==ControllerAction.retry_same_backend
    assert decide_next_action(ctx(attempt=attempt(0,'candidate',r), review=r, escalation_available=True)).action==ControllerAction.escalate
    assert decide_next_action(ctx(attempt=attempt(0,'candidate',r), review=r)).action==ControllerAction.fail

def test_human_accept_reject_retry_escalate_fail():
    r=review('fail',False,'fail'); a=attempt(1,'failed',r)
    assert decide_next_action(ctx(attempt=a, review=r, human_approval=HumanApprovalResult(requested=True, decision='accept'), human_override_allowed=True)).action==ControllerAction.accept
    assert decide_next_action(ctx(attempt=a, review=r, human_approval=HumanApprovalResult(requested=True, decision='reject'), attempts_remaining_for_backend=1)).action==ControllerAction.retry_same_backend
    assert decide_next_action(ctx(attempt=a, review=r, human_approval=HumanApprovalResult(requested=True, decision='retry'), attempts_remaining_for_backend=1)).action==ControllerAction.retry_same_backend
    assert decide_next_action(ctx(attempt=a, review=r, human_approval=HumanApprovalResult(requested=True, decision='escalate'), escalation_available=True)).action==ControllerAction.escalate
    assert decide_next_action(ctx(attempt=a, review=r, human_approval=HumanApprovalResult(requested=True, decision='fail'))).action==ControllerAction.fail
