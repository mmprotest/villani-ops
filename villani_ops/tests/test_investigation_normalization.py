
from villani_ops.performance.investigator import normalize_investigation_payload
from villani_ops.performance.models import InvestigationResult


def test_investigation_aliases_and_confidence_preserve_extras():
    raw={"summary":"Task spans checkout flow failures.","files_to_modify":["a.py","b.py","c.py","d.py"],"implementation_steps":["fix pricing"],"test_validation":"pytest","identified_bugs":[{"file":"e.py","issue":"bad tax","fix":"apply coupon first"}]}
    norm, notes=normalize_investigation_payload(raw)
    inv=InvestigationResult.model_validate(norm)
    assert inv.investigation_normalized is True
    assert set(["a.py","b.py","c.py","d.py","e.py"]).issubset(inv.relevant_files)
    assert "fix pricing" in inv.implementation_plan and "apply coupon first" in inv.implementation_plan
    assert inv.relevant_tests == ["pytest"]
    assert "bad tax" in inv.risks
    assert inv.confidence >= .70
    assert inv.raw_findings["files_to_modify"] == raw["files_to_modify"]


def test_useful_raw_signals_raise_confidence_without_summary():
    norm,_=normalize_investigation_payload({"files_to_modify":"a.py","implementation_steps":"fix it"})
    inv=InvestigationResult.model_validate(norm)
    assert inv.confidence >= .65
    assert inv.relevant_files == ["a.py"]
    assert inv.implementation_plan == ["fix it"]


def test_nested_analysis_summary_and_root_causes_map_safely():
    raw={"analysis":{"summary":"The checkout flow spans pricing, inventory, orders, and receipts.","root_causes":[{"file":"src/signalshop/pricing.py","issue":"tax before discount","fix":"apply coupon before tax"}]},"files_to_modify":["src/signalshop/pricing.py","src/signalshop/inventory.py"],"implementation_steps":["Fix pricing","Fix inventory"]}
    norm, _=normalize_investigation_payload(raw)
    assert isinstance(norm["summary"], str)
    assert norm["summary"] == raw["analysis"]["summary"]
    assert norm["summary"] is not raw["analysis"]
    assert "src/signalshop/pricing.py" in norm["relevant_files"]
    assert "src/signalshop/inventory.py" in norm["relevant_files"]
    assert "tax before discount" in norm["risks"]
    assert "apply coupon before tax" in norm["implementation_plan"]
    assert norm["investigation_fallback_used"] is False
    inv=InvestigationResult.model_validate(norm)
    assert inv.summary == raw["analysis"]["summary"]


def test_malformed_summary_with_useful_files_synthesizes_safe_summary():
    norm, _=normalize_investigation_payload({"summary":{"bad":"shape"},"files_to_modify":["a.py"]})
    assert norm["summary"] == "Investigation identified relevant files, risks, or implementation steps for the task."
    assert norm["investigation_fallback_used"] is False
    InvestigationResult.model_validate(norm)
