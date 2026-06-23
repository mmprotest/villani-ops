from __future__ import annotations
import os, shlex, subprocess
from pathlib import Path
from .base import RunnerContext, RunnerResult

class ShellRunner:
    name = "shell"
    def run(self, context: RunnerContext) -> RunnerResult:
        if not context.command:
            return RunnerResult(exit_code=2, stderr="Shell runner command is not configured.")
        task_file = Path(context.run_dir) / "task.md"
        vals = {"repo": context.repo_path, "task_file": str(task_file), "attempt_id": context.attempt_id, "run_dir": context.run_dir,
                "backend_name": context.backend.name, "backend_model": context.backend.model, "backend_base_url": context.backend.base_url or ""}
        cmd = context.command.format(**{k: shlex.quote(str(v)) for k, v in vals.items()})
        env = os.environ.copy(); env.update(context.backend.env); env.update(context.env)
        try:
            p = subprocess.run(cmd, shell=True, cwd=context.repo_path, env=env, text=True, capture_output=True, timeout=context.timeout_seconds)
            return RunnerResult(exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr)
        except subprocess.TimeoutExpired as e:
            return RunnerResult(exit_code=124, stdout=e.stdout or "", stderr=(e.stderr or "") + f"\nCommand timed out after {context.timeout_seconds}s")
