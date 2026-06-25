from __future__ import annotations
from pathlib import Path
from typing import Any
from villani_ops.core.acceptance import has_non_empty_patch
from villani_ops.orchestration.artifacts import write_text_utf8

def _selection_evidence_lines(candidates: list[dict], selection: Any) -> list[str]:
    selected_id = selection.selected_attempt_id if selection and selection.decision == 'select' else None
    by_id = {c.get('attempt_id'): c for c in candidates}
    reasons = list((selection.reasons if selection else []) or [])
    lines = [f"Selected attempt: {selected_id or 'reject all'}"]
    if selected_id and selected_id in by_id:
        c = by_id[selected_id]
        bits = [f"Attempt {selected_id} was selected"]
        bits.append('because it was acceptance-eligible' if c.get('acceptance_eligible') else 'even though it was not marked acceptance-eligible')
        if c.get('review_decision') or c.get('review_recommended_action') or c.get('review_score') is not None:
            bits.append(f"reviewer returned {c.get('review_decision') or 'unknown'}/{c.get('review_recommended_action') or 'unknown'} with score {c.get('review_score')}")
        if c.get('changed_files'):
            bits.append('changed files: ' + ', '.join(c.get('changed_files') or []))
        if c.get('acceptance_blockers'):
            bits.append('acceptance blockers: ' + '; '.join(c.get('acceptance_blockers') or []))
        else:
            bits.append('it had no acceptance blockers')
        if reasons:
            bits.append('selector reasons: ' + '; '.join(reasons))
        if c.get('review_summary'):
            bits.append('review summary: ' + c.get('review_summary'))
        if c.get('review_issues'):
            bits.append('review issues: ' + '; '.join(c.get('review_issues') or []))
        lines += ['Why selected:', '- ' + '; '.join(bits) + '.']
    else:
        lines += ['Why selected:', '- Detailed winner evidence was not available in artifacts.']
        if reasons:
            lines += [*[f"- Selector reason: {r}" for r in reasons]]
    alt=[]
    rejected=set((selection.rejected_attempts if selection else []) or [])
    for c in candidates:
        aid=c.get('attempt_id')
        if aid == selected_id: continue
        why=[]
        if aid in rejected: why.append('selector rejected it')
        if not c.get('acceptance_eligible'):
            why.append('it was not acceptance-eligible')
        if c.get('acceptance_blockers'):
            why.append('blockers: ' + '; '.join(c.get('acceptance_blockers') or []))
        if c.get('review_decision') and c.get('review_decision') != 'pass':
            why.append(f"review decision was {c.get('review_decision')}")
        if c.get('review_issues'):
            why.append('review issues: ' + '; '.join(c.get('review_issues') or []))
        alt.append(f"- Attempt {aid} was not selected because " + ('; '.join(why) if why else 'selector ranked another attempt higher') + '.')
    if not alt and not selected_id:
        alt.append('- No candidate was selected; reject-all handled without winner evidence.')
    lines += ['Why alternatives were not selected:', *alt]
    lines += [f"Selector fallback used: {getattr(selection, 'selector_fallback_used', getattr(selection, 'fallback_used', False)) if selection else False}", f"Selector fallback reason: {getattr(selection, 'selector_fallback_reason', getattr(selection, 'fallback_reason', '')) or ''}", f"Selector backend: {getattr(selection, 'selector_backend', None) or ''}", f"Selector confidence: {getattr(selection, 'confidence', '') if selection else ''}"]
    return lines

def write_performance_report(run_dir: str|Path, task: Any, investigation: Any, candidates: list[dict], selection: Any, decision: Any, duration: float, mode: str = 'performance', runner: str = 'villani-code', graph: Any = None, selected_backend_per_node: dict | None = None, routing_decisions: dict | None = None) -> Path:
    p=Path(run_dir)/'report.md'
    p.parent.mkdir(parents=True, exist_ok=True)
    lines=['# Villani Ops Run Report','', '## Task', f"Objective: {task.objective or task.instruction or ''}", f"Success criteria: {task.success_criteria or ''}", '', '## Mode', f"mode: {mode}", f"runner: {runner}", '', '## Orchestration Graph']
    if graph:
        lines += [f"run_id: {graph.run_id}", f"nodes: {len(graph.nodes)}", '', '| Node id | Kind | Status | Backend | Model | Difficulty | Risk | Confidence | Artifacts |', '| --- | --- | --- | --- | --- | --- | --- | ---: | --- |']
        for n in graph.nodes:
            lines.append(f"| {n.id} | {n.kind} | {n.status} | {n.assigned_backend or ''} | {n.assigned_model or ''} | {n.difficulty} | {n.risk} | {n.confidence if n.confidence is not None else ''} | {', '.join((n.artifacts or {}).keys())} |")
    lines += ['', '## Execution Policy']
    if mode == 'performance':
        lines.append('Performance mode used the most capable enabled backend for every node.')
    else:
        lines += ['Backend routing decisions, confidence signals, and escalations:', *[f"- {k}: {v.get('backend_name')} — {v.get('reason')}" for k,v in (routing_decisions or {}).items()]]
    lines += ['', '## Classification', 'See classification.json if classification was enabled; otherwise skipped.', '', '## Investigation']
    if investigation:
        lines += [f"Summary: {investigation.summary}", f"Suspected root cause: {investigation.suspected_root_cause or ''}", 'Relevant files: '+', '.join(investigation.relevant_files), 'Relevant tests: '+', '.join(investigation.relevant_tests), 'Implementation plan: '+'; '.join(investigation.implementation_plan), f"Investigation normalized: {str(getattr(investigation, 'investigation_normalized', False)).lower()}", 'Investigation normalization notes:', *[f"- {x}" for x in getattr(investigation, 'investigation_normalization_notes', [])], f"Investigation fallback used: {str(getattr(investigation, 'investigation_fallback_used', False)).lower()}", f"Investigation fallback reason: {getattr(investigation, 'investigation_fallback_reason', '') or ''}", 'Risks:', *[f"- {x}" for x in investigation.risks], f"Confidence: {investigation.confidence}"]
    plan=decision.plan or {}; dec=decision.decomposition or {}
    lines += ['', '## Plan', f"Planner normalized: {str(plan.get('planner_normalized', False)).lower()}", 'Planner normalization notes:', *[f'- {x}' for x in plan.get('planner_normalization_notes', [])], f"Planner repaired: {str(plan.get('planner_repaired', False)).lower()}", 'Planner repair notes:', *[f'- {x}' for x in plan.get('planner_repair_notes', [])], f"Planner fallback used: {str(plan.get('planner_fallback_used', plan.get('fallback_used', False))).lower()}", f"Planner fallback reason: {plan.get('planner_fallback_reason', '') or ''}", f"Strategy: {plan.get('strategy','')}", f"Should decompose: {plan.get('should_decompose', False)}", f"Candidate attempts: {plan.get('candidate_attempts', decision.candidate_attempts_requested)}", f"Expected difficulty: {plan.get('expected_difficulty','')}", f"Confidence: {plan.get('confidence','')}", 'Risks:', *[f"- {x}" for x in plan.get('risks', [])], '', '## Decomposition']
    if dec:
        subtasks=dec.get('subtasks', []) or []
        lines += [f"Decomposition normalized: {str(dec.get('decomposition_normalized', dec.get('planner_normalized', False))).lower()}", f"Decomposition fallback used: {str(dec.get('decomposition_fallback_used', dec.get('planner_fallback_used', dec.get('fallback_used', False)))).lower()}", f"Decomposition fallback reason: {dec.get('decomposition_fallback_reason', dec.get('planner_fallback_reason', '')) or ''}", f"Subtask count: {len(subtasks)}", f"Reason: {dec.get('reason','')}", f"Merge strategy: {dec.get('merge_strategy') or ''}", f"Advisory only: {dec.get('advisory_only', True)}"]
        for st in subtasks:
            files=', '.join(st.get('relevant_files') or [])
            lines.append(f"- {st.get('id')}: {st.get('title') or st.get('objective')}" + (f" Files: {files}" if files else ''))
        if getattr(decision, 'decomposition_advisory_only', False):
            lines += ['', 'Subtasks were generated but not executed separately. This was advisory-only and does not count as active decomposition.']
    else: lines.append('No decomposition was used.')
    px=getattr(decision,'parallel_execution',None) or {}
    lines += ['', '## Decomposed Execution', f"Decomposition executed: {str(getattr(decision,'decomposition_executed',False)).lower()}", f"Advisory only: {str(getattr(decision,'decomposition_advisory_only',False)).lower()}", f"Subtask count: {getattr(decision,'subtask_count',0)}", f"Parallel subtask execution: {'enabled' if px.get('enabled') else 'disabled'}"]
    if px.get('backend_limits'):
        lines += ['Backend limits:', *[f"- {name}: max_parallel={limit}" for name, limit in (px.get('backend_limits') or {}).items()], f"Max observed concurrency: {px.get('max_observed_concurrency',0)}"]
    elif px.get('reason'):
        lines.append(f"Reason: {px.get('reason')}")
    if getattr(decision,'decomposition_executed',False):
        attempts=getattr(decision,'attempts',[]) or []
        lines += ['', '### Subtasks', '| Subtask | Status | Started | Completed | Changed files | Review | Accepted | Patch |', '|---|---|---|---|---:|---|---|---|']
        accepted=set(getattr(decision,'subtasks_accepted',[]) or [])
        for a in attempts:
            if not a.get('subtask_id'): continue
            rv=a.get('review') or {}
            lines.append(f"| {a.get('subtask_id')} | {a.get('status')} | {a.get('started_at') or ''} | {a.get('completed_at') or ''} | {len(a.get('changed_files') or [])} | {rv.get('decision')}/{rv.get('recommended_action')} | {str(a.get('subtask_id') in accepted).lower()} | {a.get('patch_path') or ''} |")
        val=getattr(decision,'integration_validation',None) or {}
        init=getattr(decision,'integration_validation_initial',None) or {}
        after=getattr(decision,'integration_validation_after_repair',None)
        scope=getattr(decision,'integration_scope_analysis',None) or {}
        lines += ['', '### Scope Analysis', f"Accepted: {scope.get('summary',{}).get('accepted','')}", f"Skipped for overreach: {scope.get('summary',{}).get('skipped_for_overreach','')}", f"Overlapping files: {len(scope.get('overlapping_files') or {})}"]
        for row in scope.get('subtasks',[]) or []:
            lines.append(f"- {row.get('subtask_id')}: overreach={row.get('scope_overreach')} unexpected={', '.join(row.get('unexpected_files') or [])} decision={row.get('integration_decision')} risk={row.get('integration_risk')}")
        lines += ['', '### Integration Validation', f"Worktree: {getattr(decision,'integration_worktree_path',None) or ''}", f"Accepted subtask patches: {len(getattr(decision,'subtasks_accepted',[]) or [])}", f"Initial validation: passed={str(bool(init.get('passed'))).lower()} exit_code={init.get('exit_code','')} command={' '.join(init.get('command') or [])}", f"Repair used: {str(bool(getattr(decision,'integration_repair_used',False))).lower()}", f"Post-repair validation: {('not run' if after is None else 'passed='+str(bool(after.get('passed'))).lower()+' exit_code='+str(after.get('exit_code',''))+' command='+' '.join(after.get('command') or []))}", f"Latest validation passed: {str(bool(val.get('passed'))).lower()}", f"Validation passed: {str(bool(val.get('passed'))).lower()}", f"Final patch: {getattr(decision,'integration_patch_path',None) or ''}"]
        fr=getattr(decision,'final_review',None) or {}
        lines += ['', '### Final Review', f"Decision: {fr.get('decision','')}", f"Recommended action: {fr.get('recommended_action','')}", f"Score: {fr.get('score','')}"]
    lines += ['', '## Candidate Attempts', '| Attempt | Backend | Model | Status | Exit | Changed files | Review decision | Review score | Eligible | Blockers | Patch | Patch path |', '| --- | --- | --- | --- | ---: | --- | --- | ---: | --- | --- | --- | --- |']
    for c in candidates:
        lines.append(f"| {c.get('attempt_id')} | {c.get('backend_name')} | {c.get('model')} | {c.get('status')} | {c.get('exit_code')} | {', '.join(c.get('changed_files') or [])} | {c.get('review_decision')} | {c.get('review_score')} | {c.get('acceptance_eligible')} | {'; '.join(c.get('acceptance_blockers') or [])} | {'yes' if has_non_empty_patch(c.get('patch_path')) else 'no'} | {c.get('patch_path') or ''} |")
    lines += ['', '## Candidate Reviews']
    for c in candidates:
        lines += [f"### {c.get('attempt_id')}", c.get('review_summary') or '', 'Evidence:', *[f"- {x}" for x in (c.get('review_evidence') or [])], 'Issues:', *[f"- {x}" for x in (c.get('review_issues') or [])], f"Recommended action: {c.get('review_recommended_action')}"]
    norm_notes=[]
    steps=getattr(decision, 'controller_steps', []) or []
    for st in steps:
        if st.get('action') in {'selector_normalized','selector_fallback_used'}: norm_notes.append(st.get('summary') or '')
    lines += ['', '## Selection', f"Selector summary: {selection.summary if selection else ''}", 'Raw selector output:', *([f"- {x}" for x in norm_notes if x] or ['- No selector normalization or fallback was recorded.']), f"Selector normalized: {str(getattr(selection, 'selector_normalized', False) if selection else False).lower()}", f"Selector reason synthesized: {str(getattr(selection, 'selector_reason_synthesized', False) if selection else False).lower()}", f"Selector fallback used: {str(getattr(selection, 'selector_fallback_used', getattr(selection, 'fallback_used', False)) if selection else False).lower()}", f"Selector fallback reason: {getattr(selection, 'selector_fallback_reason', getattr(selection, 'fallback_reason', '')) or ''}", 'Reasons:', *[f"- {x}" for x in ((selection.reasons if selection else []) or [])], *_selection_evidence_lines(candidates, selection), '', '## Final Decision', f"Accepted: {decision.accepted}", f"Winner: {decision.winning_attempt_id or ''}", f"Failure reason: {decision.failure_reason}", 'Acceptance blockers:', *[f"- {x}" for x in decision.acceptance_blockers], '', '## Controller Steps', f"Path: {Path(run_dir)/'controller_steps.jsonl'}", f"Recorded steps: {sum(1 for _ in open(Path(run_dir)/'controller_steps.jsonl')) if (Path(run_dir)/'controller_steps.jsonl').exists() else 0}", '', '## Artifacts', f"Graph: {decision.orchestration_graph_path}", f"Decision: {Path(run_dir)/'decision.json'}", f"Selection input: {Path(run_dir)/'selection_input.json'}", *[f"- attempt {c.get('attempt_id')}: {Path(run_dir)/'attempts'/str(c.get('attempt_id'))}" for c in candidates], '', '## Next Commands']
    if decision.accepted:
        lines += [f"villani-ops apply {decision.run_id}", f"villani-ops branch {decision.run_id} --name villani-ops/{decision.run_id}", f"villani-ops pr {decision.run_id} --title \"{(task.objective or 'Villani Ops changes')[:60]}\""]
    else: lines.append('No apply/branch/PR commands because no candidate was accepted.')
    lines += ['', '## Warnings', *[f"- {x}" for x in decision.warnings], f"Duration seconds: {duration:.1f}"]
    write_text_utf8(p, '\n'.join(lines)+'\n')
    return p
