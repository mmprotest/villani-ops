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
