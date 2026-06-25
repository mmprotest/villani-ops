import json
from villani_ops.performance.investigator import normalize_investigation_payload
from villani_ops.performance.models import InvestigationResult
from villani_ops.orchestration.planner import normalize_plan_payload, PlanResult


def test_investigation_aliases_and_coercions():
    payload={"analysis":"sum","root_cause":"cause","files":"src/a.py","tests":"tests/test_a.py","plan":"do it","risks":"risk","confidence":2}
    norm, notes=normalize_investigation_payload(payload)
    inv=InvestigationResult.model_validate(norm)
    assert inv.summary == "sum"
    assert inv.suspected_root_cause == "cause"
    assert inv.relevant_files == ["src/a.py"]
    assert inv.relevant_tests == ["tests/test_a.py"]
    assert inv.implementation_plan == ["do it"]
    assert inv.risks == ["risk"]
    assert inv.confidence == 1.0
    assert any("Mapped analysis to summary" == n for n in notes)


def test_investigation_findings_missing_confidence_and_no_summary_fallback_signal():
    norm, notes=normalize_investigation_payload({"findings":"found"})
    assert norm["summary"] == "found"
    assert norm["confidence"] == 0.0
    assert notes
    bad, _=normalize_investigation_payload({"confidence": .5})
    assert not bad.get("summary")


def test_plan_aliases_and_coercions():
    norm, notes=normalize_plan_payload({"plan":["Run tests","Fix logic"],"execution_strategy":"parallel","candidates":99,"decompose":True,"difficulty":"bogus","confidence":-1,"warnings":"risk"}, requested_candidate_attempts=3)
    plan=PlanResult.model_validate(norm)
    assert "Run tests" in plan.summary
    assert plan.strategy == "parallel_candidates"
    assert plan.candidate_attempts == 8
    assert plan.should_decompose is True
    assert plan.expected_difficulty == "unknown"
    assert plan.confidence == 0.0
    assert plan.risks == ["risk"]
    assert notes


def test_plan_approach_strategy_maps_defaults_and_no_summary_fallback_signal():
    assert normalize_plan_payload({"approach":"do","execution_strategy":"single"}, requested_candidate_attempts=2)[0]["strategy"] == "single_task"
    assert normalize_plan_payload({"approach":"do","execution_strategy":"decompose"}, requested_candidate_attempts=2)[0]["strategy"] == "decompose_then_execute"
    norm, notes=normalize_plan_payload({"approach":"do"}, requested_candidate_attempts=4)
    assert norm["candidate_attempts"] == 4
    bad, _=normalize_plan_payload({"confidence": .8}, requested_candidate_attempts=3)
    assert not bad.get("summary")
