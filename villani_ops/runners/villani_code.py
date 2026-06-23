from __future__ import annotations
import os, subprocess, shutil, json
from pathlib import Path
from .base import RunnerContext, RunnerResult

class VillaniCodeRunner:
    name='villani_code'
    def build_prompt(self, c: RunnerContext) -> str:
        return f"""Objective:\n{c.task_instruction}\n\nSuccess criteria:\n{c.success_criteria or 'Not provided'}\n\nAttempt: {c.attempt_id}\nWork only in repo: {c.repo_path}\n"""
    def run(self, context: RunnerContext) -> RunnerResult:
        command_name=context.backend.command_name or 'villani-code'
        if shutil.which(command_name) is None:
            return RunnerResult(exit_code=127, stderr=f"Villani Code command '{command_name}' was not found.")
        api_key=context.backend.resolved_api_key()
        if not api_key:
            if context.backend.provider=='local' and context.backend.metadata.get('allow_dummy_api_key'):
                api_key='dummy'
            else:
                return RunnerResult(exit_code=2, stderr=f"Backend '{context.backend.name}' has no resolved API key.")
        prompt=self.build_prompt(context); max_tokens=str(context.backend.max_tokens or 50000)
        cmd=[command_name,'run',prompt,'--base-url',context.backend.base_url or '', '--model',context.backend.model,'--repo',context.repo_path,'--provider',context.backend.provider,'--api-key',api_key,'--auto-approve','--no-stream','--max-tokens',max_tokens]
        red=[('***REDACTED***' if x==api_key else x) for x in cmd]
        Path(context.run_dir,'villani_code_command.json').write_text(json.dumps(red, indent=2))
        try:
            p=subprocess.run(cmd, cwd=context.repo_path, text=True, capture_output=True, timeout=context.timeout_seconds, env={**os.environ, **context.env})
            return RunnerResult(exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr)
        except subprocess.TimeoutExpired as e:
            return RunnerResult(exit_code=124, stdout=e.stdout or '', stderr=(e.stderr or '')+f"\nCommand timed out after {context.timeout_seconds}s")
