from villani_ops.controller.human_approval import TerminalHumanApprovalProvider, NonInteractiveHumanApprovalProvider, TestHumanApprovalProvider, HumanApprovalPromptContext
from villani_ops.core.acceptance import is_attempt_acceptance_eligible


def ctx(patch='p.diff', changed=None, reasons=None):
    return HumanApprovalPromptContext(run_id='r', attempt_id='a', backend_name='b', backend_model='m', runner_exit_code=1, review_decision='uncertain', review_score=.2, review_summary='s', review_evidence=['e'], review_issues=['i'], acceptance_blockers=['runner exit code is 1'], patch_path=patch, changed_files=['f.txt'] if changed is None else changed, cost_so_far=0.1, request_reasons=['reviewer_recommended_ask_human'] if reasons is None else reasons)


def test_noninteractive_provider_never_overrides():
    r=NonInteractiveHumanApprovalProvider().request_approval(ctx())
    assert r.decision=='skipped' and not r.prompted and r.skipped_reason=='non_interactive' and not r.valid_override


def test_test_provider_accept_sets_valid_override_with_evidence():
    r=TestHumanApprovalProvider('accept').request_approval(ctx())
    assert r.valid_override and r.shown_evidence['patch_path']=='p.diff'


def test_test_provider_accept_without_patch_not_valid():
    assert not TestHumanApprovalProvider('accept').request_approval(ctx(patch=None)).valid_override


def test_test_provider_reject_retry_escalate_fail_not_override():
    for d in ['reject','retry','escalate','fail']:
        assert not TestHumanApprovalProvider(d).request_approval(ctx()).valid_override


def test_acceptance_valid_human_override_requires_changed_files():
    a={'status':'human_approved','exit_code':1,'patch_path':'p.diff','changed_files':[],'human_approval':{'decision':'accept','valid_override':True},'review':{'passed':False,'decision':'uncertain','recommended_action':'ask_human'}}
    ok, blockers=is_attempt_acceptance_eligible(a)
    assert not ok and any('changed-file' in b for b in blockers)


def test_terminal_provider_reprompts_invalid_input(monkeypatch):
    vals=iter(['bad','accept','because'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(vals))
    r=TerminalHumanApprovalProvider().request_approval(ctx())
    assert r.decision=='accept' and r.valid_override
