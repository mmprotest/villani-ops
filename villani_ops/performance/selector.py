from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMClient, LLMCallResult
from .models import SelectionResult
from .prompts import SELECTOR_SYSTEM, SELECTOR_USER


def deterministic_fallback(candidates: list[dict[str, Any]]) -> SelectionResult:
    eligible=[c for c in candidates if c.get('acceptance_eligible')]
    if not eligible:
        return SelectionResult(decision='reject_all', summary='No candidate passed acceptance gates.', reasons=['no eligible candidates'], rejected_attempts=[c.get('attempt_id') for c in candidates], fallback_used=True)
    best=sorted(eligible, key=lambda c: (-(c.get('review_score') or 0), len(c.get('review_issues') or []), len(c.get('changed_files') or []), c.get('attempt_id') or ''))[0]
    return SelectionResult(decision='select', selected_attempt_id=best.get('attempt_id'), summary='Deterministic fallback selected highest-scored eligible candidate.', fallback_used=True)


class Selector:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def select(self, task: Any, investigation: Any, candidates: list[dict[str, Any]], backend_name: str, backend: Backend, run_dir: str|Path) -> tuple[SelectionResult, LLMCallResult|None]:
        run_dir=Path(run_dir)
        ctx={"task": task.model_dump(mode='json'), "selector_backend": {"name": backend_name, "model": backend.model}, "investigation": investigation.model_dump(mode='json') if investigation else None, "candidates": candidates}
        (run_dir/'selection_input.json').write_text(json.dumps(ctx, indent=2))
        if not any(c.get('acceptance_eligible') for c in candidates):
            sel=deterministic_fallback(candidates); sel.selector_backend=backend_name; sel.selector_backend_details={'name': backend_name, 'model': backend.model}
            (run_dir/'selection.json').write_text(sel.model_dump_json(indent=2)); (run_dir/'selection.raw.txt').write_text(''); return sel, None
        try:
            call=self.client.complete_json(backend, SELECTOR_SYSTEM, SELECTOR_USER.format(context=json.dumps(ctx, indent=2)[:90000]), "SelectionResult")
            sel=SelectionResult.model_validate(call.parsed_json); sel.selector_backend=backend_name; sel.selector_backend_details={'name': backend_name, 'model': backend.model}
            ids={c.get('attempt_id') for c in candidates}; elig={c.get('attempt_id') for c in candidates if c.get('acceptance_eligible')}
            if sel.decision=='select' and (sel.selected_attempt_id not in ids or sel.selected_attempt_id not in elig): raise ValueError('selector chose invalid or ineligible candidate')
        except Exception:
            sel=deterministic_fallback(candidates); sel.selector_backend=backend_name; sel.selector_backend_details={'name': backend_name, 'model': backend.model}; call=locals().get('call')
        (run_dir/'selection.json').write_text(sel.model_dump_json(indent=2)); (run_dir/'selection.raw.txt').write_text((call.raw_text if call else '') or '')
        return sel, call if 'call' in locals() else None
