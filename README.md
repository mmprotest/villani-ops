# Villani Ops

Villani Ops is a terminal-first orchestration layer for AI coding agents.

It runs multiple independent coding attempts in parallel, captures evidence from each run, compares the candidates, and materializes the best patch with a full audit trail.

## What it does

Villani Ops takes one repository task and runs a candidate tournament:

```text
1. Start from the original task and success criteria.
2. Launch independent Villani Code attempts in isolated worktrees.
3. Run attempts in parallel up to the backend's max_parallel setting.
4. Capture patches, telemetry, debug artifacts, and command evidence.
5. Review and compare candidates.
6. Select the strongest materializable candidate.
7. Apply the selected patch.
8. Write an auditable run report.
```

Candidate generation is intentionally clean: each candidate receives the original task and success criteria, not previous candidate failures, review notes, comparison notes, or hidden-test guesses.

## Install

From the repository root:

```bash
pip install -e .
```

Villani Ops currently expects `villani-code` to be available as the coding runner.

## Quick start

Initialize a workspace:

```bash
villani-ops init
```

Add a backend. This example uses an OpenAI-compatible local endpoint:

```bash
villani-ops backend add qwen35b \
  --provider openai-compatible \
  --base-url http://127.0.0.1:1234/v1 \
  --model villanis/models/qwen3.6-35b-a3b-ud-iq4_xs.gguf \
  --api-key dummy \
  --input-cost 0.14 \
  --output-cost 1.00 \
  --roles coding,classification,review,policy,investigation,selection \
  --capability-score 32 \
  --max-tokens 50000 \
  --max-parallel 4
```

Run a tournament (equivalent one-line form: `villani-ops run --mode performance --repo ./repo --task "Fix the failing tests"`):

```bash
villani-ops run \
  --repo ./repo \
  --task "Fix the failing tests" \
  --success-criteria "Tests pass and the diff is minimal." \
  --mode performance \
  --runner villani-code \
  --candidate-attempts 4 \
  --orchestrator adaptive \
  --non-interactive
```

`--candidate-attempts 4` means Villani Ops will try to run four independent candidate attempts. Parallelism is bounded by the backend's `--max-parallel` value.

## Main command

```bash
villani-ops run \
  --repo <path-to-repo> \
  --task "<task>" \
  --success-criteria "<success criteria>" \
  --mode performance \
  --runner villani-code \
  --candidate-attempts 4 \
  --orchestrator adaptive \
  --non-interactive
```

Recommended demo defaults:

| Option | Value | Purpose |
|---|---:|---|
| `--mode` | `performance` | Use the strongest enabled backend for orchestration and coding roles. |
| `--runner` | `villani-code` | Use Villani Code as the coding runner. |
| `--orchestrator` | `adaptive` | Use the candidate tournament orchestrator. |
| `--candidate-attempts` | `4` | Run multiple independent candidates. |
| `--timeout-seconds` | `1500` | Default run timeout if not explicitly set. |
| backend `--max-parallel` | `4` | Allow parallel candidate execution when capacity exists. |

## Legacy compatibility

The previous cost-policy runner remains available as a legacy compatibility command via `villani-ops cost-run` for older YAML policy workflows. New runs should use `villani-ops run --mode performance`.

## Adaptive tournament mode

`adaptive` is the main path.

In adaptive mode, Villani Ops uses parallel independent candidate generation plus comparative selection:

```text
Candidate generation:
  clean task prompt
  isolated worktree
  no feedback from other candidates

Candidate evaluation:
  evidence packet per candidate
  risk review
  pairwise comparison
  tournament ranking

Finalization:
  selected candidate materialized
  artifacts written to the run directory
```

Adaptive mode does not use decomposition as the primary demo path.

## Run output

Each run writes a directory under:

```text
.villani-ops/runs/<run-id>/
```

Important artifacts include:

```text
state.json
runtime_events.jsonl
cost_summary.json
candidates/<candidate_id>/patch.diff
candidates/<candidate_id>/evidence.json
candidates/<candidate_id>/runner_summary.json
reviews/<candidate_id>.json
comparisons/pairwise.json
comparisons/ranking.json
comparisons/agreement.json
selection.json
final_report.md
viewer/index.html
```

The run directory is the audit trail. It should show what each candidate did, why the winner was selected, what evidence was available, and what risks remained.

## Inspecting results

The CLI prints the run directory at the end of a run:

```text
Run directory: .villani-ops/runs/<run-id>
```

Start with:

```text
final_report.md
selection.json
comparisons/ranking.json
comparisons/pairwise.json
candidates/*/evidence.json
```

These files are the main product surface for now.

## Applying the result

The selected patch is materialized automatically when the run finishes accepted.

You can also use the helper commands exposed by the CLI:

```bash
villani-ops apply <run-id>
villani-ops branch <run-id> --name villani-ops/<run-id>
villani-ops pr <run-id> --title "Villani Ops changes"
```

## Backend roles

For the demo path, one strong backend can handle every role:

```text
coding
classification
review
policy
investigation
selection
```

The important setting for parallel attempts is:

```text
--max-parallel <N>
```

Villani Ops will not exceed the backend's configured parallelism.

## Current limitations

Villani Ops is alpha software.

Known limitations:

- Candidate selection is still experimental.
- If no authoritative validation exists, selection may be best-effort.

The current release is for testing the orchestration loop, candidate tournament, artifact trail, and local-first workflow.
