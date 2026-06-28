from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import os, shlex, subprocess

ValidationSource = Literal['user_provided','project_detected','generated','diagnostic']
ValidationConfidence = Literal['high','medium','low']
ValidationStatus = Literal['passed','failed_candidate','infrastructure_error','skipped_no_reliable_command','diagnostic_failed','timeout']

@dataclass(frozen=True)
class ClassifiedValidationCommand:
    command: str
    source: ValidationSource = 'generated'
    confidence: ValidationConfidence = 'low'
    blocking: bool = False
    reason: str = ''
    argv: list[str] | None = None
    shell: bool = False
    timeout_seconds: int = 300

    @classmethod
    def from_legacy(cls, cmd: str, *, source: str | None = None, confidence: str | None = None, blocking: bool | None = None, reason: str = '', timeout_seconds: int | None = None):
        mapped = _map_source(source)
        conf = confidence if confidence in {'high','medium','low'} else ('high' if mapped == 'user_provided' else 'low')
        block = bool(blocking) if blocking is not None else (mapped == 'user_provided' or (mapped == 'project_detected' and conf == 'high'))
        return cls(command=cmd, source=mapped, confidence=conf, blocking=block, reason=reason, timeout_seconds=timeout_seconds or 300)

def _map_source(source: str | None) -> ValidationSource:
    s=(source or '').lower()
    if s in {'user_provided','user_success_criteria','explicit','final','integration'}: return 'user_provided'
    if s in {'project_detected','investigation_discovered','subtask_focused'}: return 'project_detected'
    if s in {'diagnostic','exploratory','runner_trace','villani_code_debug_trace'}: return 'diagnostic'
    return 'generated'

def classify_validation_command(*, cmd: str, source: str | None = None, confidence: str | None = None, blocking: bool | None = None, reason: str = '', timeout_seconds: int | None = None) -> ClassifiedValidationCommand:
    return ClassifiedValidationCommand.from_legacy(cmd, source=source, confidence=confidence, blocking=blocking, reason=reason, timeout_seconds=timeout_seconds)

def command_to_argv(command: str) -> tuple[list[str] | None, str | None]:
    try:
        argv=shlex.split(command, posix=(os.name != 'nt'))
    except ValueError as e:
        return None, f'command_parse_error: {e}'
    if not argv:
        return None, 'empty_command'
    # Commands containing shell control syntax require an explicit shell contract.
    # Check parsed tokens so language snippets such as Python -c "a; b" remain argv-safe.
    shell_tokens={'&&','||','|',';','<','>','>>','2>&1'}
    if any(t in shell_tokens or t.startswith(('>','<')) or t.endswith('>&1') for t in argv):
        return None, 'shell_syntax_requires_explicit_shell_mode'
    if any(x in command for x in ['`','$(', '\n']):
        return None, 'shell_syntax_requires_explicit_shell_mode'
    return argv, None

def run_classified_validation(command: ClassifiedValidationCommand, *, cwd: Path, stdout_path: Path, stderr_path: Path) -> dict:
    stdout_path.parent.mkdir(parents=True, exist_ok=True); stderr_path.parent.mkdir(parents=True, exist_ok=True)
    mode='shell' if command.shell else 'argv'
    argv=command.argv
    infra=None
    if not command.shell and argv is None:
        argv, infra = command_to_argv(command.command)
    if infra:
        stdout_path.write_text('', encoding='utf-8'); stderr_path.write_text(infra+'\n', encoding='utf-8')
        return _result(command, 'infrastructure_error', None, mode, argv or [], str(stdout_path), str(stderr_path), infra)
    try:
        if command.shell:
            # Single centralized shell path. The caller must explicitly request shell mode.
            completed=subprocess.run(command.command, shell=True, cwd=cwd, text=True, capture_output=True, timeout=command.timeout_seconds)
        else:
            completed=subprocess.run(argv or [], shell=False, cwd=cwd, text=True, capture_output=True, timeout=command.timeout_seconds)
        stdout_path.write_text(completed.stdout or '', encoding='utf-8')
        stderr_path.write_text(completed.stderr or '', encoding='utf-8')
        if completed.returncode == 0:
            status='passed'
        elif not command.blocking:
            status='diagnostic_failed'
        else:
            status='failed_candidate'
        return _result(command, status, completed.returncode, mode, argv, str(stdout_path), str(stderr_path), None)
    except subprocess.TimeoutExpired as e:
        stdout_path.write_text((e.stdout or ''), encoding='utf-8'); stderr_path.write_text((e.stderr or '')+'\ntimeout\n', encoding='utf-8')
        return _result(command, 'timeout', None, mode, argv, str(stdout_path), str(stderr_path), 'validation timed out')
    except FileNotFoundError as e:
        stdout_path.write_text('', encoding='utf-8'); stderr_path.write_text(str(e)+'\n', encoding='utf-8')
        return _result(command, 'infrastructure_error', None, mode, argv, str(stdout_path), str(stderr_path), 'executable not found')
    except Exception as e:
        stdout_path.write_text('', encoding='utf-8'); stderr_path.write_text(f'{type(e).__name__}: {e}\n', encoding='utf-8')
        return _result(command, 'infrastructure_error', None, mode, argv, str(stdout_path), str(stderr_path), f'{type(e).__name__}: {e}')

def skipped_validation_result(*, target: str, target_id: str | None, cwd: Path, reason: str = 'no explicit or high-confidence validation command available') -> dict:
    return {'passed': False, 'status': 'skipped_no_reliable_command', 'commands': [], 'target': target, 'target_id': target_id, 'cwd': str(cwd), 'infrastructure_error': None, 'reason': reason}

def _result(c: ClassifiedValidationCommand, status: ValidationStatus, exit_code: int | None, mode: str, argv: list[str] | None, so: str, se: str, infra: str | None) -> dict:
    return {'cmd': c.command, 'command': c.command, 'argv': (argv or []), 'execution_mode': mode, 'shell': mode == 'shell', 'source': c.source, 'confidence': c.confidence, 'blocking': c.blocking, 'authority': 'acceptance_blocking' if c.blocking else 'diagnostic_only', 'reason': c.reason, 'status': status, 'passed': status == 'passed', 'exit_code': exit_code, 'stdout_path': so, 'stderr_path': se, 'infrastructure_error': infra}
