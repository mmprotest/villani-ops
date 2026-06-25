import json
from types import SimpleNamespace

from villani_ops.orchestration.planner import normalize_decomposition_payload, DecompositionResult, Planner
from villani_ops.llm.client import LLMCallResult
from villani_ops.core.backend import Backend
from villani_ops.orchestration.artifacts import write_text_utf8, write_json_utf8
from villani_ops.performance.report import write_performance_report
from villani_ops.core.task import Task
from villani_ops.core.decision import Decision


def test_strict_valid_decomposition_passes_without_normalization():
    payload={"should_use_decomposition": True, "reason":"r", "subtasks":[{"id":"a","title":"A","objective":"Do A"}], "confidence": .7}
    dec=DecompositionResult.model_validate(payload)
    assert dec.should_use_decomposition is True
    assert dec.subtasks[0].id == "a"


def test_wrong_shaped_subtasks_description_files_normalizes():
    norm, notes=normalize_decomposition_payload({"subtasks":[{"id":"fix_pricing_logic","description":"Correct coupon/tax/shipping order and quantity validation.","files":["src/signalshop/pricing.py"]}]})
    assert norm["should_use_decomposition"] is True
    assert norm["reason"] == "Task was decomposed into separable subtasks."
    assert norm["merge_strategy"] == "Apply coordinated changes in one candidate patch and validate full test suite."
    st=norm["subtasks"][0]
    assert st["title"] == "Correct coupon/tax/shipping order and quantity validation."
    assert st["objective"] == "Correct coupon/tax/shipping order and quantity validation."
    assert st["relevant_files"] == ["src/signalshop/pricing.py"]
    assert any("files" in n for n in notes)


def test_missing_id_string_aliases_confidence_and_invalid_enums():
    norm, _=normalize_decomposition_payload({"tasks":[{"description":"Do work","difficulty":"weird","risk":"odd"}, "Second task"], "should_decompose":"yes", "confidence":"85%"})
    assert norm["should_use_decomposition"] is True
    assert norm["confidence"] == 0.85
    assert norm["subtasks"][0]["id"] == "subtask_001"
    assert norm["subtasks"][0]["expected_difficulty"] == "unknown"
    assert norm["subtasks"][0]["risk"] == "unknown"
    assert norm["subtasks"][1]["title"] == "Second task"


def test_steps_alias_defaults_reason_merge():
    norm, _=normalize_decomposition_payload({"steps":["Fix A"]})
    assert norm["should_use_decomposition"] is True
    assert norm["reason"]
    assert norm["merge_strategy"]


def test_utf8_artifact_writes_and_json_preserves_unicode(tmp_path):
    write_text_utf8(tmp_path/"investigation.raw.txt", "ok ✅")
    write_text_utf8(tmp_path/"decomposition.raw.txt", "ok ✅")
    write_json_utf8(tmp_path/"decomposition_normalized.json", {"msg":"ok ✅"})
    assert "✅" in (tmp_path/"decomposition_normalized.json").read_text(encoding="utf-8")
    assert json.loads((tmp_path/"decomposition_normalized.json").read_text(encoding="utf-8"))["msg"] == "ok ✅"


def test_report_generation_with_unicode_and_decomposition(tmp_path):
    task=Task(repo_path=str(tmp_path), objective="Fix ✅", success_criteria="ok")
    dec={"should_use_decomposition": True, "decomposition_normalized": True, "decomposition_fallback_used": False, "subtasks":[{"id":"fix_pricing_logic","title":"Correct ✅","objective":"Correct ✅","relevant_files":["src/signalshop/pricing.py"]}], "merge_strategy":"merge ✅"}
    decision=Decision(run_id="r", mode="performance", runner="villani-code", accepted=False, lifecycle_completed=True, final_state="failed", final_action="fail", candidate_attempts_requested=1, decomposition=dec, acceptance_blockers=[], warnings=[])
    (tmp_path/"controller_steps.jsonl").write_text("{}\n", encoding="utf-8")
    p=write_performance_report(tmp_path, task, None, [], None, decision, 0)
    text=p.read_text(encoding="utf-8")
    assert "Decomposition normalized: true" in text
    assert "Decomposition fallback used: false" in text
    assert "Subtask count: 1" in text
    assert "fix_pricing_logic" in text and "src/signalshop/pricing.py" in text
    assert "No decomposition was used" not in text
    assert "\nFallback used:" not in text


def test_decompose_live_path_normalizes_and_writes(tmp_path):
    raw={"subtasks":[{"id":"fix_pricing_logic","description":"Correct coupon/tax/shipping order and quantity validation.","files":["src/signalshop/pricing.py"]},{"id":"implement_atomic_reservation","description":"Make inventory reservation atomic and avoid partial stock mutation.","files":["src/signalshop/inventory.py","src/signalshop/checkout.py"]}]}
    class Client:
        def complete_json(self, *a, **k): return LLMCallResult(parsed_json=raw, raw_text=json.dumps(raw), backend_name="b", model="m")
    planner=Planner(Client())
    plan=SimpleNamespace(model_dump=lambda mode='json': {"should_decompose": True}, should_decompose=True)
    task=Task(repo_path=str(tmp_path), objective="Fix checkout")
    dec, call=planner.decompose(task=task, plan=plan, investigation={}, backend=Backend(name="b", provider="openai", model="m"), run_dir=tmp_path)
    out=json.loads((tmp_path/"decomposition.json").read_text(encoding="utf-8"))
    assert out["should_use_decomposition"] is True
    assert len(out["subtasks"]) == 2
    assert out["decomposition_normalized"] is True
    assert out["decomposition_fallback_used"] is False
    norm=json.loads((tmp_path/"decomposition_normalized.json").read_text(encoding="utf-8"))
    assert norm["decomposition_normalized"] is True
    assert dec.subtasks[0].id == "fix_pricing_logic"
