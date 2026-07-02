from __future__ import annotations
import os, subprocess, shutil, json, signal
from pathlib import Path
from .base import RunnerContext, RunnerResult
from .villani_code_debug import write_runner_telemetry

def provider_for_villani_code_cli(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    mapping = {
        "openai-compatible": "openai",
        "openai_compatible": "openai",
        "openai compatible": "openai",
        "openai": "openai",
        "anthropic": "anthropic",
    }
    return mapping.get(normalized, provider)

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
        cli_provider=provider_for_villani_code_cli(context.backend.provider)
        provider_warning=None
        if (context.backend.provider or "").strip().lower() and cli_provider == context.backend.provider and cli_provider not in {"openai", "anthropic"}:
            provider_warning=f"Unknown Villani Code CLI provider mapping for '{context.backend.provider}'; passing through unchanged."
        debug_dir = Path(context.run_dir) / 'villani_code_debug'
        debug_dir.mkdir(parents=True, exist_ok=True)
        telemetry_path = Path(context.run_dir) / 'runner_telemetry.json'
        prompt_path = Path(context.run_dir) / 'villani_code_prompt.txt'
        prompt_path.write_text(prompt, encoding='utf-8')
        safe_inline_limit = int(os.environ.get('VILLANI_CODE_INLINE_PROMPT_LIMIT', '12000'))
        if len(prompt) > safe_inline_limit:
            cmd=[command_name,'run','--task-file',str(prompt_path),'--base-url',context.backend.base_url or '', '--model',context.backend.model,'--repo',context.repo_path,'--provider',cli_provider,'--api-key',api_key,'--auto-approve','--no-stream','--max-tokens',max_tokens,'--debug','trace','--debug-dir',str(debug_dir)]
        else:
            cmd=[command_name,'run',prompt,'--base-url',context.backend.base_url or '', '--model',context.backend.model,'--repo',context.repo_path,'--provider',cli_provider,'--api-key',api_key,'--auto-approve','--no-stream','--max-tokens',max_tokens,'--debug','trace','--debug-dir',str(debug_dir)]
        red=[('***REDACTED***' if x==api_key else x) for x in cmd]
        Path(context.run_dir,'villani_code_command.json').write_text(json.dumps(red, indent=2))
        def _result(exit_code:int, stdout='', stderr=''):
            tel=write_runner_telemetry(debug_dir, telemetry_path, context.backend)
            warnings=list(tel.token_accounting_warnings)
            telemetry=tel.model_dump(mode='json')
            if provider_warning:
                warnings.append(provider_warning)
                telemetry.setdefault('token_accounting_warnings', []).append(provider_warning)
            return RunnerResult(exit_code=exit_code, stdout=stdout or '', stderr=stderr or '', input_tokens=tel.input_tokens, output_tokens=tel.output_tokens, total_tokens=tel.total_tokens, total_cost=(context.backend.estimate_cost(tel.input_tokens,tel.output_tokens) if tel.token_accounting_status != "missing" else None), debug_artifact_dir=str(debug_dir), resolved_trace_dir=tel.resolved_trace_dir, telemetry_path=str(telemetry_path), duration_ms=tel.duration_ms, model_requests=tel.model_requests, model_failures=tel.model_failures, total_tool_calls=tel.total_tool_calls, tool_calls_by_name=tel.tool_calls_by_name, total_file_reads=tel.total_file_reads, total_file_writes=tel.total_file_writes, commands_executed=tel.commands_executed, commands_failed=tel.commands_failed, first_substantive_file_read_tool_index=tel.first_substantive_file_read_tool_index, first_substantive_file_read_seconds=tel.first_substantive_file_read_seconds, first_file_mutation_tool_index=tel.first_file_mutation_tool_index, first_file_mutation_seconds=tel.first_file_mutation_seconds, first_command_tool_index=tel.first_command_tool_index, first_command_seconds=tel.first_command_seconds, token_accounting_status=tel.token_accounting_status, token_accounting_warnings=warnings, telemetry=telemetry)
        def _norm(x):
            if x is None: return ''
            if isinstance(x, bytes): return x.decode(errors='replace')
            return str(x)
        def _descendants(pid: int) -> set[int]:
            found=set(); stack=[pid]
            while stack:
                parent=stack.pop()
                try:
                    listed=subprocess.run(['pgrep','-P',str(parent)], text=True, capture_output=True, timeout=1)
                    kids=[int(x) for x in listed.stdout.split() if x.strip().isdigit()]
                except Exception:
                    kids=[]
                for child in kids:
                    if child not in found:
                        found.add(child); stack.append(child)
            return found
        def _kill_process_tree(pid: int, sig, known: set[int]|None=None) -> None:
            targets=set(known or set()) | _descendants(pid) | {pid}
            if os.name == 'posix':
                try:
                    listed=subprocess.run(['pgrep','-g',str(pid)], text=True, capture_output=True, timeout=1)
                    targets |= {int(x) for x in listed.stdout.split() if x.strip().isdigit()}
                except Exception:
                    pass
                try: os.killpg(pid, sig)
                except Exception: pass
            for target in sorted(targets, reverse=True):
                try:
                    if target != os.getpid(): os.kill(target, sig)
                except Exception:
                    pass
        proc=None
        try:
            popen_kwargs={'cwd':context.repo_path,'text':True,'stdout':subprocess.PIPE,'stderr':subprocess.PIPE,'env':{**os.environ, **context.env}}
            if os.name == 'posix': popen_kwargs['start_new_session']=True
            proc=subprocess.Popen(cmd, **popen_kwargs)
            stdout, stderr = proc.communicate(timeout=context.timeout_seconds)
            return _result(proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as e:
            if proc and proc.poll() is None:
                known_children=_descendants(proc.pid) if os.name == 'posix' else set()
                try:
                    if os.name == 'posix': _kill_process_tree(proc.pid, signal.SIGTERM, known_children)
                    else: proc.terminate()
                    try: proc.communicate(timeout=1)
                    except Exception: pass
                    if os.name == 'posix':
                        try: _kill_process_tree(proc.pid, signal.SIGKILL, known_children)
                        except ProcessLookupError: pass
                    elif proc.poll() is None: proc.kill()
                    try: proc.communicate(timeout=2)
                    except Exception: pass
                except Exception:
                    try:
                        if os.name == 'posix': _kill_process_tree(proc.pid, signal.SIGKILL, known_children)
                        else: proc.kill()
                    except Exception: pass
                    try: proc.communicate(timeout=2)
                    except Exception: pass
            r=_result(124, _norm(getattr(e,'stdout',None)), _norm(getattr(e,'stderr',None))+f"\nCommand timed out after {context.timeout_seconds}s")
            r.token_accounting_warnings.append('Runner timed out; telemetry may be partial.')
            r.telemetry.setdefault('token_accounting_warnings', []).append('Runner timed out; telemetry may be partial.')
            Path(telemetry_path).write_text(json.dumps(r.telemetry, indent=2))
            return r

class VillaniCodeAdapter(VillaniCodeRunner):
    name='villani-code'
    def run_task(self, *, repo_path: Path, task: str, success_criteria: str | None, backend_name: str, backend_config, timeout_seconds: int | None, context: dict, artifacts_dir: Path) -> RunnerResult:
        return self.run(RunnerContext(attempt_id=str(context.get('attempt_id') or 'attempt'), repo_path=str(repo_path), task_instruction=task, success_criteria=success_criteria, backend=backend_config, timeout_seconds=timeout_seconds or backend_config.timeout_seconds or 1200, run_dir=str(artifacts_dir)))
