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


def _as_list(value: Any) -> list[str]:
    if value is None: return []
    if isinstance(value, list): return [str(x) for x in value if str(x).strip()]
    if isinstance(value, str): return [value.strip()] if value.strip() else []
    return [str(value)]

def normalize_investigation_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    data=dict(payload or {}); notes=[]
    aliases={'analysis':'summary','findings':'summary','diagnosis':'summary','repo_analysis':'summary','root_cause':'suspected_root_cause','suspected_cause':'suspected_root_cause','files':'relevant_files','relevant_file_paths':'relevant_files','tests':'relevant_tests','test_files':'relevant_tests','plan':'implementation_plan','steps':'implementation_plan','actions':'implementation_plan','warnings':'risks','risk_factors':'risks'}
    for a,t in aliases.items():
        if t not in data and a in data:
            data[t]=data[a]; notes.append(f"Mapped {a} to {t}")
    if not str(data.get('summary') or '').strip():
        for k,v in data.items():
            if isinstance(v,str) and v.strip() and k not in {'suspected_root_cause'}:
                data['summary']=v.strip(); notes.append(f"Mapped {k} to summary"); break
    if not str(data.get('summary') or '').strip(): return data, notes
    for key in ('implementation_plan','risks','relevant_files','relevant_tests'):
        if isinstance(data.get(key), str): notes.append(f"Converted {key} string to list")
        data[key]=_as_list(data.get(key))
    try: conf=float(data.get('confidence',0.0)); data['confidence']=max(0.0,min(1.0,conf))
    except Exception: data['confidence']=0.0
    if 'confidence' not in payload: notes.append('Defaulted confidence to 0.0')
    return data, notes

class Investigator:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def investigate(self, task: Task, classification: Any, backend_name: str, backend: Backend, run_dir: str|Path, estimate_cost: bool=True) -> tuple[InvestigationResult, LLMCallResult]:
        run_dir=Path(run_dir)

        try:
            repo=Path(task.repo_path)
            tree=[str(x.relative_to(repo)).replace('\\','/') for x in repo.rglob('*') if x.is_file() and not is_skipped_repo_file(x.relative_to(repo))][:500]
            snippets=collect_relevant_file_snippets(repo, task.objective or task.instruction or '', tree, getattr(classification, 'relevant_file_paths', None) or [])
            repo_ctx={'tree': tree[:200], 'snippets': [s.__dict__ for s in snippets]}
        except Exception as e: repo_ctx={"error": str(e)}
        ctx={"task": task.model_dump(mode='json'), "classification": classification.model_dump(mode='json') if classification else None, "repo_path": task.repo_path, "repo_context": repo_ctx}

        try:
            call=self.client.complete_json(backend, INVESTIGATOR_SYSTEM, INVESTIGATOR_USER.format(context=json.dumps(ctx, indent=2)[:60000]), "InvestigationResult", estimate_cost=estimate_cost)
        except TypeError:
            call=self.client.complete_json(backend, INVESTIGATOR_SYSTEM, INVESTIGATOR_USER.format(context=json.dumps(ctx, indent=2)[:60000]), "InvestigationResult")
        (run_dir/'investigation.raw.txt').write_text(call.raw_text or '')
        normalized_payload=None; notes=[]
        try:
            inv=InvestigationResult.model_validate(call.parsed_json)
        except Exception:
            normalized_payload, notes = normalize_investigation_payload(call.parsed_json if isinstance(call.parsed_json, dict) else {})
            inv=InvestigationResult.model_validate(normalized_payload)
            inv.investigation_normalized=True; inv.investigation_normalization_notes=notes
        inv.investigator_backend=backend_name; inv.assigned_backend={'name': backend_name, 'model': backend.model}
        if normalized_payload is not None: (run_dir/'investigation_normalized.json').write_text(json.dumps({'payload': normalized_payload, 'notes': notes}, indent=2))
        (run_dir/'investigation.json').write_text(inv.model_dump_json(indent=2))
        cc=run_dir/'controller_calls'; cc.mkdir(exist_ok=True); (cc/'investigation.json').write_text(call.model_dump_json(indent=2))
        return inv, call
