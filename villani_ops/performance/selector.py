from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from villani_ops.core.backend import Backend, coding_backends
from villani_ops.llm.client import LLMClient, LLMCallResult
from .models import SelectionResult
from .prompts import SELECTOR_SYSTEM, SELECTOR_USER

def select_selector_backend(backends: dict[str, Backend]) -> Backend:
    for role in ("selection", "review", "classification"):
        xs=[b for b in backends.values() if b.enabled and role in b.roles]
        if xs: return sorted(xs, key=lambda b:(-b.capability_score,b.name))[0]
    xs=coding_backends(backends)
    if not xs: raise ValueError("No enabled backend available for selection")
    return sorted(xs, key=lambda b:(-b.capability_score,b.name))[0]

def deterministic_fallback(candidates: list[dict[str, Any]]) -> SelectionResult:
    eligible=[c for c in candidates if c.get('acceptance_eligible')]
    if not eligible: return SelectionResult(decision='reject_all', summary='No candidate passed acceptance gates.', reasons=['no eligible candidates'], rejected_attempts=[c.get('attempt_id') for c in candidates], fallback_used=True)
    best=sorted(eligible, key=lambda c: (-(c.get('review_score') or 0), len(c.get('review_issues') or []), len(c.get('changed_files') or []), c.get('attempt_id') or ''))[0]
    return SelectionResult(decision='select', selected_attempt_id=best.get('attempt_id'), summary='Deterministic fallback selected highest-scored eligible candidate.', fallback_used=True)

class Selector:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def select(self, task: Any, investigation: Any, candidates: list[dict[str, Any]], backends: dict[str, Backend], run_dir: str|Path) -> tuple[SelectionResult, LLMCallResult|None]:
        run_dir=Path(run_dir)
        if not any(c.get('acceptance_eligible') for c in candidates):
            sel=deterministic_fallback(candidates); (run_dir/'selection.json').write_text(sel.model_dump_json(indent=2)); (run_dir/'selection.raw.txt').write_text(''); return sel, None
        backend=select_selector_backend(backends); ctx={"task": task.model_dump(mode='json'), "investigation": investigation.model_dump(mode='json') if investigation else None, "candidates": candidates}
        try:
            call=self.client.complete_json(backend, SELECTOR_SYSTEM, SELECTOR_USER.format(context=json.dumps(ctx, indent=2)[:90000]), "SelectionResult")
            sel=SelectionResult.model_validate(call.parsed_json); sel.selector_backend=backend.name
            ids={c.get('attempt_id') for c in candidates}; elig={c.get('attempt_id') for c in candidates if c.get('acceptance_eligible')}
            if sel.decision=='select' and (sel.selected_attempt_id not in ids or sel.selected_attempt_id not in elig): raise ValueError('selector chose invalid or ineligible candidate')
        except Exception:
            sel=deterministic_fallback(candidates); call=locals().get('call')
        (run_dir/'selection.json').write_text(sel.model_dump_json(indent=2)); (run_dir/'selection.raw.txt').write_text((call.raw_text if call else '') or '')
        return sel, call if 'call' in locals() else None
