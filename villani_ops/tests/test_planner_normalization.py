import json
from pathlib import Path
from types import SimpleNamespace

from villani_ops.core.backend import Backend
from villani_ops.core.task import Task
from villani_ops.llm.client import LLMCallResult
from villani_ops.orchestration.planner import Planner, PlanResult, DecompositionResult, normalize_plan_payload
from villani_ops.orchestration.progress import ConsoleProgressReporter
from villani_ops.performance.report import write_performance_report
from villani_ops.performance.investigator import normalize_investigation_payload
from villani_ops.core.decision import Decision


def _plan(payload, requested=3, **kw):
    norm, notes = normalize_plan_payload(payload, requested_candidate_attempts=requested, **kw)
    return PlanResult.model_validate(norm), notes


def test_strict_valid_planresult_passes_without_normalization(tmp_path):
    payload={"summary":"do it","strategy":"parallel_candidates","should_decompose":False,"candidate_attempts":3,"risks":[],"expected_difficulty":"medium","confidence":.7}
    plan=PlanResult.model_validate(payload)
    assert plan.planner_normalized is False


def test_summary_aliases():
    cases=[({"plan":["Inspect failing tests","Fix pricing"]},"Inspect failing tests"),({"plan":"Do the plan"},"Do the plan"),({"steps":["a","b"]},"a; b"),({"approach":"Use modules"},"Use modules"),({"thought":"Task spans files"},"Task spans files"),({"analysis":"Analyze bug"},"Analyze bug")]
    for payload, expected in cases:
        plan,_=_plan(payload)
        assert expected in plan.summary


def test_strategy_aliases():
    assert _plan({"summary":"x","execution_strategy":"parallel"})[0].strategy == "parallel_candidates"
    assert _plan({"summary":"x","execution_strategy":"single"})[0].strategy == "single_task"
    assert _plan({"summary":"x","execution_strategy":"decompose"})[0].strategy == "decompose_then_execute"


def test_subtasks_drive_decomposition_and_summary():
    plan,_=_plan({"subtasks":["Fix pricing", {"title":"Fix inventory","objective":"Rollback reservations"}]})
    assert plan.should_decompose is True
    assert plan.strategy == "decompose_then_execute"
    assert "Fix pricing" in plan.summary
    assert plan.decomposition_reason == "Task contains multiple separable subtasks."


def test_resulting_state_files_drive_decomposition_and_action_shape():
    payload={"thought":"The task spans checkout, pricing, inventory, orders, and receipts.","command":"ls -R","resulting_state":{"files":["src/signalshop/pricing.py","src/signalshop/inventory.py","src/signalshop/checkout.py","src/signalshop/orders.py"]}}
    plan,notes=_plan(payload)
    assert plan.should_decompose is True
    assert plan.strategy == "decompose_then_execute"
    assert "checkout" in plan.summary
    assert any("resulting_state.files" in n for n in notes)


def test_candidate_attempts_defaults_and_clamps():
    assert _plan({"summary":"x","candidates":"2"})[0].candidate_attempts == 2
    assert _plan({"summary":"x"}, requested=4)[0].candidate_attempts == 4
    assert _plan({"summary":"x","candidate_attempts":99})[0].candidate_attempts == 8
    assert _plan({"summary":"x","candidate_attempts":0})[0].candidate_attempts == 1


def test_decomposition_difficulty_confidence_and_risks():
    plan,_=_plan({"summary":"x","decomposition":"needed","complexity":"nonsense","confidence":85,"warnings":"careful"})
    assert plan.should_decompose is True
    assert plan.decomposition_reason == "needed"
    assert plan.expected_difficulty == "unknown"
    assert plan.confidence == 0.85
    assert "careful" in plan.risks
    assert _plan({"summary":"x","confidence":"85%"})[0].confidence == 0.85


class FakeClient:
    def __init__(self, payload, raw=None): self.payload=payload; self.raw=raw if raw is not None else json.dumps(payload)
    def complete_json(self, *args, **kwargs):
        return LLMCallResult(parsed_json=self.payload, raw_text=self.raw, backend_name="b", model="m")


def test_normalized_planner_does_not_set_fallback_and_writes_artifact(tmp_path):
    planner=Planner(FakeClient({"plan":["a","b"],"confidence":.8}))
    plan,_=planner.plan(task=Task(repo_path=str(tmp_path), objective="x"), classification={}, investigation={}, repo_summary=None, candidate_attempts=3, mode="performance", backend_name="b", backend=Backend(name="b", provider="local", model="m", base_url="http://x"), run_dir=tmp_path)
    assert plan.planner_normalized is True
    assert plan.planner_fallback_used is False
    assert (tmp_path/"plan_normalized.json").exists()
    assert json.loads((tmp_path/"plan_normalized.json").read_text())["planner_normalized"] is True


def test_strict_and_fallback_write_plan_normalized(tmp_path):
    strict={"summary":"ok","strategy":"parallel_candidates","should_decompose":False,"candidate_attempts":3}
    Planner(FakeClient(strict)).plan(task=Task(repo_path=str(tmp_path), objective="x"), classification={}, investigation={}, repo_summary=None, candidate_attempts=3, mode="performance", backend_name="b", backend=Backend(name="b", provider="local", model="m", base_url="http://x"), run_dir=tmp_path)
    assert json.loads((tmp_path/"plan_normalized.json").read_text())["planner_normalized"] is False
    bad=tmp_path/"bad"; bad.mkdir()
    plan,_=Planner(FakeClient({"confidence":.5})).plan(task=Task(repo_path=str(tmp_path), objective="x"), classification={}, investigation={}, repo_summary=None, candidate_attempts=3, mode="performance", backend_name="b", backend=Backend(name="b", provider="local", model="m", base_url="http://x"), run_dir=bad)
    assert plan.planner_fallback_used is True
    assert json.loads((bad/"plan_normalized.json").read_text())["planner_fallback_used"] is True


def test_progress_output_includes_normalized(capsys):
    ConsoleProgressReporter().node_completed(SimpleNamespace(kind="plan"), {"strategy":"decompose_then_execute","candidate_attempts":3,"should_decompose":True,"planner_normalized":True})
    assert "normalized=true" in capsys.readouterr().out


def test_report_plan_metadata_and_no_bare_fallback_used(tmp_path):
    dec=Decision(run_id="r", accepted=False, plan={"strategy":"parallel_candidates","should_decompose":False,"candidate_attempts":3,"expected_difficulty":"medium","confidence":.6,"planner_normalized":True,"planner_normalization_notes":["Mapped plan"],"planner_fallback_used":False})
    report=write_performance_report(tmp_path, Task(repo_path=str(tmp_path), objective="fix"), None, [], None, dec, 1.0).read_text()
    assert "Planner normalized:" in report
    assert "Planner fallback used:" in report
    assert "Expected difficulty:" in report
    assert "\nFallback used:" not in report


def test_unusable_payload_requires_fallback_signal():
    norm,_=normalize_plan_payload({"confidence":.2}, requested_candidate_attempts=3)
    try:
        PlanResult.model_validate(norm)
    except Exception:
        pass
    else:
        raise AssertionError("unusable payload should not validate")


def test_regression_action_shaped_payload_requests_decompose_node(tmp_path, monkeypatch):
    from villani_ops.orchestration.engine import OrchestrationEngine
    from villani_ops.execution_policies import policy_for_mode
    from villani_ops.runners.base import RunnerResult
    import subprocess, os
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init"],cwd=repo,check=True,capture_output=True, timeout=10); subprocess.run(["git","config","user.email","a@b.c"],cwd=repo,check=True, timeout=10); subprocess.run(["git","config","user.name","A"],cwd=repo,check=True, timeout=10); (repo/"a.txt").write_text("a\n"); subprocess.run(["git","add","."],cwd=repo,check=True, timeout=10); subprocess.run(["git","commit","-m","init"],cwd=repo,check=True,capture_output=True, timeout=10)
    backend=Backend(name="b", provider="local", model="m", base_url="http://x", roles=["coding","review","classification","investigation","selection","policy"])
    raw={"thought":"The task spans checkout, pricing, inventory, orders, and receipts.","command":"ls -R","resulting_state":{"files":["src/signalshop/pricing.py","src/signalshop/inventory.py","src/signalshop/checkout.py","src/signalshop/orders.py","src/signalshop/receipts.py"]}}
    class Client:
        def complete_json(self, backend, system_prompt, user_prompt, schema_name, **kw):
            if schema_name == "PlanResult": return LLMCallResult(parsed_json=raw, raw_text=json.dumps(raw), backend_name="b", model="m")
            return LLMCallResult(parsed_json={"decision":"reject_all","summary":"none","reasons":["none"]}, raw_text='{}', backend_name="b", model="m")
    monkeypatch.setattr('villani_ops.classification.classifier.TaskClassifier.classify', lambda self, task, backends, out_path=None, backend_override=None, **kw: (task.classification or __import__('villani_ops.core.task', fromlist=['TaskClassification']).TaskClassification(difficulty='medium', category='bugfix', risk='medium'), LLMCallResult(parsed_json={}, raw_text='{}', backend_name='b', model='m')))
    inv_cls=__import__('villani_ops.performance.models', fromlist=['InvestigationResult']).InvestigationResult
    monkeypatch.setattr('villani_ops.performance.investigator.Investigator.investigate', lambda self, task, cls, backend_name, backend, run_dir, **kw: (inv_cls(summary='look'), LLMCallResult(parsed_json={}, raw_text='{}', backend_name='b', model='m')))
    monkeypatch.setattr('villani_ops.orchestration.planner.Planner.decompose', lambda self, **kw: (DecompositionResult(should_use_decomposition=True, reason='decomposed', confidence=.8), LLMCallResult(parsed_json={}, raw_text='{}', backend_name='b', model='m')))
    monkeypatch.setattr('villani_ops.review.reviewer.LLMReviewer.review', lambda self, *a, **kw: (__import__('villani_ops.review.reviewer', fromlist=['ReviewResult']).ReviewResult(decision='fail', recommended_action='fail'), LLMCallResult(parsed_json={}, raw_text='{}', backend_name='b', model='m')))
    runner=SimpleNamespace(name='villani-code', run_task=lambda **kw: RunnerResult(exit_code=0, stdout='', stderr='', duration_ms=1))
    engine=OrchestrationEngine(backends={"b":backend}, execution_policy=policy_for_mode("performance"), runner_adapter=runner, llm_client=Client(), workspace=tmp_path/"ws", non_interactive=True)
    res=engine.run(repo=repo, task=Task(repo_path=str(repo), objective="fix checkout"), candidate_attempts=3, classify=True)
    plan=res.decision.plan
    assert plan["strategy"] == "decompose_then_execute"
    assert plan["should_decompose"] is True
    assert plan["candidate_attempts"] == 3
    graph=json.loads((Path(res.run_dir)/"orchestration_graph.json").read_text())
    assert {n["id"]: n for n in graph["nodes"]}["decompose"]["status"] == "succeeded"

from villani_ops.orchestration.planner import repair_plan_against_context


def test_plan_repair_regression_checkout_context_does_not_inject_domain_decomposition():
    classification={"difficulty":"easy","category":"bug_fix","estimated_attempts_needed":3,"likely_files":["src/signalshop/checkout.py","src/signalshop/inventory.py","src/signalshop/orders.py","src/signalshop/pricing.py","src/signalshop/receipts.py"]}
    inv_raw={"summary":"Task spans checkout flow failures.","files_to_modify":classification["likely_files"],"implementation_steps":["Fix pricing","Fix inventory reservation","Fix payment rollback","Fix receipt rendering"]}
    inv_norm,_=normalize_investigation_payload(inv_raw)
    plan=PlanResult(summary="Fix failing checkout tests directly.", strategy="single_task", should_decompose=False, candidate_attempts=1, expected_difficulty="easy", confidence=.85)
    repaired, notes=repair_plan_against_context(plan, requested_candidate_attempts=1, task="Fix the failing tests", success_criteria="Fix the failing tests. The checkout flow must correctly price carts, reserve inventory atomically, create orders with stable IDs, release reservations on payment failure, and render deterministic receipts. Tests pass and diff is minimal.", classification=classification, investigation=inv_norm)
    assert len(inv_norm["relevant_files"]) == 5 and inv_norm["confidence"] >= .70
    assert repaired.strategy == "single_task"
    assert repaired.should_decompose is False
    assert repaired.candidate_attempts == 1
    assert repaired.planner_repaired is False
    assert notes == []


def test_plan_repair_not_for_narrow_task():
    plan=PlanResult(summary="x", strategy="single_task", should_decompose=False, candidate_attempts=2)
    repaired, notes=repair_plan_against_context(plan, requested_candidate_attempts=2, task="fix typo", success_criteria="tests pass", classification={"likely_files":["a.py"],"estimated_attempts_needed":1}, investigation={"relevant_files":["a.py"],"confidence":.9})
    assert not repaired.planner_repaired and not notes
