from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMClient, LLMCallResult
from .models import SelectionResult
from .prompts import SELECTOR_SYSTEM, SELECTOR_USER

WINNER_ALIASES = ["selected_candidate_id", "selected_candidate", "winner_attempt_id", "winning_attempt_id", "winner", "candidate_id"]
SUMMARY_ALIASES = ["reasoning", "reason", "rationale", "explanation"]


def _meaningful_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def normalize_selector_payload(payload: Any, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    data = dict(payload) if isinstance(payload, dict) else {}
    notes: list[str] = []
    if not data.get("selected_attempt_id"):
        for alias in WINNER_ALIASES:
            if _meaningful_text(data.get(alias)):
                data["selected_attempt_id"] = data[alias].strip()
                notes.append(f"Normalized {alias} to selected_attempt_id={data['selected_attempt_id']}")
                break
    if not _meaningful_text(data.get("summary")):
        for alias in SUMMARY_ALIASES:
            if _meaningful_text(data.get(alias)):
                data["summary"] = data[alias].strip()
                notes.append(f"Normalized {alias} to summary")
                break
    if "reasons" not in data or data.get("reasons") in (None, []):
        for alias in SUMMARY_ALIASES:
            if _meaningful_text(data.get(alias)):
                data["reasons"] = [data[alias].strip()]
                break
    elif isinstance(data.get("reasons"), str):
        data["reasons"] = [data["reasons"].strip()] if data["reasons"].strip() else []
    if not data.get("decision"):
        data["decision"] = "select" if data.get("selected_attempt_id") else "reject_all"
    elif data.get("decision") == "reject_all" and data.get("selected_attempt_id"):
        ids = {c.get("attempt_id") for c in candidates}
        if data.get("selected_attempt_id") in ids:
            data["decision"] = "select"
            notes.append("Normalized reject_all decision with selected_attempt_id to select")
    return data, notes



def synthesize_reason(selected_id: str, candidates: list[dict[str, Any]]) -> str:
    c=next((x for x in candidates if x.get('attempt_id')==selected_id), {})
    bits=[f"Selected {selected_id} because it was acceptance-eligible" if c.get('acceptance_eligible') else f"Selected {selected_id}"]
    if c.get('review_decision') or c.get('review_recommended_action'):
        bits.append(f"reviewer returned {c.get('review_decision') or 'unknown'}/{c.get('review_recommended_action') or 'unknown'}")
    if not c.get('acceptance_blockers'): bits.append('no acceptance blockers were present')
    if c.get('changed_files'): bits.append('changed files were present')
    return ', '.join(bits) + '.'

def deterministic_fallback(candidates: list[dict[str, Any]], reason: str | None = None) -> SelectionResult:
    eligible=[c for c in candidates if c.get('acceptance_eligible')]
    if not eligible:
        return SelectionResult(decision='reject_all', summary='No candidate passed acceptance gates.', reasons=['No candidate passed acceptance gates.'], rejected_attempts=[c.get('attempt_id') for c in candidates], fallback_used=True, fallback_reason=reason or 'No eligible candidates were available for fallback.', selector_fallback_used=True, selector_fallback_reason=reason or 'No eligible candidates were available for fallback.')
    best=sorted(eligible, key=lambda c: (-(c.get('review_score') or 0), len(c.get('review_issues') or []), len(c.get('changed_files') or []), c.get('attempt_id') or ''))[0]
    fallback_reason = reason or 'Selector did not return a valid eligible selected_attempt_id.'
    return SelectionResult(decision='select', selected_attempt_id=best.get('attempt_id'), summary=f'Deterministic fallback selected {best.get("attempt_id")}.', reasons=[fallback_reason], fallback_used=True, fallback_reason=fallback_reason, selector_fallback_used=True, selector_fallback_reason=fallback_reason)


def resolve_selection(payload: Any, candidates: list[dict[str, Any]], *, selector_backend: str | None = None, backend_model: str | None = None) -> tuple[SelectionResult, dict[str, Any], list[str]]:
    normalized, notes = normalize_selector_payload(payload, candidates)
    ids={c.get('attempt_id') for c in candidates}
    elig={c.get('attempt_id') for c in candidates if c.get('acceptance_eligible')}
    def attach(sel: SelectionResult) -> SelectionResult:
        sel.selector_backend=selector_backend
        if selector_backend or backend_model: sel.selector_backend_details={'name': selector_backend or '', 'model': backend_model or ''}
        return sel
    if not elig:
        return attach(SelectionResult(decision='reject_all', summary='No candidate passed acceptance gates.', reasons=['No candidate passed acceptance gates.'], rejected_attempts=list(ids))), normalized, notes
    malformed = not isinstance(payload, dict) or (normalized.get('decision') == 'reject_all' and not _meaningful_text(normalized.get('summary')) and not normalized.get('reasons'))
    try:
        sel=SelectionResult.model_validate(normalized)
    except Exception:
        return attach(deterministic_fallback(candidates, 'Selector output could not be parsed.')), normalized, notes
    if sel.decision == 'select':
        if sel.selected_attempt_id not in ids:
            return attach(deterministic_fallback(candidates, f'Selector returned invalid selected attempt id {sel.selected_attempt_id!r}.')), normalized, notes
        if sel.selected_attempt_id not in elig:
            return attach(deterministic_fallback(candidates, f'Selector selected ineligible candidate {sel.selected_attempt_id}.')), normalized, notes
        if not sel.summary.strip() and not sel.reasons:
            reason=synthesize_reason(sel.selected_attempt_id, candidates)
            sel.summary=reason; sel.reasons=[reason]; sel.selector_reason_synthesized=True
        sel.selector_normalized=bool(notes); sel.selector_normalization_notes=notes; sel.fallback_used=False; sel.fallback_reason=None; sel.selector_fallback_used=False; sel.selector_fallback_reason=None
        return attach(sel), normalized, notes
    if malformed or (not sel.selected_attempt_id and not sel.summary.strip() and not sel.reasons):
        return attach(deterministic_fallback(candidates, 'Selector rejected all without meaningful reasons despite eligible candidates.')), normalized, notes
    sel.summary = sel.summary or 'Selector intentionally rejected all candidates despite eligibility.'
    sel.selector_normalized=bool(notes); sel.selector_normalization_notes=notes; sel.fallback_used=False; sel.fallback_reason=None; sel.selector_fallback_used=False; sel.selector_fallback_reason=None
    return attach(sel), normalized, notes


class Selector:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def select(self, task: Any, investigation: Any, candidates: list[dict[str, Any]], backend_name: str, backend: Backend, run_dir: str|Path, estimate_cost: bool=True) -> tuple[SelectionResult, LLMCallResult|None, list[str]]:
        run_dir=Path(run_dir)
        ctx={"task": task.model_dump(mode='json'), "selector_backend": {"name": backend_name, "model": backend.model}, "investigation": investigation.model_dump(mode='json') if investigation else None, "candidates": candidates}
        (run_dir/'selection_input.json').write_text(json.dumps(ctx, indent=2))
        call=None; raw_payload: Any = {}
        if any(c.get('acceptance_eligible') for c in candidates):
            try:
                try:
                    call=self.client.complete_json(backend, SELECTOR_SYSTEM, SELECTOR_USER.format(context=json.dumps(ctx, indent=2)[:90000]), "SelectionResult", estimate_cost=estimate_cost)
                except TypeError:
                    call=self.client.complete_json(backend, SELECTOR_SYSTEM, SELECTOR_USER.format(context=json.dumps(ctx, indent=2)[:90000]), "SelectionResult")
                raw_payload=call.parsed_json
            except Exception:
                raw_payload={}
        sel, normalized, notes = resolve_selection(raw_payload, candidates, selector_backend=backend_name, backend_model=backend.model)
        (run_dir/'selection.raw.txt').write_text((call.raw_text if call else '') or (json.dumps(raw_payload) if raw_payload else ''))
        (run_dir/'selection_normalized.json').write_text(json.dumps({'payload': normalized, 'notes': notes}, indent=2, default=str))
        (run_dir/'selection.json').write_text(sel.model_dump_json(indent=2))
        return sel, call, notes
