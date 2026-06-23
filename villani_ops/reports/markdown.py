from pathlib import Path
import json

def write_markdown_report(run_dir, task, policy_or_strategy, attempts, decision, wall_time: float):
    p=Path(run_dir)/'report.md'
    strategy=decision.execution_strategy or (policy_or_strategy.model_dump(mode='json') if hasattr(policy_or_strategy,'model_dump') else {})
    cls=decision.classification or {}
    rows=[]
    for a in attempts:
        r=a.get('review') or {}; rows.append(f"| {a.get('attempt_id')} | {a.get('backend_name')} | {a.get('model','')} | {a.get('status')} | {a.get('exit_code','')} | {r.get('decision','')} | {r.get('score','')} | {r.get('recommended_action','')} | {(a.get('human_approval') or {}).get('decision','')} | {a.get('acceptance_eligible','')} | {'; '.join(a.get('acceptance_blockers') or [])} | {a.get('controller_action','')} | {a.get('patch_path','')} |")
    evidence='\n'.join(f"- {e}" for e in decision.reviewer_evidence) or '- none'
    warnings='\n'.join(f"- {w}" for w in decision.warnings) or '- none'
    changed='\n'.join(f"- {f}" for a in attempts for f in a.get('changed_files',[])) or '- none'
    apply=decision.apply_options or {}
    p.write_text(f"""# Villani Ops Run Report

## Task

Objective: {task.objective or task.instruction}

Success criteria: {task.success_criteria or 'Not provided'}

## Classification

`{cls.get('difficulty','?')} {cls.get('category','?')} {cls.get('risk','?')}`

```json
{json.dumps(cls, indent=2)}
```

## Policy Strategy

Profile: {strategy.get('profile','')}

{strategy.get('strategy_summary','')}

Strategy warnings: {', '.join(strategy.get('warnings') or []) or 'none'}

```json
{json.dumps(strategy, indent=2)}
```

## Attempts

| Attempt | Backend | Model | Status | Exit | Review | Score | Recommended | Human | Acceptance eligible | Blockers | Controller action | Patch |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
{chr(10).join(rows)}

## Changed Files

{changed}

## Reviewer Evidence

{evidence}

## Controller Decision Steps

```json
{json.dumps(decision.decision_steps, indent=2)}
```

## Final Controller Decision

Result: {'ACCEPTED' if decision.accepted else 'FAILED'}

Final action: {decision.final_action}

Reason: {decision.reason}

Winner: {decision.winning_attempt_id or 'none'}

Cost: ${decision.total_cost:.6f} (classification ${decision.classification_cost:.6f}, policy ${decision.policy_cost:.6f}, coding ${decision.coding_cost:.6f}, review ${decision.review_cost:.6f})

## Accepted Result / Next Commands

Apply:
  {apply.get('apply_command', f'villani-ops apply {decision.run_id}')}

Branch:
  {apply.get('branch_command', f'villani-ops branch {decision.run_id} --name villani-ops/{decision.run_id}')}

PR:
  {apply.get('pr_command', f'villani-ops pr {decision.run_id} --title "..."')}

Branch: {decision.winning_branch or 'none'}
Worktree: {decision.winning_worktree_path or 'none'}
Patch: {decision.winning_patch_path or 'none'}

## Warnings

{warnings}

Run directory: {Path(run_dir)}
Wall time: {wall_time:.2f}s
""")
    return p
