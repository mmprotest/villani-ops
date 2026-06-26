import json, subprocess
from pathlib import Path
from typer.testing import CliRunner

from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.core.decision import Decision
from villani_ops.core.task import Task
from villani_ops.execution_policies.base import prior_forces_escalation
from villani_ops.execution_policies.cheap import CheapExecutionPolicy
from villani_ops.execution_policies.balanced import BalancedExecutionPolicy
from villani_ops.execution_policies.quality import QualityExecutionPolicy
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.nodes import NodeResult, OrchestrationNode
from villani_ops.performance.models import SelectionResult
from villani_ops.performance.report import write_performance_report
from villani_ops.review.reviewer import ReviewResult
from villani_ops.tests.test_performance_orchestration import git_repo, make_ops

runner = CliRunner()


def backends():
    return {
        'small': Backend(name='small', provider='openai-compatible', model='s', capability_score=1, roles=['coding','review','selection','classification','investigation']),
        'large': Backend(name='large', provider='openai-compatible', model='l', capability_score=10, roles=['coding','review','selection','classification','investigation']),
    }


def test_prior_escalation_uses_explicit_flags_only():
    assert prior_forces_escalation([NodeResult(has_failure=True)])
    assert prior_forces_escalation([NodeResult(has_review_blocker=True)])
    assert prior_forces_escalation([NodeResult(has_acceptance_blocker=True)])
    assert not prior_forces_escalation([NodeResult(status='succeeded', data={'note':'this unrelated text says blocker but is not a blocker flag'})])


def test_policy_escalation_scoped_prior_results():
    node=OrchestrationNode(id='select', kind='select', objective='select', difficulty='easy', risk='low', confidence=.95)
    ctx=TaskContext(objective='x', confidence=.95)
    assert CheapExecutionPolicy().select_backend(node=node, backends=backends(), task_context=ctx, prior_results=[NodeResult(has_review_blocker=True)]).backend_name == 'large'
    verify=OrchestrationNode(id='verify', kind='verify', objective='verify', difficulty='easy', risk='low', confidence=.95)
    assert BalancedExecutionPolicy().select_backend(node=verify, backends=backends(), task_context=ctx, prior_results=[NodeResult(has_acceptance_blocker=True)]).backend_name == 'large'
    safe=OrchestrationNode(id='investigate', kind='investigate', objective='investigate', difficulty='easy', risk='low', confidence=.95)
    assert CheapExecutionPolicy().select_backend(node=safe, backends=backends(), task_context=ctx, prior_results=[]).backend_name == 'small'


def test_cli_non_performance_backend_output(monkeypatch, tmp_path):
    class Result:
        def __init__(self, mode):
            self.run_dir=str(tmp_path/'run')
            self.decision=Decision(run_id='r', mode=mode, runner='villani-code', accepted=False, reason='done', candidate_attempts_requested=1, candidate_attempts_completed=0, orchestration_graph_path='orchestration_graph.json', node_backend_assignments={'investigate':'small'})
    def fake_run(self, **kwargs): return Result(kwargs['mode'])
    monkeypatch.setattr('villani_ops.controller.executor.VillaniOps.run', fake_run)
    for mode in ['cheap','balanced','quality']:
        res=runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--mode',mode,'--orchestrator','graph'])
        assert res.exit_code == 0, res.output
        assert f'Mode: {mode}' in res.output
        assert 'Primary backend: None/None' not in res.output
        assert 'Backend assignments' in res.output
        assert 'performance_orchestration' not in res.output
    def perf_run(self, **kwargs):
        r=Result('performance'); r.decision.performance_backend_name='large'; r.decision.performance_backend_model='l'; return r
    monkeypatch.setattr('villani_ops.controller.executor.VillaniOps.run', perf_run)
    res=runner.invoke(app, ['run','--repo',str(tmp_path),'--task','x','--mode','performance','--orchestrator','graph'])
    assert 'Performance backend: large/l' in res.output


def test_report_selection_uses_evidence_and_missing_fallback(tmp_path):
    sel=SelectionResult(decision='select', selected_attempt_id='attempt_002', reasons=['better test evidence'], rejected_attempts=['attempt_001'], confidence=.91, selector_backend='large')
    decision=Decision(run_id='r', accepted=True, winning_attempt_id='attempt_002', plan={})
    cands=[{'attempt_id':'attempt_001','backend_name':'small','model':'s','status':'failed','exit_code':1,'changed_files':[],'review_decision':'fail','review_score':.1,'review_issues':['missing test evidence'],'acceptance_eligible':False,'acceptance_blockers':['patch is missing']}, {'attempt_id':'attempt_002','backend_name':'large','model':'l','status':'validated','exit_code':0,'changed_files':['src/auth.py','tests/test_auth.py'],'review_decision':'pass','review_recommended_action':'accept','review_score':.92,'review_summary':'tests pass','review_issues':[],'review_evidence':['pytest passed'],'acceptance_eligible':True,'acceptance_blockers':[]}]
    (tmp_path/'controller_steps.jsonl').write_text('{}\n')
    report=write_performance_report(tmp_path, Task(repo_path=str(tmp_path), objective='fix'), None, cands, sel, decision, 1.0).read_text()
    assert 'Selected attempt: attempt_002' in report
    assert 'better test evidence' in report and 'pytest passed' in report
    assert 'patch is missing' in report
    assert 'selector considered correctness' not in report
    missing=write_performance_report(tmp_path/'m', Task(repo_path=str(tmp_path), objective='fix'), None, [], SelectionResult(decision='reject_all', reasons=['none eligible']), Decision(run_id='r2'), 1.0)
    assert 'Detailed winner evidence was not available in artifacts' in missing.read_text()


def test_controller_steps_populated_and_performance_avoids_estimate_cost(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; git_repo(repo)
    class NoEstimateBackend(Backend):
        def estimate_cost(self, *a, **k): raise AssertionError('estimate_cost called')
    ops, _=make_ops(tmp_path, monkeypatch, [ReviewResult(passed=True,decision='pass',recommended_action='accept',score=.9)])
    ops.storage.save_backends({'code': NoEstimateBackend(name='code',provider='openai-compatible',base_url='http://x/v1',model='m',api_key='dummy',capability_score=1,roles=['coding','classification','review','investigation','selection'])})
    res=ops.run(repo, Task(repo_path=str(repo), objective='edit'), candidate_attempts=1, non_interactive=True, mode='performance')
    rd=Path(res.run_dir)
    steps=(rd/'controller_steps.jsonl').read_text().splitlines()
    decision=json.loads((rd/'decision.json').read_text())
    assert steps and decision['controller_steps'] and decision['controller_steps_path']=='controller_steps.jsonl'
    actions=[json.loads(s)['action'] for s in steps]
    assert 'backend_assigned' in actions and 'node_started' in actions and any(a in actions for a in ['node_succeeded','node_failed','node_skipped'])
    assert any(a.startswith('final_decision_') for a in actions)
    for n in json.loads((rd/'orchestration_graph.json').read_text())['nodes']:
        if n['status'] in {'succeeded','failed','skipped'}:
            nd=rd/'nodes'/n['id']
            assert (nd/'node.json').exists() and (nd/'input.json').exists() and (nd/'output.json').exists()
    assert (rd/'nodes'/'select'/'output.json').exists()
    assert Decision.model_validate({'run_id':'legacy','accepted':False})


def test_report_component_specific_fallback_labels(tmp_path):
    from villani_ops.core.decision import Decision
    from villani_ops.performance.models import InvestigationResult, SelectionResult
    from villani_ops.core.task import Task
    from villani_ops.performance.report import write_performance_report
    inv=InvestigationResult(summary='investigate', investigation_fallback_used=True, investigation_fallback_reason='bad inv')
    sel=SelectionResult(decision='reject_all', summary='none', reasons=['none'], selector_fallback_used=False, selector_normalized=True)
    dec=Decision(run_id='r', accepted=False, plan={'strategy':'parallel_candidates','candidate_attempts':1,'planner_fallback_used':True,'planner_fallback_reason':'bad plan'}, failure_reason='none')
    report=write_performance_report(tmp_path, Task(repo_path=str(tmp_path), objective='fix'), inv, [], sel, dec, 1.0).read_text()
    assert 'Investigation fallback used:' in report
    assert 'Planner fallback used:' in report
    assert 'Selector fallback used:' in report
    assert 'Planner normalized:' in report
    assert 'Selector normalized:' in report
    assert '\nFallback used:' not in report
    assert 'Planner fallback used: true' in report
    assert 'Selector fallback used: false' in report
