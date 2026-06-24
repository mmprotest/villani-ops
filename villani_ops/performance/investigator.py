from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from villani_ops.core.backend import Backend, coding_backends
from villani_ops.core.task import Task
from villani_ops.llm.client import LLMClient, LLMCallResult
from villani_ops.classification.context import collect_relevant_file_snippets, is_skipped_repo_file
from .models import InvestigationResult
from .prompts import INVESTIGATOR_SYSTEM, INVESTIGATOR_USER

def select_investigator_backend(backends: dict[str, Backend]) -> Backend:
    for role in ("investigation", "classification", "review"):
        xs=[b for b in backends.values() if b.enabled and role in b.roles]
        if xs: return sorted(xs, key=lambda b:(-b.capability_score,b.name))[0]
    xs=coding_backends(backends)
    if not xs: raise ValueError("No enabled backend available for investigation")
    return sorted(xs, key=lambda b:(-b.capability_score,b.name))[0]

class Investigator:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def investigate(self, task: Task, classification: Any, backends: dict[str, Backend], run_dir: str|Path) -> tuple[InvestigationResult, LLMCallResult]:
        run_dir=Path(run_dir); backend=select_investigator_backend(backends)
        
        try:
            repo=Path(task.repo_path)
            tree=[str(x.relative_to(repo)).replace('\\','/') for x in repo.rglob('*') if x.is_file() and not is_skipped_repo_file(x.relative_to(repo))][:500]
            snippets=collect_relevant_file_snippets(repo, task.objective or task.instruction or '', tree, getattr(classification, 'relevant_file_paths', None) or [])
            repo_ctx={'tree': tree[:200], 'snippets': [s.__dict__ for s in snippets]}
        except Exception as e: repo_ctx={"error": str(e)}
        ctx={"task": task.model_dump(mode='json'), "classification": classification.model_dump(mode='json') if classification else None, "repo_path": task.repo_path, "repo_context": repo_ctx}
        call=self.client.complete_json(backend, INVESTIGATOR_SYSTEM, INVESTIGATOR_USER.format(context=json.dumps(ctx, indent=2)[:60000]), "InvestigationResult")
        inv=InvestigationResult.model_validate(call.parsed_json); inv.investigator_backend=backend.name
        (run_dir/'investigation.json').write_text(inv.model_dump_json(indent=2)); (run_dir/'investigation.raw.txt').write_text(call.raw_text or '')
        cc=run_dir/'controller_calls'; cc.mkdir(exist_ok=True); (cc/'investigation.json').write_text(call.model_dump_json(indent=2))
        return inv, call
