from villani_ops.performance.selector import resolve_selection, deterministic_fallback


def c(aid, eligible=True, score=1.0, issues=None, files=None, cost=0):
    return {"attempt_id": aid, "acceptance_eligible": eligible, "review_score": score, "review_issues": issues or [], "changed_files": files or ["x.py"], "cost": cost}


def test_selected_candidate_id_maps_to_selected_attempt_id():
    sel, norm, notes = resolve_selection({"selected_candidate_id":"attempt_002"}, [c("attempt_001"), c("attempt_002")])
    assert norm["selected_attempt_id"] == "attempt_002"
    assert sel.selected_attempt_id == "attempt_002" and sel.decision == "select" and not sel.fallback_used


def test_winner_attempt_id_maps_to_selected_attempt_id():
    sel, norm, _ = resolve_selection({"winner_attempt_id":"attempt_001"}, [c("attempt_001")])
    assert sel.selected_attempt_id == "attempt_001"


def test_winner_maps_to_selected_attempt_id():
    sel, norm, _ = resolve_selection({"winner":"attempt_001"}, [c("attempt_001")])
    assert sel.selected_attempt_id == "attempt_001"


def test_reasoning_maps_to_summary_and_reasons():
    sel, norm, _ = resolve_selection({"selected_candidate_id":"attempt_001", "reasoning":"because"}, [c("attempt_001")])
    assert sel.summary == "because"
    assert sel.reasons == ["because"]


def test_missing_decision_becomes_select_when_selected_attempt_exists():
    sel, _, _ = resolve_selection({"selected_attempt_id":"attempt_001"}, [c("attempt_001")])
    assert sel.decision == "select"


def test_reject_all_with_selected_eligible_candidate_becomes_select():
    sel, _, _ = resolve_selection({"decision":"reject_all", "selected_attempt_id":"attempt_001"}, [c("attempt_001")])
    assert sel.decision == "select"


def test_invalid_selected_attempt_id_triggers_fallback():
    sel, _, _ = resolve_selection({"selected_candidate_id":"attempt_999", "reasoning":"Invalid candidate"}, [c("attempt_001"), c("attempt_002", score=.9)])
    assert sel.selected_attempt_id == "attempt_001"
    assert sel.fallback_used
    assert "invalid selected attempt" in sel.fallback_reason


def test_ineligible_selected_attempt_triggers_fallback():
    sel, _, _ = resolve_selection({"selected_attempt_id":"attempt_002"}, [c("attempt_001"), c("attempt_002", eligible=False, score=2)])
    assert sel.selected_attempt_id == "attempt_001"
    assert sel.fallback_used
    assert "ineligible" in sel.fallback_reason


def test_empty_reject_all_with_eligible_candidates_triggers_fallback():
    sel, _, _ = resolve_selection({"decision":"reject_all"}, [c("attempt_001")])
    assert sel.selected_attempt_id == "attempt_001"
    assert sel.fallback_used


def test_no_eligible_candidates_returns_reject_all_clear_reason():
    sel, _, _ = resolve_selection({"selected_attempt_id":"attempt_001"}, [c("attempt_001", eligible=False)])
    assert sel.decision == "reject_all"
    assert sel.summary == "No candidate passed acceptance gates."


def test_fallback_tie_break_score_issues_changed_files_attempt_id():
    assert deterministic_fallback([c("attempt_001", score=.8), c("attempt_002", score=.9)]).selected_attempt_id == "attempt_002"
    assert deterministic_fallback([c("attempt_001", issues=["x"]), c("attempt_002")]).selected_attempt_id == "attempt_002"
    assert deterministic_fallback([c("attempt_001", files=["a.py"]), c("attempt_002", files=["a.py","b.py"])]).selected_attempt_id == "attempt_001"
    assert deterministic_fallback([c("attempt_002"), c("attempt_001")]).selected_attempt_id == "attempt_001"


def test_fallback_does_not_use_cost():
    sel = deterministic_fallback([c("attempt_001", cost=100), c("attempt_002", cost=0, issues=["x"])])
    assert sel.selected_attempt_id == "attempt_001"


def test_live_run_regression_alias_selection_accepts_attempt_002():
    sel, norm, _ = resolve_selection({"selected_candidate_id":"attempt_002", "reasoning":"All three candidates successfully resolve the failing tests."}, [c("attempt_001"), c("attempt_002"), c("attempt_003")])
    assert sel.selected_attempt_id == "attempt_002"
    assert sel.decision == "select"
    assert sel.fallback_used is False


def test_meaningful_reject_all_is_preserved():
    payload={"decision":"reject_all", "summary":"All candidates are risky", "reasons":["Candidate patches modify unrelated files", "Tests did not run"]}
    sel, _, _ = resolve_selection(payload, [c("attempt_001")])
    assert sel.decision == "reject_all"
    assert not sel.fallback_used
from pathlib import Path
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMCallResult
from villani_ops.performance.selector import Selector

class DummyTask:
    def model_dump(self, mode='json'):
        return {"objective":"fix"}

class DummyInv:
    def model_dump(self, mode='json'):
        return {"summary":"look"}

def test_selector_writes_raw_normalized_and_final_artifacts(tmp_path, monkeypatch):
    def complete_json(self, *a, **k):
        return LLMCallResult(parsed_json={"selected_candidate_id":"attempt_002", "reasoning":"All three candidates successfully resolve the failing tests."}, raw_text='{"selected_candidate_id":"attempt_002"}', backend_name='code', model='m')
    monkeypatch.setattr('villani_ops.performance.selector.LLMClient.complete_json', complete_json)
    sel, call, notes = Selector().select(DummyTask(), DummyInv(), [c("attempt_001"), c("attempt_002"), c("attempt_003")], 'code', Backend(name='code', provider='local', model='m'), tmp_path)
    assert sel.selected_attempt_id == 'attempt_002'
    assert not sel.fallback_used
    assert (tmp_path/'selection.raw.txt').read_text() == '{"selected_candidate_id":"attempt_002"}'
    assert 'selected_attempt_id' in (tmp_path/'selection_normalized.json').read_text()
    assert 'fallback_used' in (tmp_path/'selection.json').read_text()


def test_selector_prompt_requires_rationale_and_no_cost_choice():
    from villani_ops.performance.prompts import SELECTOR_USER
    assert 'selected_attempt_id' in SELECTOR_USER
    assert 'Include at least one reason' in SELECTOR_USER or 'reasons' in SELECTOR_USER
    assert 'Do not choose ineligible candidates' in SELECTOR_USER
    assert 'Do not choose based on cost' in SELECTOR_USER


def test_thin_valid_selector_output_synthesizes_reason_from_evidence():
    cand=c('attempt_001') | {'review_decision':'pass','review_recommended_action':'accept','acceptance_blockers':[]}
    sel, _, _=resolve_selection({'selected_attempt_id':'attempt_001'}, [cand])
    assert sel.selected_attempt_id == 'attempt_001'
    assert sel.selector_reason_synthesized is True
    assert 'acceptance-eligible' in sel.reasons[0]
    assert 'reviewer returned pass/accept' in sel.reasons[0]


def test_meaningful_selector_reasons_preserved_no_synthesis():
    sel, _, _=resolve_selection({'selected_attempt_id':'attempt_001','summary':'good','reasons':['specific evidence']}, [c('attempt_001')])
    assert sel.reasons == ['specific evidence']
    assert sel.selector_reason_synthesized is False


def test_selector_fallback_explicit_field_for_ineligible():
    sel, _, _=resolve_selection({'selected_attempt_id':'attempt_002'}, [c('attempt_001'), c('attempt_002', eligible=False)])
    assert sel.selector_fallback_used is True
    assert 'ineligible' in sel.selector_fallback_reason
