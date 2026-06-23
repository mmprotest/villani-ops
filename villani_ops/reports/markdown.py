from pathlib import Path

def write_markdown_report(run_dir, task, policy, attempts, decision, wall_time: float):
    p=Path(run_dir)/"report.md"
    rows=[]
    for a in attempts:
        v=a.validation
        rows.append(f"| {a.attempt_id} | {a.backend_name} | {a.runner_name} | {a.status} | {bool(v and v.passed)} | {v.score if v else ''} | {a.estimated_cost:.6f} | {a.input_tokens}/{a.output_tokens} | {a.diff_path or ''} |")
    warnings="\n".join(f"- {w}" for w in decision.warnings) or "- none"
    p.write_text(f"""# Villani Ops Run Report

Task: {task.instruction}

Result: {'ACCEPTED' if decision.accepted else 'REJECTED'}
Winner: {decision.winning_attempt_id or 'none'}

## Summary

| Metric | Value |
| --- | --- |
| Policy | {policy.name} |
| Total attempts | {decision.total_attempts} |
| Total cost | {decision.total_cost:.6f} |
| Total input tokens | {decision.total_input_tokens} |
| Total output tokens | {decision.total_output_tokens} |
| Total wall time | {wall_time:.2f}s |

## Attempts

| Attempt | Backend | Runner | Status | Valid | Score | Cost | Tokens | Diff |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{chr(10).join(rows)}

## Decision

{decision.reason}

## Warnings

{warnings}

## Artifact Paths

Run directory: {Path(run_dir)}
""")
    return p
