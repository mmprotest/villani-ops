from __future__ import annotations
from pathlib import Path
from typing import Any
import json
from villani_ops.core.durable_io import durable_write_text, durable_write_json


def write_text_utf8(path: Path, text: str, *, atomic: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if atomic:
        durable_write_text(path, text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")


def read_text_utf8(path: Path, default: str | None = None) -> str:
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        if default is not None:
            return default
        raise


def write_json_utf8(path: Path, data: Any, *, atomic: bool = False, indent: int = 2) -> None:
    if atomic:
        durable_write_json(Path(path), data, indent=indent)
    else:
        text = json.dumps(data, indent=indent, ensure_ascii=False, default=str)
        write_text_utf8(Path(path), text, atomic=False)


def read_json_utf8(path: Path) -> Any:
    return json.loads(read_text_utf8(Path(path)))


def write_transcript(run_dir: Path, transcript: list[dict]):
    write_json_utf8(Path(run_dir) / 'transcript.json', transcript, atomic=True)


def derive_graph(state, events: list[dict]) -> dict:
    nodes=[{'id':'run','type':'run','behavioural_oracle_count':len(getattr(state,'behavioural_oracles',[]) or []),'task_action_contract_count':len(getattr(state,'task_action_contracts',[]) or []),'behavioural_probe_results_count':len(getattr(state,'behavioural_probe_results',[]) or []),'status':state.status,'concurrency_mode':getattr(state,'concurrency_mode',None),'max_parallel':getattr(state,'max_parallel',None),'execution_concurrency':getattr(state,'execution_concurrency',{}),'candidate_concurrency':getattr(state,'candidate_concurrency',{}),'candidate_execution_mode':getattr(state,'candidate_execution_mode','unknown'),'attempts_requested':getattr(state,'attempts_requested',None),'attempts_started':getattr(state,'attempts_started',0),'stopped_early':getattr(state,'stopped_early',False),'stop_reason':getattr(state,'stop_reason',None),'subtask_concurrency':getattr(state,'subtask_concurrency',{})},{'id':'investigation','type':'investigation','present':state.investigation is not None},{'id':'plan','type':'plan','present':state.plan is not None}]
    if state.decomposition: nodes.append({'id':'decomposition','type':'decomposition','accepted':state.decomposition_accepted,'validated':state.decomposition_validated,'executed':state.decomposition_executed,'fallback_used':state.decomposition_fallback_used,'decomposed_execution_status':state.decomposed_execution_status,'failed_subtasks':state.decomposed_execution_failed_subtasks,'blocked_subtasks':state.decomposed_execution_blocked_subtasks})
    for c in state.candidates: nodes.append({'id':c.attempt_id,'type':c.scope,'status':('accepted' if c.status=='accepted' and c.acceptance_eligible else c.status),'runner_failed':bool(c.failure_reason or (c.exit_code is not None and c.exit_code!=0)),'review_passed_but_blocked':bool(c.review and c.review.get('decision')=='pass' and not c.acceptance_eligible),'validation_failed':bool((c.validation or {}).get('passed') is False),'deletion_only_patch':bool(c.deleted_files and not (c.added_files or c.modified_files or c.renamed_files)),'changed_files':c.changed_files,'deleted_files':c.deleted_files,'acceptance_eligible':c.acceptance_eligible,'acceptance_blockers':c.acceptance_blockers})
    for s in state.subtasks: nodes.append({'id':s.subtask_id,'type':'subtask','status':s.status,'dependencies':s.dependencies,'accepted_attempt_id':s.accepted_attempt_id,'blocked':s.status=='skipped'})
    for i,w in enumerate((getattr(state,'subtask_concurrency',{}) or {}).get('waves') or [],1): nodes.append({'id':f'subtask_wave_{i}','type':'subtask_wave',**w})
    if getattr(state,'execution_path',None)!='single_task':
        for i in range(1,((getattr(state,'candidate_concurrency',{}) or {}).get('batch_count') or 0)+1):
            nodes.append({'id':f'candidate_batch_{i}','type':'candidate_batch','max_parallel':getattr(state,'max_parallel',None)})
    if state.decomposed_execution_status in {'blocked','failed'}: nodes.append({'id':'decomposition_deadlock','type':'deadlock','status':state.decomposed_execution_status,'failed_subtasks':state.decomposed_execution_failed_subtasks,'blocked_subtasks':state.decomposed_execution_blocked_subtasks,'blockers':state.decomposed_execution_blockers})
    if state.fallback_used: nodes.append({'id':'candidate_fallback','type':'fallback','from':'decomposed_subtasks','to':state.fallback_execution_path,'reason':state.fallback_reason})
    if state.integration: nodes.append({'id':'integration','type':'integration','status':state.integration.get('status'),'integration_unsupported':state.integration.get('failure_reason')=='agentic_subtask_integration_not_implemented','integration_failed':state.integration.get('status')=='failed','completed_but_unreviewed':state.integration.get('status')=='completed' and not state.integration.get('review'),'failure_reason':state.integration.get('failure_reason'),'merge_conflicts':state.integration.get('merge_conflicts') or [],'conflict_artifacts':state.integration.get('conflict_artifacts') or [],'applied_subtasks':state.integration.get('applied_subtasks') or [],'failed_subtasks':state.integration.get('failed_subtasks') or [],'acceptance_eligible':state.integration.get('acceptance_eligible'),'acceptance_blockers':state.integration.get('acceptance_blockers') or []})

    for i,o in enumerate(getattr(state,'behavioural_oracles',[]) or [],1):
        nodes.append({'id':f'behavioural_oracle_{i}','type':'behavioural_oracle','scope':o.get('scope'),'subtask_id':o.get('subtask_id'),'critical_requirements':[r for r in o.get('requirements',[]) if r.get('priority') in {'critical','high'}],'edge_cases':o.get('edge_cases',[]),'validation_probes':o.get('validation_probes',[]),'adversarial_review_checklist':o.get('adversarial_review_checklist',[])})
    for i,o in enumerate(getattr(state,'task_action_contracts',[]) or [],1):
        nodes.append({'id':f'task_action_contract_{i}','type':'task_action_contract','scope':o.get('scope'),'subtask_id':o.get('subtask_id'),'action_type':o.get('action_type'),'expected_artifacts':o.get('expected_artifacts',[]),'source_grounding_requirements':o.get('source_grounding_requirements',[]),'audit_requirements':o.get('audit_requirements',[]),'validation_implications':o.get('validation_implications',[])})
    nodes += [{'id':'selection','type':'selection','present':state.selection is not None},{'id':'finalization','type':'finalization','present':state.final_decision is not None}]
    return {'canonical':'state.json','derived_from_events':len(events),'nodes':nodes,'edges':[]}


def write_artifacts(run_dir: Path, state, events: list[dict], transcript: list[dict]):
    run_dir=Path(run_dir)
    state.save(run_dir/'state.json')
    write_transcript(run_dir,transcript)
    write_json_utf8(run_dir/'orchestration_graph.json', derive_graph(state,events), atomic=True)
