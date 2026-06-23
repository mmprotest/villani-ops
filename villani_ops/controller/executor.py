from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import time, secrets, json, subprocess
from pydantic import BaseModel
from villani_ops.core.task import Task
from villani_ops.core.decision import Decision
from villani_ops.storage.files import FileStorage
from villani_ops.classification import TaskClassifier
from villani_ops.policy_engine import PolicyEngine, ExecutionStrategy
from villani_ops.isolation.worktree import GitWorktreeIsolation, capture_worktree
from villani_ops.runners.base import RunnerContext
from villani_ops.runners.villani_code import VillaniCodeRunner
from villani_ops.review import LLMReviewer, ReviewResult
from villani_ops.reports.markdown import write_markdown_report

class RunResult(BaseModel):
    run_id: str; run_dir: str; decision: Decision; report_path: str; attempts: list[dict]

class VillaniOps:
    def __init__(self, storage: FileStorage): self.storage=storage
    @classmethod
    def from_workspace(cls, path: str | Path = '.villani-ops') -> 'VillaniOps': return cls(FileStorage(path))

    def run(self, repo: str|Path, task: Task, policy: str='balanced', isolation: str='worktree') -> RunResult:
        self.storage.init_workspace(); start=time.time(); repo=Path(repo).resolve(); task.repo_path=str(repo)
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)
        run_dir=self.storage.create_run_dir(run_id); self.storage.save_task(run_dir, task)
        backends=self.storage.load_backends(); warnings=[]; attempts=[]; costs={'classification':0.0,'policy':0.0,'review':0.0,'coding':0.0}; tin=tout=0
        # classify
        cls, cls_call = TaskClassifier().classify(task, backends, run_dir/'classification.json'); task.classification=cls; self.storage.save_task(run_dir, task)
        costs['classification']+=cls_call.estimated_cost; tin+=cls_call.input_tokens; tout+=cls_call.output_tokens
        # policy
        strategy, pol_call = PolicyEngine().generate(cls, backends, policy, run_dir/'strategy.json')
        costs['policy']+=pol_call.estimated_cost; tin+=pol_call.input_tokens; tout+=pol_call.output_tokens
        accepted=None; runner=VillaniCodeRunner()
        for plan in strategy.attempts:
            backend=backends[plan.backend]
            for _ in range(plan.max_attempts):
                attempt_id=f"attempt_{len(attempts)+1:03d}"; adir=run_dir/'attempts'/attempt_id; adir.mkdir(parents=True, exist_ok=True)
                meta={'attempt_id':attempt_id,'backend_name':backend.name,'model':backend.model,'runner_name':'villani_code','status':'running','started_at':datetime.now(timezone.utc).isoformat(),'policy_reason':plan.reason}
                try:
                    if isolation!='worktree': raise ValueError('Only worktree isolation is supported by the v0.2 controller loop')
                    wt=GitWorktreeIsolation().create(repo, run_id, attempt_id, self.storage.workspace); meta.update(wt)
                    result=runner.run(RunnerContext(attempt_id=attempt_id, repo_path=wt['worktree_path'], task_instruction=task.objective or task.instruction or '', success_criteria=task.success_criteria, backend=backend, timeout_seconds=plan.timeout_seconds or backend.timeout_seconds or 1200, run_dir=str(adir)))
                    (adir/'stdout.log').write_text(result.stdout); (adir/'stderr.log').write_text(result.stderr)
                    meta.update({'exit_code':result.exit_code,'stdout_path':str(adir/'stdout.log'),'stderr_path':str(adir/'stderr.log'),'input_tokens':result.input_tokens,'output_tokens':result.output_tokens})
                    cap=capture_worktree(wt['worktree_path'], adir); meta.update(cap)
                    costs['coding']+=backend.estimate_cost(result.input_tokens,result.output_tokens); tin+=result.input_tokens; tout+=result.output_tokens
                    review_input={k:meta.get(k) for k in ['attempt_id','exit_code','stdout_path','stderr_path','changed_files','git_status']}
                    review_input['stdout_summary']=result.stdout[-4000:]; review_input['stderr_summary']=result.stderr[-4000:]; review_input['patch']=(Path(cap['patch_path']).read_text(errors='replace')[:50000])
                    review, rev_call=LLMReviewer().review(task, cls, backend, review_input, backends, adir/'review.json')
                    costs['review']+=rev_call.estimated_cost; tin+=rev_call.input_tokens; tout+=rev_call.output_tokens
                    meta['review']=review.model_dump(mode='json'); meta['review_path']=str(adir/'review.json')
                    # P0 guard: nonzero exit / unhandled error can never be auto-accepted without human override.
                    can_accept=(result.exit_code==0 and review.passed and review.decision=='pass' and review.recommended_action=='accept')
                    meta['status']='validated' if can_accept else ('rejected' if result.exit_code==0 else 'failed')
                    if result.exit_code!=0: meta['error']=result.stderr.strip() or f"Runner exited with {result.exit_code}"
                    if can_accept:
                        accepted=meta; attempts.append(meta); (adir/'attempt.json').write_text(json.dumps(meta, indent=2)); break
                except Exception as e:
                    meta['status']='failed'; meta['error']=str(e); warnings.append(str(e))
                meta['ended_at']=datetime.now(timezone.utc).isoformat(); attempts.append(meta); (adir/'attempt.json').write_text(json.dumps(meta, indent=2))
                rec=((meta.get('review') or {}).get('recommended_action'))
                if rec in {'retry_same_backend'}: continue
                break
            if accepted: break
        apply_opts={}
        if accepted:
            apply_opts={'apply_command':f'villani-ops apply {run_id}','branch_command':f'villani-ops branch {run_id} --name villani-ops/{run_id}','pr_command':f'villani-ops pr {run_id} --title "{(task.objective or "Villani Ops changes")[:60]}"'}
        decision=Decision(run_id=run_id, accepted=bool(accepted), final_action='accept' if accepted else 'fail', winning_attempt_id=accepted.get('attempt_id') if accepted else None, winning_branch=accepted.get('branch_name') if accepted else None, winning_worktree_path=accepted.get('worktree_path') if accepted else None, winning_patch_path=accepted.get('patch_path') if accepted else None, reviewer_decision=(accepted.get('review') or {}).get('decision') if accepted else None, reviewer_score=(accepted.get('review') or {}).get('score') if accepted else None, reviewer_evidence=(accepted.get('review') or {}).get('evidence',[]) if accepted else [], classification=cls.model_dump(mode='json'), execution_strategy=strategy.model_dump(mode='json'), total_cost=sum(costs.values()), coding_cost=costs['coding'], classification_cost=costs['classification'], policy_cost=costs['policy'], review_cost=costs['review'], total_input_tokens=tin, total_output_tokens=tout, attempts=attempts, warnings=warnings, apply_options=apply_opts, reason='Accepted reviewer-passed successful runner attempt.' if accepted else 'No attempt satisfied acceptance gates.', total_attempts=len(attempts))
        self.storage.save_decision(run_dir, decision); report=write_markdown_report(run_dir, task, strategy, attempts, decision, time.time()-start)
        return RunResult(run_id=run_id, run_dir=str(run_dir), decision=decision, report_path=str(report), attempts=attempts)
