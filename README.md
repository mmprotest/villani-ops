# Villani Ops

Villani Ops is the control plane for cost-aware AI coding operations. It turns coding agents from expensive one-shot guesses into auditable, cost-optimised local operations.

Villani Ops v0.1 is intentionally boring: a Python package with a clean internal API and a thin CLI. It runs isolated attempts, captures stdout/stderr/diffs, validates attempts with honest deterministic validators, selects a winner, and writes inspectable artifacts to disk.

## What it is not

Villani Ops is **not** a web app, dashboard, hosted job system, HTTP API, auth system, or React UI. Those are intentionally excluded until the local controller proves the core thesis.

## Install editable

```bash
pip install -e .
```

This installs the `villani-ops` console script and exposes the `villani_ops` Python package.

## v0.1 CLI flow

Initialize local file storage:

```bash
villani-ops init
```

Add a local backend configuration. Backend config is local YAML; Villani Ops does not create or host model backends.

```bash
villani-ops backend add local \
  --provider local \
  --model local-test \
  --input-cost 0 \
  --output-cost 0
```

Configure a runner command template. Villani Ops does not hardcode any coding-agent binary. The runner runs inside an isolated copy of the target repository.

```bash
villani-ops runner set shell \
  --command "python -m my_agent --repo {repo} --task-file {task_file}"
```

Create a default policy from configured backends:

```bash
villani-ops policy create-default --name villani-escalation-v1
```

Run a task against a local repository:

```bash
villani-ops run \
  --repo ./some-repo \
  --task "Fix the failing auth tests" \
  --policy .villani-ops/policies/villani-escalation-v1.yaml
```

View the latest report:

```bash
villani-ops report latest
```

You can also invoke the CLI module directly:

```bash
python -m villani_ops.cli.main --help
```

## Python API

```python
from villani_ops import VillaniOps, Task, Policy

ops = VillaniOps.from_workspace(".villani-ops")
policy = Policy.load(".villani-ops/policies/villani-escalation-v1.yaml")
result = ops.run(
    repo="./repo",
    task=Task(
        repo_path="./repo",
        instruction="Fix the failing auth tests",
        success_criteria="Tests pass and diff is minimal",
    ),
    policy=policy,
)
print(result.decision.accepted, result.report_path)
```

## Artifacts

Runs are stored under `.villani-ops/runs/<run_id>/` with task and policy snapshots, per-attempt repositories, stdout/stderr logs, `diff.patch`, validation JSON, decision JSON, CSV, and a Markdown report.

## Validation honesty

`diff_review` is the functional v0.1 validator. It checks that an attempt produced a non-empty diff and records exactly that. `llm_review` exists as a clean extension point but returns a clear error unless a real reviewer backend client is implemented in the future.
