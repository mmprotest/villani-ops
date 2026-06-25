
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
