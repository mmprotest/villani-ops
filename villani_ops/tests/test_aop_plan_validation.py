from villani_ops.orchestration.planner import DecompositionResult, Subtask, validate_decomposition_plan
from villani_ops.core.backend import Backend


def _backs():
    return {
        'reviewer': Backend(name='reviewer', provider='local', model='r', capability_score=99, roles=['review']),
        'coder': Backend(name='coder', provider='local', model='c', capability_score=10, roles=['coding'], max_parallel=2),
    }


def _good():
    return DecompositionResult(should_use_decomposition=True, reason='separable', subtasks=[
        Subtask(id='a', title='A', objective='implement alpha requirement', success_criteria='alpha requirement works', relevant_files=['a.py'], can_run_parallel=True),
        Subtask(id='b', title='B', objective='implement beta requirement', success_criteria='beta requirement works', relevant_files=['b.py'], can_run_parallel=True),
    ])


def test_plan_validation_accepts_good_decomposition():
    assert validate_decomposition_plan(_good(), task='alpha beta', success_criteria='alpha beta', backends=_backs()).accepted


def test_plan_validation_rejects_redundant_decomposition():
    d=_good(); d.subtasks[1].objective=d.subtasks[0].objective
    v=validate_decomposition_plan(d, task='alpha beta', success_criteria='alpha beta', backends=_backs())
    assert not v.accepted and not v.non_redundancy.passed


def test_plan_validation_rejects_invalid_dependencies_and_parallel_layout():
    d=_good(); d.subtasks[1].dependencies=['missing']; d.subtasks[1].relevant_files=['a.py']
    v=validate_decomposition_plan(d, task='alpha beta', success_criteria='alpha beta', backends=_backs())
    assert not v.accepted
    assert not v.dependency_validity.passed
    assert not v.parallel_safety.passed


def test_plan_validation_backend_fit_is_role_aware():
    d=_good(); d.subtasks[0].assigned_backend='reviewer'
    v=validate_decomposition_plan(d, task='alpha beta', success_criteria='alpha beta', backends=_backs())
    assert not v.accepted and not v.backend_fit.passed
import json
from pathlib import Path
from types import SimpleNamespace

from villani_ops.llm.client import LLMCallResult
from villani_ops.orchestration.planner import (
    DecompositionPlanValidationResult,
    revise_decomposition_with_feedback,
    semantic_validate_decomposition_plan,
)


class _SeqClient:
    def __init__(self, payloads):
        self.payloads=list(payloads); self.prompts=[]
    def complete_json(self, backend, system_prompt, user_prompt, schema_name, **kw):
        self.prompts.append(json.loads(user_prompt))
        payload=self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, str):
            return LLMCallResult(parsed_json={}, raw_text=payload, backend_name=backend.name, model=backend.model)
        return LLMCallResult(parsed_json=payload, raw_text=json.dumps(payload), backend_name=backend.name, model=backend.model)


def _validator_payload(accepted=True, **sections):
    payload={"accepted": accepted, "required_revisions": []}
    payload.update(sections)
    return payload


def test_semantic_validator_accepts_paraphrased_valid_plan():
    det=validate_decomposition_plan(_good(), task="persist user preferences and notify clients", success_criteria="settings are saved; subscribers are informed", backends=_backs())
    sem, meta=semantic_validate_decomposition_plan(_SeqClient([_validator_payload(True)]), _good(), task="persist prefs and alert clients", success_criteria="save settings; send notification", deterministic=det, backend=_backs()["reviewer"])
    assert sem.accepted and not meta["malformed"]


def test_semantic_validator_rejects_redundant_and_unsafe_plans():
    det=validate_decomposition_plan(_good(), task="alpha beta", success_criteria="alpha beta", backends=_backs())
    sem,_=semantic_validate_decomposition_plan(_SeqClient([_validator_payload(False, non_redundancy={"passed":False,"issues":["duplicate"],"overlapping_subtasks":[{"subtasks":["a","b"]}]})]), _good(), task="t", success_criteria="s", deterministic=det, backend=_backs()["reviewer"])
    assert not sem.accepted and not sem.non_redundancy.passed
    sem,_=semantic_validate_decomposition_plan(_SeqClient([_validator_payload(False, parallel_safety={"passed":False,"issues":["shared module"],"unsafe_parallel_groups":[{"subtasks":["a","b"]}]})]), _good(), task="t", success_criteria="s", deterministic=det, backend=_backs()["reviewer"])
    assert not sem.accepted and not sem.parallel_safety.passed


def test_malformed_semantic_validation_is_reported_not_accepted():
    det=validate_decomposition_plan(_good(), task="alpha beta", success_criteria="alpha beta", backends=_backs())
    sem, meta=semantic_validate_decomposition_plan(_SeqClient([ValueError("bad json")]), _good(), task="t", success_criteria="s", deterministic=det, backend=_backs()["reviewer"])
    assert sem is None and meta["malformed"] and meta["status"] == "failed"


def test_deterministic_dependency_cycle_and_invalid_dependency_reject_without_semantic():
    d=_good(); d.subtasks[0].dependencies=["b"]; d.subtasks[1].dependencies=["a"]
    assert not validate_decomposition_plan(d, task="t", success_criteria="s", backends=None).dependency_validity.passed
    d=_good(); d.subtasks[0].dependencies=["missing"]
    assert not validate_decomposition_plan(d, task="t", success_criteria="s", backends=None).dependency_validity.passed


def test_structured_reviser_receives_feedback_and_returns_corrected_plan():
    validation=DecompositionPlanValidationResult(accepted=False)
    validation.completeness.passed=False; validation.completeness.missing_success_criteria=["beta"] ; validation.required_revisions=["add beta"]
    revised=_good().model_dump(mode="json"); revised["subtasks"].append({"id":"c","title":"C","objective":"implement gamma","success_criteria":"gamma works"})
    client=_SeqClient([revised])
    plan, meta=revise_decomposition_with_feedback(task="alpha beta gamma", success_criteria=["alpha","beta","gamma"], original_plan=_good(), validation_result=validation, backend_registry=_backs(), client=client, backend=_backs()["reviewer"])
    assert meta["parsed_cleanly"] and plan is not None
    assert client.prompts[0]["validation_failures"]["completeness"]["missing_success_criteria"] == ["beta"]
    assert "add beta" in client.prompts[0]["required_revisions"]


def test_structured_reviser_malformed_plan_falls_back_signal():
    validation=DecompositionPlanValidationResult(accepted=False, required_revisions=["fix"])
    plan, meta=revise_decomposition_with_feedback(task="t", success_criteria=["s"], original_plan=_good(), validation_result=validation, backend_registry=_backs(), client=_SeqClient([{"not":"a decomposition"}]), backend=_backs()["reviewer"])
    assert plan is None and not meta["parsed_cleanly"]


def test_runtime_semantic_rejection_revises_once_and_activates(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from villani_ops.orchestration.engine import OrchestrationEngine, EngineContext
    from villani_ops.orchestration.graph import OrchestrationGraph
    from villani_ops.orchestration.nodes import OrchestrationNode
    from villani_ops.orchestration.context import TaskContext
    from villani_ops.core.task import Task
    from villani_ops.core.backend import Backend
    from villani_ops.execution_policies.base import BackendSelection
    from villani_ops.orchestration.planner import PlanResult, DecompositionResult, Subtask, DecompositionPlanValidationResult
    import threading, time, json
    class Policy:
        mode='performance'
        def select_backend(self, **kw): b=kw['backends']['b']; return BackendSelection(backend_name='b',backend=b,reason='x')
    class Runner: name='villani-code'
    def dec():
        return DecompositionResult(should_use_decomposition=True, reason='r', subtasks=[Subtask(id='a',title='A',objective='oa',success_criteria='sa'),Subtask(id='b',title='B',objective='ob',success_criteria='sb')])
    calls={'rev':0,'sem':0}
    monkeypatch.setattr('villani_ops.orchestration.engine.Planner.decompose', lambda *a, **k: (dec(), SimpleNamespace(raw_text='{}')))
    monkeypatch.setattr('villani_ops.orchestration.engine.validate_decomposition_plan', lambda *a, **k: DecompositionPlanValidationResult(accepted=True))
    def sem(*a, **k):
        calls['sem']+=1
        return DecompositionPlanValidationResult(accepted=calls['sem']==2, required_revisions=[] if calls['sem']==2 else ['missing']), {'status':'ok'}
    monkeypatch.setattr('villani_ops.orchestration.engine.semantic_validate_decomposition_plan', sem)
    def rev(*a, **k): calls['rev']+=1; return dec(), {'parsed_cleanly':True,'revision_request':{'x':1}}
    monkeypatch.setattr('villani_ops.orchestration.engine.revise_decomposition_with_feedback', rev)
    e=OrchestrationEngine(backends={'b':Backend(name='b',provider='openai',model='m')},execution_policy=Policy(),runner_adapter=Runner(),workspace=tmp_path/'ws')
    g=OrchestrationGraph(run_id='r',nodes=[OrchestrationNode(id='decompose',kind='decompose',objective='d')])
    ctx=EngineContext(repo=tmp_path,task=Task(repo_path=str(tmp_path),objective='x'),candidate_attempts=2,timeout_seconds=None,isolation='worktree',run_id='r',run_dir=tmp_path/'run',mode='performance',runner='villani-code',graph=g,scheduler=None,task_context=TaskContext(objective='x'),start=time.time())
    (ctx.run_dir).mkdir(); ctx.plan=PlanResult(summary='p',should_decompose=True,candidate_attempts=2); node=g.get('decompose'); node.assigned_backend='b'; node.assigned_model='m'
    e._execute_decompose_node(node,ctx)
    assert ctx.decomposed_active is True and calls['rev']==1
    assert json.loads((ctx.run_dir/'decomposition'/'plan_validation_decision.json').read_text())['decision']=='decomposition_revised_and_accepted'
    assert (ctx.run_dir/'decomposition'/'plan_revision_request.json').exists()


def test_runtime_semantic_rejection_twice_falls_back(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from villani_ops.orchestration.engine import OrchestrationEngine, EngineContext
    from villani_ops.orchestration.graph import OrchestrationGraph
    from villani_ops.orchestration.nodes import OrchestrationNode
    from villani_ops.orchestration.context import TaskContext
    from villani_ops.core.task import Task
    from villani_ops.core.backend import Backend
    from villani_ops.execution_policies.base import BackendSelection
    from villani_ops.orchestration.planner import PlanResult, DecompositionResult, Subtask, DecompositionPlanValidationResult
    import time, json
    class Policy:
        mode='performance'
        def select_backend(self, **kw): b=kw['backends']['b']; return BackendSelection(backend_name='b',backend=b,reason='x')
    class Runner: name='villani-code'
    dec=DecompositionResult(should_use_decomposition=True, reason='r', subtasks=[Subtask(id='a',title='A',objective='oa',success_criteria='sa'),Subtask(id='b',title='B',objective='ob',success_criteria='sb')])
    calls={'rev':0}
    monkeypatch.setattr('villani_ops.orchestration.engine.Planner.decompose', lambda *a, **k: (dec, SimpleNamespace(raw_text='{}')))
    monkeypatch.setattr('villani_ops.orchestration.engine.validate_decomposition_plan', lambda *a, **k: DecompositionPlanValidationResult(accepted=True))
    monkeypatch.setattr('villani_ops.orchestration.engine.semantic_validate_decomposition_plan', lambda *a, **k: (DecompositionPlanValidationResult(accepted=False, required_revisions=['bad']), {'status':'ok'}))
    def rev(*a, **k): calls['rev']+=1; return dec, {'parsed_cleanly':True,'revision_request':{}}
    monkeypatch.setattr('villani_ops.orchestration.engine.revise_decomposition_with_feedback', rev)
    e=OrchestrationEngine(backends={'b':Backend(name='b',provider='openai',model='m')},execution_policy=Policy(),runner_adapter=Runner(),workspace=tmp_path/'ws')
    g=OrchestrationGraph(run_id='r',nodes=[OrchestrationNode(id='decompose',kind='decompose',objective='d')])
    ctx=EngineContext(repo=tmp_path,task=Task(repo_path=str(tmp_path),objective='x'),candidate_attempts=2,timeout_seconds=None,isolation='worktree',run_id='r',run_dir=tmp_path/'run',mode='performance',runner='villani-code',graph=g,scheduler=None,task_context=TaskContext(objective='x'),start=time.time())
    ctx.run_dir.mkdir(); ctx.plan=PlanResult(summary='p',should_decompose=True,candidate_attempts=2); node=g.get('decompose'); node.assigned_backend='b'; node.assigned_model='m'
    e._execute_decompose_node(node,ctx)
    assert ctx.decomposed_active is False and calls['rev']==1
    assert json.loads((ctx.run_dir/'decomposition'/'plan_validation_decision.json').read_text())['decision']=='decomposition_rejected_fallback_to_candidates'


def _runtime_decompose_context(tmp_path):
    import time
    from villani_ops.orchestration.engine import OrchestrationEngine, EngineContext
    from villani_ops.orchestration.graph import OrchestrationGraph
    from villani_ops.orchestration.nodes import OrchestrationNode
    from villani_ops.orchestration.context import TaskContext
    from villani_ops.core.task import Task
    from villani_ops.orchestration.planner import PlanResult
    from villani_ops.execution_policies.base import BackendSelection
    class Policy:
        mode='performance'
        def select_backend(self, **kw):
            b=kw['backends']['b']; return BackendSelection(backend_name='b',backend=b,reason='x')
    class Runner: name='villani-code'
    e=OrchestrationEngine(backends={'b':Backend(name='b',provider='openai',model='m',roles=['policy','coding','review'])},execution_policy=Policy(),runner_adapter=Runner(),workspace=tmp_path/'ws')
    g=OrchestrationGraph(run_id='r',nodes=[OrchestrationNode(id='decompose',kind='decompose',objective='d')])
    ctx=EngineContext(repo=tmp_path,task=__import__('villani_ops.core.task', fromlist=['Task']).Task(repo_path=str(tmp_path),objective='x'),candidate_attempts=2,timeout_seconds=None,isolation='worktree',run_id='r',run_dir=tmp_path/'run',mode='performance',runner='villani-code',graph=g,scheduler=None,task_context=TaskContext(objective='x'),start=time.time())
    ctx.run_dir.mkdir(); ctx.plan=PlanResult(summary='p',should_decompose=True,candidate_attempts=2); node=g.get('decompose'); node.assigned_backend='b'; node.assigned_model='m'
    return e, ctx, node


def _two_subtask_dec():
    from villani_ops.orchestration.planner import DecompositionResult, Subtask
    return DecompositionResult(should_use_decomposition=True, reason='r', subtasks=[Subtask(id='a',title='A',objective='oa',success_criteria='sa'),Subtask(id='b',title='B',objective='ob',success_criteria='sb')])


def test_runtime_malformed_semantic_validation_falls_back_without_graph_rewrite(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from villani_ops.orchestration.planner import DecompositionPlanValidationResult
    monkeypatch.setattr('villani_ops.orchestration.engine.Planner.decompose', lambda *a, **k: (_two_subtask_dec(), SimpleNamespace(raw_text='{}')))
    monkeypatch.setattr('villani_ops.orchestration.engine.validate_decomposition_plan', lambda *a, **k: DecompositionPlanValidationResult(accepted=True))
    monkeypatch.setattr('villani_ops.orchestration.engine.semantic_validate_decomposition_plan', lambda *a, **k: (None, {'status':'failed','malformed':True,'error':'schema invalid'}))
    monkeypatch.setattr('villani_ops.orchestration.engine.revise_decomposition_with_feedback', lambda *a, **k: (None, {'parsed_cleanly':False,'error':'not parseable','revision_request':{}}))
    e,ctx,node=_runtime_decompose_context(tmp_path)
    e._execute_decompose_node(node,ctx)
    assert ctx.decomposed_active is False
    assert not any(n.id.startswith('subtask_') for n in ctx.graph.nodes)
    decision=json.loads((ctx.run_dir/'decomposition'/'plan_validation_decision.json').read_text())
    assert decision['decision']=='decomposition_rejected_fallback_to_candidates'
    assert decision['semantic_status']['malformed'] is True
    initial=json.loads((ctx.run_dir/'decomposition'/'plan_validation_semantic_initial.json').read_text())
    assert initial['malformed'] is True and 'schema invalid' in initial['error']


def test_runtime_malformed_revision_falls_back_and_reviser_called_once(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from villani_ops.orchestration.planner import DecompositionPlanValidationResult
    calls={'rev':0}
    monkeypatch.setattr('villani_ops.orchestration.engine.Planner.decompose', lambda *a, **k: (_two_subtask_dec(), SimpleNamespace(raw_text='{}')))
    monkeypatch.setattr('villani_ops.orchestration.engine.validate_decomposition_plan', lambda *a, **k: DecompositionPlanValidationResult(accepted=True))
    monkeypatch.setattr('villani_ops.orchestration.engine.semantic_validate_decomposition_plan', lambda *a, **k: (DecompositionPlanValidationResult(accepted=False, required_revisions=['fix']), {'status':'ok','malformed':False}))
    def rev(*a, **k):
        calls['rev']+=1; return None, {'parsed_cleanly':False,'error':'malformed revised decomposition','revision_request':{'required_revisions':['fix']}}
    monkeypatch.setattr('villani_ops.orchestration.engine.revise_decomposition_with_feedback', rev)
    e,ctx,node=_runtime_decompose_context(tmp_path)
    e._execute_decompose_node(node,ctx)
    assert calls['rev']==1 and ctx.decomposed_active is False
    assert not any(n.id.startswith('subtask_') for n in ctx.graph.nodes)
    rev_art=json.loads((ctx.run_dir/'decomposition'/'plan_revision_result.json').read_text())
    assert rev_art['parsed_cleanly'] is False and 'malformed' in rev_art['error']
    assert json.loads((ctx.run_dir/'decomposition'/'plan_validation_decision.json').read_text())['accepted'] is False


def test_runtime_deterministic_guardrail_rejects_even_if_semantic_would_accept(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from villani_ops.orchestration.planner import DecompositionPlanValidationResult
    calls={'sem':0}
    det=DecompositionPlanValidationResult(accepted=False, required_revisions=['invalid dependency'])
    det.dependency_validity.passed=False; det.dependency_validity.issues=['missing dependency']
    monkeypatch.setattr('villani_ops.orchestration.engine.Planner.decompose', lambda *a, **k: (_two_subtask_dec(), SimpleNamespace(raw_text='{}')))
    monkeypatch.setattr('villani_ops.orchestration.engine.validate_decomposition_plan', lambda *a, **k: det)
    def sem(*a, **k):
        calls['sem']+=1; return DecompositionPlanValidationResult(accepted=True), {'status':'ok'}
    monkeypatch.setattr('villani_ops.orchestration.engine.semantic_validate_decomposition_plan', sem)
    e,ctx,node=_runtime_decompose_context(tmp_path)
    e._execute_decompose_node(node,ctx)
    assert calls['sem']==0
    assert ctx.decomposed_active is False
    assert not any(n.id.startswith('subtask_') for n in ctx.graph.nodes)
    decision=json.loads((ctx.run_dir/'decomposition'/'plan_validation_decision.json').read_text())
    assert decision['accepted'] is False and decision['deterministic_accepted'] is False
    assert decision['semantic_status']['reason']=='deterministic validation failed'
    assert decision['deterministic_failures']['dependency_validity']['passed'] is False
