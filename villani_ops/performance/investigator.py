from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from villani_ops.core.backend import Backend
from villani_ops.core.task import Task
from villani_ops.llm.client import LLMClient, LLMCallResult
from villani_ops.classification.context import collect_relevant_file_snippets, is_skipped_repo_file
from .models import InvestigationResult
from .prompts import INVESTIGATOR_SYSTEM, INVESTIGATOR_USER

class Investigator:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def investigate(self, task: Task, classification: Any, backend_name: str, backend: Backend, run_dir: str|Path) -> tuple[InvestigationResult, LLMCallResult]:
        run_dir=Path(run_dir)
        
        try:
            repo=Path(task.repo_path)
            tree=[str(x.relative_to(repo)).replace('\\','/') for x in repo.rglob('*') if x.is_file() and not is_skipped_repo_file(x.relative_to(repo))][:500]
            snippets=collect_relevant_file_snippets(repo, task.objective or task.instruction or '', tree, getattr(classification, 'relevant_file_paths', None) or [])
            repo_ctx={'tree': tree[:200], 'snippets': [s.__dict__ for s in snippets]}
        except Exception as e: repo_ctx={"error": str(e)}
        ctx={"task": task.model_dump(mode='json'), "classification": classification.model_dump(mode='json') if classification else None, "repo_path": task.repo_path, "repo_context": repo_ctx}
        call=self.client.complete_json(backend, INVESTIGATOR_SYSTEM, INVESTIGATOR_USER.format(context=json.dumps(ctx, indent=2)[:60000]), "InvestigationResult")
        inv=InvestigationResult.model_validate(call.parsed_json); inv.investigator_backend=backend_name; inv.performance_backend={'name': backend_name, 'model': backend.model}
        (run_dir/'investigation.json').write_text(inv.model_dump_json(indent=2)); (run_dir/'investigation.raw.txt').write_text(call.raw_text or '')
        cc=run_dir/'controller_calls'; cc.mkdir(exist_ok=True); (cc/'investigation.json').write_text(call.model_dump_json(indent=2))
        return inv, call
