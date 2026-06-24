import json

from villani_ops.core.backend import Backend
from villani_ops.core.task import TaskClassification
from villani_ops.llm.client import LLMCallResult
from villani_ops.policy_engine.engine import ExecutionStrategy, PolicyEngine, normalize_execution_strategy_payload
from villani_ops.policy_engine.prompts import USER


def _backend(name, cap=50):
    return Backend(name=name, provider="openai-compatible", base_url="http://x/v1", model=name, api_key="dummy", roles=["coding", "policy"], capability_score=cap)


def _validate(raw, profile="balanced"):
    return ExecutionStrategy.model_validate(normalize_execution_strategy_payload(raw, profile))


def test_real_model_backend_sequence_and_execution_phases_normalize():
    strategy=_validate({
        "strategy_id": "exec_strat_bug_fix_easy_balanced",
        "strategy_name": "Balanced Cost-Quality Bug Fix",
        "profile": "balanced",
        "backend_sequence": ["qwen9b", "qwen35b"],
        "execution_phases": [
            {"phase": "initial_attempt", "assigned_backend": "qwen9b", "max_attempts": 1, "instructions": "Use the cheaper backend first."},
            {"phase": "escalation", "assigned_backend": "qwen35b", "max_attempts": 1, "instructions": "Escalate only if needed."},
        ],
        "termination_conditions": {"stop_on_success": True, "max_total_attempts": 2},
    })
    assert strategy.profile == "balanced"
    assert len(strategy.attempts) == 2
    assert [a.backend for a in strategy.attempts] == ["qwen9b", "qwen35b"]
    assert [a.max_attempts for a in strategy.attempts] == [1, 1]
    assert strategy.stop_conditions == {"mode": "first_accepted"}


def test_backend_sequence_alone_normalizes():
    strategy=_validate({"backend_sequence": ["qwen9b", "qwen35b"]})
    assert [a.backend for a in strategy.attempts] == ["qwen9b", "qwen35b"]


def test_execution_phases_preferred_over_backend_sequence():
    strategy=_validate({"backend_sequence": ["qwen35b", "qwen9b"], "execution_phases": [{"assigned_backend": "qwen9b", "max_attempts": 1}, {"assigned_backend": "qwen35b", "max_attempts": 1}]})
    assert [a.backend for a in strategy.attempts] == ["qwen9b", "qwen35b"]


def test_assigned_backend_alias():
    strategy=_validate({"execution_phases": [{"assigned_backend": "qwen9b"}]})
    assert strategy.attempts[0].backend == "qwen9b"


def test_cli_profile_wins():
    strategy=_validate({"profile": "quality", "backend_sequence": ["qwen35b"]}, "balanced")
    assert strategy.profile == "balanced"


def test_budget_trimming_by_profile():
    raw={"backend_sequence": ["qwen9b", "qwen35b", "qwen9b", "qwen35b"]}
    assert sum(a.max_attempts for a in _validate(raw, "balanced").attempts) <= 2
    assert sum(a.max_attempts for a in _validate(raw, "cheap").attempts) <= 1
    assert sum(a.max_attempts for a in _validate(raw, "quality").attempts) <= 3


class _Client:
    def __init__(self, payload): self.payload=payload
    def complete_json(self, *args, **kwargs):
        return LLMCallResult(parsed_json=self.payload, raw_text=json.dumps(self.payload), backend_name="qwen9b", model="m")


def test_invalid_planning_fields_trigger_fallback_artifact(tmp_path):
    backs={"qwen9b": _backend("qwen9b", 20), "qwen35b": _backend("qwen35b", 90)}
    strategy, _=PolicyEngine(_Client({"execution_phases": [{"phase": "initial_attempt", "assigned_backend": ""}]})).generate(TaskClassification(difficulty="medium", risk="medium"), backs, "balanced", tmp_path/"execution_strategy.json")
    artifact=json.loads((tmp_path/"controller_calls"/"policy_error.json").read_text())
    assert artifact["fallback_used"] is True
    assert artifact["fallback_reason"] if "fallback_reason" in artifact else artifact["validation_error"]
    assert artifact["fallback_payload"]
    assert any("deterministic fallback" in w for w in strategy.warnings)


def test_fallback_artifact_accurate_on_validation_failure(tmp_path):
    backs={"qwen9b": _backend("qwen9b")}
    PolicyEngine(_Client({"attempts": "not-a-list"})).generate(TaskClassification(), backs, "balanced", tmp_path/"execution_strategy.json")
    artifact=json.loads((tmp_path/"controller_calls"/"policy_error.json").read_text())
    assert artifact["fallback_used"] is True
    assert artifact["fallback_payload"]
    assert artifact["validation_error"]


def test_policy_prompt_contains_canonical_schema():
    for text in ["attempts", "backend", "max_attempts", "runner", "profile", "Use attempts, not execution_phases", "Use backend, not assigned_backend"]:
        assert text in USER


def test_real_shape_does_not_use_fallback(tmp_path):
    payload={"backend_sequence": ["qwen9b", "qwen35b"], "execution_phases": [{"assigned_backend": "qwen9b", "max_attempts": 1}, {"assigned_backend": "qwen35b", "max_attempts": 1}]}
    backs={"qwen9b": _backend("qwen9b", 20), "qwen35b": _backend("qwen35b", 90)}
    strategy, _=PolicyEngine(_Client(payload)).generate(TaskClassification(difficulty="medium", risk="medium"), backs, "balanced", tmp_path/"execution_strategy.json")
    assert [a.backend for a in strategy.attempts] == ["qwen9b", "qwen35b"]
    assert not any("deterministic fallback" in w or "Policy engine produced no valid attempts" in w for w in strategy.warnings)
    assert not (tmp_path/"controller_calls"/"policy_error.json").exists()
