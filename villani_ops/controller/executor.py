from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import time, secrets
from pydantic import BaseModel
from villani_ops.core import pricing
from villani_ops.core.task import Task
from villani_ops.core.policy import Policy
from villani_ops.core.attempt import Attempt
from villani_ops.core.decision import Decision, select_attempt
from villani_ops.storage.files import FileStorage, capture_diff
from villani_ops.isolation.copy import CopyIsolation
from villani_ops.runners.base import RunnerContext
from villani_ops.runners.shell import ShellRunner
from villani_ops.runners.villani_code import VillaniCodeRunner
from villani_ops.validation.base import DiffReviewValidator, LLMReviewValidator
from villani_ops.reports.markdown import write_markdown_report
from villani_ops.reports.csv_report import write_attempts_csv
from villani_ops.telemetry.events import write_events

class RunResult(BaseModel):
    run_id: str
    run_dir: str
    decision: Decision
    report_path: str
    attempts: list[Attempt]

class VillaniOps:
    def __init__(self, storage: FileStorage): self.storage=storage
    @classmethod
    def from_workspace(cls, path: str | Path = ".villani-ops") -> "VillaniOps":
        return cls(FileStorage(path))
    def run(self, repo: str|Path, task: Task, policy: Policy) -> RunResult:
        self.storage.init_workspace(); start=time.time(); repo=Path(repo).resolve()
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+secrets.token_hex(3)
        run_dir=self.storage.create_run_dir(run_id); self.storage.save_task(run_dir, task); self.storage.save_policy_snapshot(run_dir, policy)
        backends=self.storage.load_backends(); cfg=self.storage.load_config(); attempts=[]; warnings=[]; seen_failures=set(); stop=False
        runners={"shell":ShellRunner(),"villani_code":VillaniCodeRunner()}
        for plan in policy.attempts:
            if stop: break
            backend=backends.get(plan.backend)
            if not backend:
                warnings.append(f"Backend '{plan.backend}' is not configured."); continue
            for _ in range(plan.max_attempts):
                idx=len(attempts)+1; attempt_id=f"attempt_{idx:03d}"; attempt_dir=run_dir/"attempts"/attempt_id; attempt_dir.mkdir(parents=True)
                a=Attempt(attempt_id=attempt_id, run_id=run_id, backend_name=backend.name, runner_name=plan.runner, status="running", started_at=datetime.now(timezone.utc))
                attempts.append(a); repo_copy=CopyIsolation().create(repo, attempt_dir/"repo"); a.isolated_repo_path=str(repo_copy)
                task_file=attempt_dir/"task.md"; task_file.write_text(f"# Task\n\n{task.instruction}\n\n## Success criteria\n\n{task.success_criteria or 'Not provided'}\n\n## Backend\n\n{backend.name} / {backend.model}\n\n## Attempt\n\n{attempt_id}\n")
                command=(cfg.get("runners",{}).get(plan.runner,{}) or {}).get("command")
                runner=runners.get(plan.runner, ShellRunner())
                result=runner.run(RunnerContext(attempt_id=attempt_id, repo_path=str(repo_copy), task_instruction=task.instruction, success_criteria=task.success_criteria, backend=backend, timeout_seconds=plan.timeout_seconds, run_dir=str(attempt_dir), command=command))
                (attempt_dir/"stdout.log").write_text(result.stdout); (attempt_dir/"stderr.log").write_text(result.stderr)
                a.stdout_path=str(attempt_dir/"stdout.log"); a.stderr_path=str(attempt_dir/"stderr.log"); a.input_tokens=result.input_tokens; a.output_tokens=result.output_tokens
                write_events(attempt_dir/"events.jsonl", result.events); a.events_path=str(attempt_dir/"events.jsonl")
                a.estimated_cost=pricing.estimate_cost(a.input_tokens,a.output_tokens,backend)
                if a.input_tokens==0 and a.output_tokens==0: warnings.append("Token counts were unavailable from runner output.")
                diff_path=capture_diff(repo, repo_copy, attempt_dir/"diff.patch"); a.diff_path=str(diff_path)
                if result.exit_code != 0:
                    a.status="failed"; a.error=result.stderr.strip() or f"Runner exited with {result.exit_code}"; warnings.append(a.error)
                validator = DiffReviewValidator() if policy.validation.mode == "diff_review" else LLMReviewValidator()
                a.validation=validator.validate(diff_path, require_test_evidence=policy.validation.require_test_evidence)
                if result.exit_code==0 and a.validation.passed: a.status="validated"
                elif result.exit_code==0: a.status="rejected"
                a.ended_at=datetime.now(timezone.utc); self.storage.save_validation(attempt_dir, a.validation); self.storage.save_attempt(attempt_dir, a)
                fail_sig=a.error or a.validation.summary
                if policy.stopping.stop_on_first_valid and a.validation.passed and result.exit_code==0: stop=True; break
                if policy.stopping.stop_on_repeated_same_failure and fail_sig in seen_failures: stop=True; break
                seen_failures.add(fail_sig)
        decision=select_attempt(run_id, attempts, policy.selection, sorted(set(warnings)))
        self.storage.save_decision(run_dir, decision); report=write_markdown_report(run_dir, task, policy, attempts, decision, time.time()-start); write_attempts_csv(run_dir, attempts)
        return RunResult(run_id=run_id, run_dir=str(run_dir), decision=decision, report_path=str(report), attempts=attempts)
