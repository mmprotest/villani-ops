# Villani Ops

Villani Ops is a CLI-only multi-agent orchestration engine for coding tasks. The user gives a task, and Villani Ops decides how to investigate, plan, decompose, sequence, run candidates, review outputs, and select the final result.

The main command is `villani-ops run`.

## Execution modes

- `performance`: use the most capable enabled backend for every node.
- `cheap`: use the same orchestration engine, but route easy/low-risk work to smaller backends when confidence is high.
- `balanced`: conservative cost-aware routing with stronger escalation.
- `quality`: near-performance mode with limited routing for clearly safe subtasks.

```bash
villani-ops run --mode performance
villani-ops run --mode cheap
villani-ops run --mode balanced
villani-ops run --mode quality
```

Default mode is `performance`; default runner is `villani-code`; default candidate attempts is `3`.

## Example

```bash
villani-ops run \
  --repo ./repo \
  --task "Fix the failing auth tests" \
  --success-criteria "Tests pass and diff is minimal" \
  --mode performance \
  --runner villani-code \
  --candidate-attempts 3 \
  --non-interactive
```

## Orchestration lifecycle

A run creates and updates a real orchestration graph with classification (optional), investigation, planning, advisory decomposition when requested, candidate coding nodes, candidate review nodes, selection, verification, and reporting. Graph nodes record status, timing, backend assignment, model assignment, risk/difficulty/confidence signals, summaries, and artifact paths.

Only the `villani-code` runner is implemented today. `claude-code`, `pi`, `aider`, and `codex` are registered adapter stubs and fail clearly as unsupported for execution.

## Legacy compatibility

`villani-ops cost-run` is a legacy compatibility command for the previous cost-policy runner. New work should use `villani-ops run --mode cheap|balanced|quality`.

## Core commands

```bash
villani-ops init
villani-ops backend add strong --provider openai --model gpt-5.5 --capability-score 100 --api-key dummy
villani-ops backend list
villani-ops run --repo ./repo --task "Implement the requested change" --non-interactive
villani-ops apply <run-id>
villani-ops branch <run-id> --name villani-ops/<run-id>
villani-ops pr <run-id> --title "Villani Ops changes"
```

`villani-ops run` rejects legacy primary-path options:

- `--policy` has been replaced by `--mode`.
- `--backend` is not accepted because backend assignment is controlled by execution policy.
- `--human-approval` is not supported in the primary orchestration path.
