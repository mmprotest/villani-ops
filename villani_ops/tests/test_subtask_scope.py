from types import SimpleNamespace
from pathlib import Path

from villani_ops.core.task import Task
from villani_ops.orchestration.engine import _subtask_prompt, _subtask_review_prompt, OrchestrationEngine


def test_subtask_prompt_prevents_full_task_solving_and_lists_scope():
    decomp=SimpleNamespace(subtasks=[SimpleNamespace(id='fix_pricing_logic', title='Pricing', objective='Fix pricing'), SimpleNamespace(id='fix_inventory_atomicity', title='Inventory', objective='Fix inventory'), SimpleNamespace(id='fix_checkout_payment_failure', title='Checkout', objective='Fix checkout')])
    st={'id':'fix_pricing_logic','title':'Pricing','objective':'Fix pricing','relevant_files':['src/signalshop/pricing.py']}
    prompt=_subtask_prompt(Task(repo_path='.', objective='Fix shop', success_criteria='all pass'), decomp, st)
    assert 'Do not solve the full original task' in prompt
    assert 'src/signalshop/pricing.py' in prompt
    assert '- fix_inventory_atomicity' in prompt and '- fix_checkout_payment_failure' in prompt
    assert 'final integration stage will combine subtask patches' in prompt
    assert 'explain why in your final output' in prompt


def test_subtask_review_prompt_judges_current_subtask_only():
    st={'id':'fix_receipt_formatting','objective':'Fix receipt formatting','relevant_files':['src/signalshop/receipt.py']}
    prompt=_subtask_review_prompt(Task(repo_path='.', objective='Fix pricing inventory checkout receipt'), st, [st, {'id':'fix_inventory_atomicity'}])
    assert 'Evaluate this patch only against the current subtask objective' in prompt
    assert 'Do not fail this patch because unrelated sibling subtasks remain unfixed' in prompt
    assert 'overreached into unrelated subtasks' in prompt
    assert 'changed files are consistent with the subtask relevant files' in prompt


def test_scope_analysis_records_overreach_overlap_and_skips_high_risk():
    engine=OrchestrationEngine.__new__(OrchestrationEngine)
    subtasks=[{'id':'fix_pricing_logic','relevant_files':['src/signalshop/pricing.py']},{'id':'fix_checkout_payment_failure','relevant_files':['src/signalshop/checkout.py']}]
    accepted=[
        {'subtask_id':'fix_pricing_logic','changed_files':['src/signalshop/pricing.py','src/signalshop/checkout.py'],'review':{'scope_ok':True,'integration_risk':'low'}},
        {'subtask_id':'fix_checkout_payment_failure','changed_files':['src/signalshop/checkout.py'],'review':{'scope_ok':True,'integration_risk':'low'}},
    ]
    scope=engine._analyze_subtask_scope(accepted, subtasks)
    pricing=scope['subtasks'][0]
    assert pricing['unexpected_files'] == ['src/signalshop/checkout.py']
    assert pricing['overlaps_sibling_scope'] is True
    assert pricing['integration_risk'] == 'high'
    assert pricing['integration_decision'] == 'skip'
    assert scope['overlapping_files']['src/signalshop/checkout.py'] == ['fix_pricing_logic','fix_checkout_payment_failure']


def test_clean_scoped_patch_is_integrated_by_scope_analysis():
    engine=OrchestrationEngine.__new__(OrchestrationEngine)
    scope=engine._analyze_subtask_scope([{'subtask_id':'fix_pricing_logic','changed_files':['src/signalshop/pricing.py'],'review':{'scope_ok':True,'integration_risk':'low'}}], [{'id':'fix_pricing_logic','relevant_files':['src/signalshop/pricing.py']}])
    assert scope['subtasks'][0]['integration_decision'] == 'integrate'
    assert scope['subtasks'][0]['scope_overreach'] is False
