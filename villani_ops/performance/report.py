from __future__ import annotations
from pathlib import Path
from typing import Any

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
    lines += [f"Fallback used: {getattr(selection, 'fallback_used', False) if selection else False}", f"Fallback reason: {getattr(selection, 'fallback_reason', '') or ''}", f"Selector backend: {getattr(selection, 'selector_backend', None) or ''}", f"Selector confidence: {getattr(selection, 'confidence', '') if selection else ''}"]
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
        lines += [f"Summary: {investigation.summary}", f"Suspected root cause: {investigation.suspected_root_cause or ''}", 'Relevant files: '+', '.join(investigation.relevant_files), 'Relevant tests: '+', '.join(investigation.relevant_tests), 'Risks:', *[f"- {x}" for x in investigation.risks], f"Confidence: {investigation.confidence}"]
    plan=decision.plan or {}; dec=decision.decomposition or {}
    lines += ['', '## Plan', f"Strategy: {plan.get('strategy','')}", f"Should decompose: {plan.get('should_decompose', False)}", f"Candidate attempts: {plan.get('candidate_attempts', decision.candidate_attempts_requested)}", f"Difficulty: {plan.get('expected_difficulty','')}", f"Fallback used: {plan.get('fallback_used', False)}", 'Risks:', *[f"- {x}" for x in plan.get('risks', [])], '', '## Decomposition']
    if dec:
        lines += [f"Reason: {dec.get('reason','')}", f"Merge strategy: {dec.get('merge_strategy') or 'not implemented'}", f"Advisory only: {dec.get('advisory_only', True)}"]
        for st in dec.get('subtasks', []): lines.append(f"- {st.get('id')}: {st.get('title')} — {st.get('objective')}")
    else: lines.append('No decomposition was used.')
    lines += ['', '## Candidate Attempts', '| Attempt | Backend | Model | Status | Exit | Changed files | Review decision | Review score | Eligible | Blockers | Patch path |', '| --- | --- | --- | --- | ---: | --- | --- | ---: | --- | --- | --- |']
    for c in candidates:
        lines.append(f"| {c.get('attempt_id')} | {c.get('backend_name')} | {c.get('model')} | {c.get('status')} | {c.get('exit_code')} | {', '.join(c.get('changed_files') or [])} | {c.get('review_decision')} | {c.get('review_score')} | {c.get('acceptance_eligible')} | {'; '.join(c.get('acceptance_blockers') or [])} | {c.get('patch_path') or ''} |")
    lines += ['', '## Candidate Reviews']
    for c in candidates:
        lines += [f"### {c.get('attempt_id')}", c.get('review_summary') or '', 'Evidence:', *[f"- {x}" for x in (c.get('review_evidence') or [])], 'Issues:', *[f"- {x}" for x in (c.get('review_issues') or [])], f"Recommended action: {c.get('review_recommended_action')}"]
    norm_notes=[]
    steps=getattr(decision, 'controller_steps', []) or []
    for st in steps:
        if st.get('action') in {'selector_normalized','selector_fallback_used'}: norm_notes.append(st.get('summary') or '')
    lines += ['', '## Selection', f"Selector summary: {selection.summary if selection else ''}", 'Raw selector output:', *([f"- {x}" for x in norm_notes if x] or ['- No selector normalization or fallback was recorded.']), 'Fallback:', f"- used: {getattr(selection, 'fallback_used', False) if selection else False}", f"- reason: {getattr(selection, 'fallback_reason', '') or ''}", 'Reasons:', *[f"- {x}" for x in ((selection.reasons if selection else []) or [])], *_selection_evidence_lines(candidates, selection), '', '## Final Decision', f"Accepted: {decision.accepted}", f"Winner: {decision.winning_attempt_id or ''}", f"Failure reason: {decision.failure_reason}", 'Acceptance blockers:', *[f"- {x}" for x in decision.acceptance_blockers], '', '## Controller Steps', f"Path: {Path(run_dir)/'controller_steps.jsonl'}", f"Recorded steps: {sum(1 for _ in open(Path(run_dir)/'controller_steps.jsonl')) if (Path(run_dir)/'controller_steps.jsonl').exists() else 0}", '', '## Artifacts', f"Graph: {decision.orchestration_graph_path}", f"Decision: {Path(run_dir)/'decision.json'}", f"Selection input: {Path(run_dir)/'selection_input.json'}", *[f"- attempt {c.get('attempt_id')}: {Path(run_dir)/'attempts'/str(c.get('attempt_id'))}" for c in candidates], '', '## Next Commands']
    if decision.accepted:
        lines += [f"villani-ops apply {decision.run_id}", f"villani-ops branch {decision.run_id} --name villani-ops/{decision.run_id}", f"villani-ops pr {decision.run_id} --title \"{(task.objective or 'Villani Ops changes')[:60]}\""]
    else: lines.append('No apply/branch/PR commands because no candidate was accepted.')
    lines += ['', '## Warnings', *[f"- {x}" for x in decision.warnings], f"Duration seconds: {duration:.1f}"]
    p.write_text('\n'.join(lines)+'\n')
    return p
