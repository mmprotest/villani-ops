from villani_ops.core.backend import Backend
from villani_ops.core.task import TaskClassification
from villani_ops.llm.client import LLMCallResult
from villani_ops.policy_engine.engine import (
    ExecutionStrategy,
    PolicyEngine,
    build_deterministic_fallback_strategy,
    normalize_execution_strategy_payload,
)


def _attempt():
    return {"backend": "code", "max_attempts": 1, "reason": "test"}


def _backend(name="code", cap=10, cost=0.0):
    return Backend(
        name=name,
        provider="openai-compatible",
        base_url="http://x/v1",
        model=name,
        api_key="dummy",
        roles=["coding", "policy"],
        capability_score=cap,
        input_cost_per_million=cost,
        output_cost_per_million=cost,
    )


def test_strategy_name_alias_validates():
    payload=normalize_execution_strategy_payload({"strategy_name":"balanced","planned_attempts":[_attempt()]}, "balanced")
    strategy=ExecutionStrategy.model_validate(payload)
    assert strategy.profile == "balanced"
    assert strategy.attempts[0].backend == "code"


def test_cli_profile_wins_over_model_profile():
    payload=normalize_execution_strategy_payload({"profile":"quality","planned_attempts":[_attempt()]}, "balanced")
    strategy=ExecutionStrategy.model_validate(payload)
    assert strategy.profile == "balanced"


class _InvalidClient:
    def complete_json(self, *args, **kwargs):
        return LLMCallResult(
            parsed_json={"profile":"quality","attempts":"not-a-list"},
            raw_text='{"profile":"quality","attempts":"not-a-list"}',
            input_tokens=12,
            output_tokens=7,
            estimated_cost=0.25,
            backend_name="policy",
            model="m",
            usage={"prompt_tokens":12,"completion_tokens":7},
        )


def test_deterministic_fallback_on_validation_failure(tmp_path):
    backs={"code":_backend("code", cap=10), "strong":_backend("strong", cap=90)}
    strategy, call=PolicyEngine(_InvalidClient()).generate(TaskClassification(), backs, "balanced", tmp_path/"execution_strategy.json")
    assert strategy.profile == "balanced"
    assert len(strategy.attempts) >= 1
    assert any("deterministic fallback" in w for w in strategy.warnings)


def test_failed_policy_cost_is_counted_in_call_result(tmp_path):
    backs={"code":_backend("code", cap=10, cost=1000)}
    strategy, call=PolicyEngine(_InvalidClient()).generate(TaskClassification(), backs, "balanced", tmp_path/"execution_strategy.json")
    assert call.estimated_cost > 0
    assert call.input_tokens > 0
    assert call.output_tokens > 0


def test_policy_error_artifact_written(tmp_path):
    backs={"code":_backend("code", cap=10)}
    PolicyEngine(_InvalidClient()).generate(TaskClassification(), backs, "balanced", tmp_path/"execution_strategy.json")
    artifact=tmp_path/"controller_calls"/"policy_error.json"
    assert artifact.exists()
    text=artifact.read_text()
    assert '"phase": "policy"' in text
    assert '"schema": "ExecutionStrategy"' in text
    assert '"validation_error"' in text
