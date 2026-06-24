from __future__ import annotations

import json
from pathlib import Path

from villani_ops.classification.classifier import (
    TaskClassifier,
    fallback_task_classification_payload,
    normalize_task_classification_payload,
)
from villani_ops.core.backend import Backend
from villani_ops.core.task import Task, TaskClassification
from villani_ops.llm.client import LLMCallResult


def _validated(payload: dict) -> TaskClassification:
    return TaskClassification.model_validate(normalize_task_classification_payload(payload))


def test_moderate_difficulty_normalizes_to_medium():
    cls = _validated({
        "difficulty": "moderate",
        "category": "bug_fix",
        "risk": "low",
        "estimated_attempts_needed": 2,
        "needs_tests": False,
        "likely_files": ["src/signalshop/pricing.py"],
        "required_capabilities": ["decimal_arithmetic"],
        "reasoning_summary": "Focused pricing bug fix.",
        "confidence": 0.95,
    })

    assert cls.difficulty == "medium"
    assert cls.risk == "low"
    assert cls.category == "bug_fix"
    assert cls.confidence == 0.95


def test_messy_strings_normalize():
    cls = _validated({
        "difficulty": "Very Hard",
        "risk": "low-medium",
        "needs_tests": "no",
        "confidence": "95%",
    })

    assert cls.difficulty == "hard"
    assert cls.risk == "medium"
    assert cls.needs_tests is False
    assert cls.confidence == 0.95


def test_missing_or_malformed_fields_are_repaired():
    cls = _validated({
        "difficulty": "???",
        "risk": None,
        "estimated_attempts_needed": "many",
        "likely_files": "src/foo.py",
        "required_capabilities": "python",
        "confidence": 7,
    })

    assert cls.difficulty == "medium"
    assert cls.risk == "medium"
    assert cls.estimated_attempts_needed == 2
    assert cls.likely_files == ["src/foo.py"]
    assert cls.required_capabilities == ["python"]
    assert cls.confidence == 1.0


def test_fallback_payload_validates_after_unrecoverable_validation_failure():
    cls = TaskClassification.model_validate(fallback_task_classification_payload())

    assert cls.difficulty == "medium"
    assert cls.category == "unknown"
    assert cls.risk == "medium"
    assert cls.estimated_attempts_needed == 2
    assert cls.needs_tests is True


def test_real_model_output_regression():
    cls = _validated({
        "difficulty": "moderate",
        "category": "bug_fix",
        "risk": "low",
        "estimated_attempts_needed": 2,
        "needs_tests": False,
        "likely_files": ["src/signalshop/pricing.py"],
        "required_capabilities": [
            "python_programming",
            "decimal_arithmetic",
            "business_logic_implementation",
            "unit_testing_debugging",
        ],
        "reasoning_summary": "Task requires precise modifications to financial calculation logic, enforcing specific operation ordering (discount → tax → shipping), implementing exact decimal rounding, and adding input validation. Scope is tightly bounded to the pricing module with existing test coverage, indicating a focused bug fix with low regression risk.",
        "confidence": 0.95,
    })

    assert cls.difficulty == "medium"
    assert cls.category == "bug_fix"
    assert cls.risk == "low"


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, backend, system_prompt, user_prompt, schema_name):
        return LLMCallResult(
            parsed_json=self.payload,
            raw_text=json.dumps(self.payload),
            backend_name=backend.name,
            model=backend.model,
            input_tokens=11,
            output_tokens=7,
            estimated_cost=0.25,
            usage={"prompt_tokens": 11, "completion_tokens": 7},
        )


def _backend():
    return Backend(
        name="local",
        provider="openai-compatible",
        base_url="http://localhost/v1",
        model="qwen",
        roles=["classification", "coding", "policy"],
    )


def test_classification_validation_failure_writes_artifact(tmp_path, monkeypatch):
    def invalid_normalize(raw):
        return {"difficulty": "impossible", "risk": "low"}

    monkeypatch.setattr("villani_ops.classification.classifier.normalize_task_classification_payload", invalid_normalize)
    out_path = tmp_path / "classification.json"
    task = Task(repo_path=str(tmp_path), objective="Fix bug")

    cls, call = TaskClassifier(_FakeClient({"difficulty": "impossible"})).classify(task, {"local": _backend()}, out_path)

    assert cls.difficulty == "medium"
    assert call.input_tokens == 11
    artifact = tmp_path / "controller_calls" / "classification_error.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text())
    assert data["phase"] == "classification"
    assert data["schema"] == "TaskClassification"
    assert data["fallback_used"] is True
    assert data["fallback_payload"]["difficulty"] == "medium"


def test_classification_cost_tokens_preserved_when_initial_validation_would_fail(tmp_path):
    payload={"difficulty":"moderate","risk":"low","confidence":"95%"}
    task = Task(repo_path=str(tmp_path), objective="Fix bug")

    cls, call = TaskClassifier(_FakeClient(payload)).classify(task, {"local": _backend()}, tmp_path / "classification.json")

    assert cls.difficulty == "medium"
    assert call.estimated_cost > 0
    assert call.input_tokens > 0
    assert call.output_tokens > 0
