from __future__ import annotations
from pathlib import Path
from typing import Any

def write_performance_report(run_dir: str|Path, task: Any, investigation: Any, candidates: list[dict], selection: Any, decision: Any, duration: float) -> Path:
    p=Path(run_dir)/'report.md'
    lines=['# Villani Ops Performance Run Report','', '## Task', f"Objective: {task.objective or task.instruction or ''}", f"Success criteria: {task.success_criteria or ''}", '', '## Mode', 'performance_orchestration', '', '## Performance Backend', f"performance_backend: {getattr(decision, 'performance_backend_name', '')}/{getattr(decision, 'performance_backend_model', '')}", '', '## Classification', 'See classification.json if present.', '', '## Investigation']
    if investigation:
        lines += [f"Summary: {investigation.summary}", f"Suspected root cause: {investigation.suspected_root_cause or ''}", 'Relevant files: '+', '.join(investigation.relevant_files), 'Relevant tests: '+', '.join(investigation.relevant_tests), 'Implementation plan:', *[f"- {x}" for x in investigation.implementation_plan], 'Risks:', *[f"- {x}" for x in investigation.risks], f"Confidence: {investigation.confidence}"]
    lines += ['', '## Candidate Attempts', '| Attempt | Backend | Model | Status | Exit | Review | Score | Eligible | Blockers | Changed files | Patch |', '| --- | --- | --- | --- | ---: | --- | ---: | --- | --- | --- | --- |']
    for c in candidates:
        lines.append(f"| {c.get('attempt_id')} | {c.get('backend_name')} | {c.get('model')} | {c.get('status')} | {c.get('exit_code')} | {c.get('review_decision')} | {c.get('review_score')} | {c.get('acceptance_eligible')} | {'; '.join(c.get('acceptance_blockers') or [])} | {', '.join(c.get('changed_files') or [])} | {c.get('patch_path') or ''} |")
    lines += ['', '## Candidate Reviews']
    for c in candidates:
        lines += [f"### {c.get('attempt_id')}", c.get('review_summary') or '', 'Issues:', *[f"- {x}" for x in (c.get('review_issues') or [])], f"Recommended action: {c.get('review_recommended_action')}"]
    lines += ['', '## Selection', f"Decision: {selection.decision if selection else 'reject_all'}", f"Selected attempt id: {selection.selected_attempt_id if selection else ''}", f"Summary: {selection.summary if selection else ''}", 'Reasons:', *[f"- {x}" for x in ((selection.reasons if selection else []) or [])], f"Confidence: {selection.confidence if selection else 0}", f"Fallback used: {getattr(selection, 'fallback_used', False) if selection else False}"]
    lines += ['', '## Final Decision', f"Accepted: {decision.accepted}", f"Winner: {decision.winning_attempt_id or ''}", f"Failure reason: {decision.failure_reason}", 'Acceptance blockers:', *[f"- {x}" for x in decision.acceptance_blockers], '', '## Telemetry', f"Input tokens: {decision.total_input_tokens}", f"Output tokens: {decision.total_output_tokens}", f"Duration seconds: {duration:.1f}", '', '## Next Commands']
    if decision.accepted:
        lines += [f"villani-ops apply {decision.run_id}", f"villani-ops branch {decision.run_id} --name villani-ops/{decision.run_id}", f"villani-ops pr {decision.run_id} --title \"{(task.objective or 'Villani Ops changes')[:60]}\""]
    else: lines.append('No apply/branch/PR commands because no candidate was accepted.')
    lines += ['', '## Warnings', *[f"- {x}" for x in decision.warnings]]
    p.write_text('\n'.join(lines)+'\n')
    return p
