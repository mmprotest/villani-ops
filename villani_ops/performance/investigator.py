from __future__ import annotations
import json
from pathlib import Path
from villani_ops.orchestration.artifacts import write_text_utf8, write_json_utf8
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

def _merge_unique(base: list[str], extra: list[str]) -> list[str]:
    out=[]
    for item in [*base, *extra]:
        txt=str(item).strip()
        if txt and txt not in out: out.append(txt)
    return out

def _parse_conf(value: Any) -> float:
    try:
        if value is None: return 0.0
        text=str(value).strip()
        n=float(text[:-1])/100 if text.endswith('%') else float(text)
        return max(0.0, min(1.0, n))
    except Exception:
        return 0.0

def normalize_investigation_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    original=dict(payload or {}); data=dict(original); notes=[]
    aliases={
        'analysis':'summary','findings':'summary','diagnosis':'summary','repo_analysis':'summary','investigation':'summary',
        'root_cause':'suspected_root_cause','suspected_cause':'suspected_root_cause','cause':'suspected_root_cause',
        'files':'relevant_files','file_paths':'relevant_files','files_to_modify':'relevant_files','affected_files':'relevant_files','modified_files':'relevant_files','target_files':'relevant_files','relevant_file_paths':'relevant_files',
        'tests':'relevant_tests','test_files':'relevant_tests','test_validation':'relevant_tests','validation_plan':'relevant_tests','tests_to_run':'relevant_tests',
        'plan':'implementation_plan','steps':'implementation_plan','actions':'implementation_plan','implementation_steps':'implementation_plan','fix_steps':'implementation_plan',
        'warnings':'risks','risk_factors':'risks',
    }
    for a,t in aliases.items():
        if a in data:
            before=_as_list(data.get(t)) if t in {'relevant_files','relevant_tests','implementation_plan','risks'} else data.get(t)
            if t in {'relevant_files','relevant_tests','implementation_plan','risks'}:
                data[t]=_merge_unique(_as_list(data.get(t)), _as_list(data[a]))
            elif not str(data.get(t) or '').strip():
                data[t]=data[a]
            if data.get(t) != before: notes.append(f"Mapped {a} to {t}")
    bug_items=[]
    for key in ('identified_bugs','bugs'):
        v=data.get(key)
        if isinstance(v, dict): v=[v]
        if isinstance(v, list): bug_items.extend([x for x in v if isinstance(x, dict)])
    bug_summaries=[]
    if bug_items:
        files=[]; issues=[]; fixes=[]
        for bug in bug_items:
            if bug.get('file'): files.append(str(bug['file']))
            if bug.get('issue'): issues.append(str(bug['issue']))
            if bug.get('fix'): fixes.append(str(bug['fix']))
            if bug.get('issue') or bug.get('fix'):
                bug_summaries.append(': '.join(str(x) for x in [bug.get('file'), bug.get('issue')] if x))
        data['relevant_files']=_merge_unique(_as_list(data.get('relevant_files')), files)
        data['risks']=_merge_unique(_as_list(data.get('risks')), issues)
        data['implementation_plan']=_merge_unique(_as_list(data.get('implementation_plan')), fixes)
        notes.append('Mapped identified_bugs/bugs to relevant_files, risks, and implementation_plan')
    if not str(data.get('summary') or '').strip() and bug_summaries:
        data['summary']='Identified bugs: ' + '; '.join(bug_summaries[:5])
        notes.append('Synthesized summary from identified_bugs')
    if not str(data.get('summary') or '').strip():
        for k,v in data.items():
            if isinstance(v,str) and v.strip() and k not in {'suspected_root_cause'}:
                data['summary']=v.strip(); notes.append(f"Mapped {k} to summary"); break
    if not str(data.get('summary') or '').strip() and str(data.get('suspected_root_cause') or '').strip():
        data['summary']=f"Suspected root cause: {data['suspected_root_cause']}"; notes.append('Derived summary from suspected_root_cause')
    for key in ('implementation_plan','risks','relevant_files','relevant_tests'):
        if isinstance(data.get(key), str): notes.append(f"Converted {key} string to list")
        data[key]=_as_list(data.get(key))
    if not str(data.get('summary') or '').strip() and any(data.get(k) for k in ('relevant_files','implementation_plan','risks','relevant_tests')):
        data['summary']='Investigation identified relevant files, risks, or implementation steps.'; notes.append('Synthesized summary from investigation signals')
    conf=_parse_conf(data.get('confidence'))
    if 'confidence' not in original and any(data.get(k) for k in ('relevant_files','implementation_plan','risks','relevant_tests')):
        conf=max(conf, .65); notes.append('Raised confidence to 0.65 because useful investigation signals were present')
    if str(data.get('summary') or '').strip() and len(data.get('relevant_files') or []) >= 4:
        conf=max(conf, .70); notes.append('Raised confidence to 0.70 because summary and at least four relevant files were present')
    data['confidence']=conf
    extra_keys=['identified_bugs','bugs','files_to_modify','implementation_steps','test_validation','validation_plan','tests_to_run','affected_files','modified_files','target_files']
    extras={k:original[k] for k in extra_keys if k in original}
    if extras: data['raw_findings']=extras
    changed={k:v for k,v in data.items() if original.get(k)!=v}
    data['investigation_normalized']=bool(changed or notes)
    data['investigation_normalization_notes']=notes
    data['investigation_fallback_used']=False
    data['investigation_fallback_reason']=None
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
        write_text_utf8(run_dir/'investigation.raw.txt', call.raw_text or '')
        raw_payload=call.parsed_json if isinstance(call.parsed_json, dict) else {}
        normalized_payload, notes = normalize_investigation_payload(raw_payload)
        try:
            inv=InvestigationResult.model_validate(normalized_payload)
            inv.investigation_normalized=bool(normalized_payload.get('investigation_normalized'))
            inv.investigation_normalization_notes=notes
            inv.investigation_fallback_used=False; inv.investigation_fallback_reason=None
            write_json_utf8(run_dir/'investigation_normalized.json', {'normalized': inv.investigation_normalized, 'payload': normalized_payload, 'notes': notes, 'raw_payload': raw_payload})
        except Exception as original_error:
            reason=str(original_error)
            inv=InvestigationResult(summary=f'Investigation unavailable: {reason}', investigation_fallback_used=True, investigation_fallback_reason=reason)
            write_json_utf8(run_dir/'investigation_normalized.json', {'normalized': False, 'payload': normalized_payload, 'notes': notes, 'raw_payload': raw_payload, 'error': reason})
        inv.investigator_backend=backend_name; inv.assigned_backend={'name': backend_name, 'model': backend.model}
        write_json_utf8(run_dir/'investigation.json', inv)
        cc=run_dir/'controller_calls'; cc.mkdir(exist_ok=True); write_json_utf8(cc/'investigation.json', call)
        return inv, call
