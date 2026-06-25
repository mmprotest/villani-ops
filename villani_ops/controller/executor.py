from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import time, secrets
from pydantic import BaseModel
from villani_ops.core.task import Task
from villani_ops.core.decision import Decision
from villani_ops.storage.files import FileStorage
from villani_ops.classification import TaskClassifier
from villani_ops.core.backend import Backend
from villani_ops.execution_policies import policy_for_mode
from villani_ops.isolation.worktree import GitWorktreeIsolation, capture_worktree
from villani_ops.runners.base import RunnerContext
from villani_ops.runners import runner_for_name
from villani_ops.review import LLMReviewer
from villani_ops.core.acceptance import is_attempt_acceptance_eligible
from villani_ops.controller.state_machine import ControllerStateRecorder
from villani_ops.controller.progress import RunProgressReporter
from villani_ops.performance.investigator import Investigator
from villani_ops.performance.selector import Selector
from villani_ops.performance.models import CandidateSummary
from villani_ops.performance.report import write_performance_report
from villani_ops.orchestration.planner import build_fixed_graph
from villani_ops.orchestration.artifacts import write_json
from villani_ops.orchestration.nodes import NodeResult

class RunResult(BaseModel):
    run_id: str; run_dir: str; decision: Decision; report_path: str; attempts: list[dict]

def _candidate_prompt(task: Task, inv, n: int, total: int) -> str:
    return f"""Original objective:\n{task.objective or task.instruction or ''}\n\nSuccess criteria:\n{task.success_criteria or ''}\n\nInvestigation summary:\n{inv.summary if inv else ''}\n\nSuspected root cause:\n{(inv.suspected_root_cause if inv else '') or ''}\n\nRelevant files:\n{', '.join(inv.relevant_files if inv else [])}\n\nRelevant tests:\n{', '.join(inv.relevant_tests if inv else [])}\n\nImplementation plan:\n{chr(10).join('- '+x for x in (inv.implementation_plan if inv else []))}\n\nYou are candidate attempt {n} of {total}.\nWork independently.\nDo not assume the other candidates are correct.\nProduce the smallest correct patch you can.\nRun relevant tests when possible.\nDo not add generated/cache artifacts.\nDo not edit files outside the repository.\n"""


def _tail(path: str | None, limit: int = 8000) -> str:
    if not path:
        return ''
    try:
        return Path(path).read_text(errors='replace')[-limit:]
    except Exception:
        return ''

def _patch_text(path: str | None, limit: int = 60000) -> str:
    if not path:
        return ''
    try:
        text=Path(path).read_text(errors='replace')
        return text[:limit] + ("\n...[truncated]" if len(text) > limit else '')
    except Exception:
        return ''

def _selector_candidate_payload(a: dict) -> dict:
    review=a.get('review') or {}
    return {
        'attempt_id': a.get('attempt_id'),
        'backend_name': a.get('backend_name'),
        'model': a.get('model'),
        'status': a.get('status'),
        'exit_code': a.get('exit_code'),
        'changed_files': a.get('changed_files') or [],
        'git_status': a.get('git_status') or '',
        'patch_text': _patch_text(a.get('patch_path')),
        'stdout_tail': _tail(a.get('stdout_path')),
        'stderr_tail': _tail(a.get('stderr_path')),
        'review': review,
        'review_score': review.get('score'),
        'review_summary': review.get('summary') or '',
        'review_evidence': review.get('evidence') or [],
        'review_issues': review.get('issues') or [],
        'review_recommended_action': review.get('recommended_action') or '',
        'acceptance_eligible': bool(a.get('acceptance_eligible')),
        'acceptance_blockers': a.get('acceptance_blockers') or [],
    }

class VillaniOps:
    def __init__(self, storage: FileStorage, progress_reporter=None):
        self.storage=storage; self.progress_reporter=progress_reporter or RunProgressReporter(False)
    @classmethod
    def from_workspace(cls, path: str | Path = '.villani-ops') -> 'VillaniOps': return cls(FileStorage(Path(path).expanduser().resolve()))

    def run(self, repo: str|Path, task: Task, candidate_attempts: int=3, timeout_seconds: int|None=None, classify: bool=True, non_interactive: bool=False, isolation: str='worktree', mode: str='performance', runner: str='villani-code') -> RunResult:
        if candidate_attempts < 1 or candidate_attempts > 8: raise ValueError('candidate_attempts must be between 1 and 8')
        if mode not in {'performance','cheap','balanced','quality'}: raise ValueError('mode must be one of: performance, cheap, balanced, quality')
        if runner != 'villani-code': raise ValueError(f"Unsupported runner '{runner}'. Supported runner: villani-code.")
        self.storage.init_workspace(); start=time.time(); repo=Path(repo).resolve(); task.repo_path=str(repo)
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)
        run_dir=self.storage.create_run_dir(run_id); self.storage.save_task(run_dir, task)
        progress=self.progress_reporter; progress.info(f'Starting Villani Ops orchestration run (mode={mode})')
        backends=self.storage.load_backends(); warnings=[]; attempts=[]; costs={'classification':0.0,'review':0.0,'coding':0.0,'investigation':0.0,'selection':0.0}; tin=tout=0
        recorder=ControllerStateRecorder(run_id, run_dir); cls=None; inv=None; selection=None
        graph=build_fixed_graph(candidate_attempts, runner=runner)
        policy=policy_for_mode(mode)
        selections={}
        for node in graph.nodes:
            conf = 0.99 if mode == 'performance' else (0.9 if node.kind == 'code' else 0.75)
            sel=policy.select_backend(node=node, backends=backends, task_context=task, confidence=conf, prior_results=[])
            node.assigned_backend=sel.backend_name
            selections[node.id]=sel
        write_json(run_dir/'orchestration_graph.json', graph)
        performance_backend_name=graph.get('investigate').assigned_backend
        performance_backend=backends[performance_backend_name]
        coding_plan=[backends[graph.get(f'code_attempt_{i:03d}').assigned_backend] for i in range(1, candidate_attempts+1)]
        if classify:
            recorder.transition('classify','classifying','Starting task classification.')
            try:
                cls, call=TaskClassifier().classify(task, backends, run_dir/'classification.json', backend_override=performance_backend); task.classification=cls; self.storage.save_task(run_dir, task)
                costs['classification']+=call.estimated_cost; tin+=call.input_tokens; tout+=call.output_tokens
            except Exception as e:
                warnings.append(f'Classification failed: {e}')
        recorder.transition('investigate','planning','Starting investigation.')
        try:
            inv, call=Investigator().investigate(task, cls, performance_backend_name, performance_backend, run_dir); (run_dir/'investigation.json').write_text(inv.model_dump_json(indent=2)); costs['investigation']+=call.estimated_cost; tin+=call.input_tokens; tout+=call.output_tokens
        except Exception as e:
            warnings.append(f'Investigation failed: {e}')
            from villani_ops.performance.models import InvestigationResult
            inv=InvestigationResult(summary=f'Investigation unavailable: {e}')
            (run_dir/'investigation.json').write_text(inv.model_dump_json(indent=2)); (run_dir/'investigation.raw.txt').write_text('')
        runner_adapter=runner_for_name(runner)
        for idx,b in enumerate(coding_plan, start=1):
            aid=f'attempt_{idx:03d}'; adir=run_dir/'attempts'/aid; adir.mkdir(parents=True, exist_ok=True)
            meta={'attempt_id':aid,'backend_name':b.name,'model':b.model,'performance_backend':{'name':performance_backend_name,'model':performance_backend.model},'runner_name':runner,'status':'running','started_at':datetime.now(timezone.utc).isoformat()}
            recorder.transition('run_candidate','attempting','Starting candidate attempt.', aid, {'backend':b.name,'model':b.model})
            try:
                if isolation!='worktree': raise ValueError('Only worktree isolation is supported')
                wt=GitWorktreeIsolation().create(repo, run_id, aid, self.storage.workspace); meta.update(wt)
                res=runner_adapter.run(RunnerContext(attempt_id=aid, repo_path=wt['worktree_path'], task_instruction=_candidate_prompt(task, inv, idx, candidate_attempts), success_criteria=task.success_criteria, backend=b, timeout_seconds=timeout_seconds or b.timeout_seconds or 1200, run_dir=str(adir)))
                (adir/'stdout.log').write_text(res.stdout); (adir/'stderr.log').write_text(res.stderr)
                coding_cost=b.estimate_cost(res.input_tokens,res.output_tokens); costs['coding']+=coding_cost; tin+=res.input_tokens; tout+=res.output_tokens
                meta.update({'exit_code':res.exit_code,'stdout_path':str(adir/'stdout.log'),'stderr_path':str(adir/'stderr.log'),'debug_artifact_dir':res.debug_artifact_dir,'resolved_trace_dir':res.resolved_trace_dir,'telemetry_path':res.telemetry_path,'duration_ms':res.duration_ms,'input_tokens':res.input_tokens,'output_tokens':res.output_tokens,'total_tokens':res.input_tokens+res.output_tokens,'coding_cost':coding_cost,'model_requests':res.model_requests,'model_failures':res.model_failures,'total_tool_calls':res.total_tool_calls,'tool_calls_by_name':res.tool_calls_by_name,'total_file_reads':res.total_file_reads,'total_file_writes':res.total_file_writes,'commands_executed':res.commands_executed,'commands_failed':res.commands_failed,'token_accounting_status':res.token_accounting_status,'runner_telemetry':res.telemetry})
                cap=capture_worktree(wt['worktree_path'], adir); meta.update(cap)
            except Exception as e:
                meta.update({'status':'failed','exit_code':meta.get('exit_code',1),'error':str(e), 'changed_files': [], 'patch_path': None})
                (adir/'stderr.log').write_text(str(e))
            recorder.transition('review_candidate','reviewing','Reviewing candidate attempt.', aid)
            review_input={k:meta.get(k) for k in ['attempt_id','exit_code','stdout_path','stderr_path','changed_files','git_status','runner_telemetry']}
            review_input['stdout_summary']=Path(meta.get('stdout_path','')).read_text(errors='replace')[-4000:] if meta.get('stdout_path') else ''
            review_input['stderr_summary']=Path(meta.get('stderr_path','')).read_text(errors='replace')[-4000:] if meta.get('stderr_path') else ''
            review_input['patch']=(Path(meta['patch_path']).read_text(errors='replace')[:50000] if meta.get('patch_path') else '')
            review_input['investigation']=inv.model_dump(mode='json') if inv else None
            try:
                review_backend=backends[graph.get(f'review_{aid}').assigned_backend]
                review, rcall=LLMReviewer().review(task, cls, review_backend, review_input, backends, adir/'review.json', backend_override=review_backend); costs['review']+=rcall.estimated_cost; tin+=rcall.input_tokens; tout+=rcall.output_tokens
            except Exception as e:
                from villani_ops.review import ReviewResult
                review=ReviewResult(decision='fail', summary=f'Reviewer failed: {e}', issues=[str(e)], recommended_action='fail')
                (adir/'review.json').write_text(review.model_dump_json(indent=2))
            meta['review']=review.model_dump(mode='json')
            meta['status']='validated' if meta.get('exit_code')==0 and review.decision=='pass' and review.recommended_action=='accept' else meta.get('status','failed') if meta.get('exit_code') else 'completed'
            if not meta.get('patch_path') or not meta.get('changed_files'):
                eligible=False; blockers=[]
                if not meta.get('patch_path'): blockers.append('patch is missing')
                if not meta.get('changed_files'): blockers.append('changed files are missing')
            else:
                eligible, blockers=is_attempt_acceptance_eligible(meta)
            meta['acceptance_eligible']=eligible; meta['acceptance_blockers']=blockers
            summary=CandidateSummary(attempt_id=aid, backend_name=b.name, model=b.model, status=meta['status'], exit_code=meta.get('exit_code'), changed_files=meta.get('changed_files') or [], patch_path=meta.get('patch_path'), review_decision=review.decision, review_score=review.score, review_recommended_action=review.recommended_action, review_summary=review.summary, review_issues=review.issues, acceptance_eligible=eligible, acceptance_blockers=blockers, telemetry=meta.get('runner_telemetry') or {})
            meta['candidate_summary']=summary.model_dump(mode='json')
            (adir/'attempt.json').write_text(__import__('json').dumps(meta, indent=2)); attempts.append(meta)
        recorder.transition('select_winner','deciding','Selecting winner from reviewed candidates.')
        select_backend_name=graph.get('select').assigned_backend; select_backend=backends[select_backend_name]
        selection, scall=Selector().select(task, inv, [_selector_candidate_payload(a) for a in attempts], select_backend_name, select_backend, run_dir)
        eligible_ids={a['attempt_id'] for a in attempts if a.get('acceptance_eligible')}
        if selection.decision == 'select' and selection.selected_attempt_id not in eligible_ids:
            from villani_ops.performance.selector import deterministic_fallback
            selection = deterministic_fallback([_selector_candidate_payload(a) for a in attempts])
            selection.selector_backend=select_backend_name; selection.performance_backend={'name': select_backend_name, 'model': select_backend.model}
            (run_dir/'selection.json').write_text(selection.model_dump_json(indent=2))
        if scall: costs['selection']+=scall.estimated_cost; tin+=scall.input_tokens; tout+=scall.output_tokens
        winner=next((a for a in attempts if a.get('attempt_id')==selection.selected_attempt_id and a.get('acceptance_eligible')), None) if selection.decision=='select' else None
        accepted=bool(winner); recorder.transition('accept' if accepted else 'fail','accepted' if accepted else 'failed','Accepted selected candidate.' if accepted else 'No eligible selected candidate.')
        apply_opts={}
        if accepted: apply_opts={'apply_command':f'villani-ops apply {run_id}','branch_command':f'villani-ops branch {run_id} --name villani-ops/{run_id}','pr_command':f'villani-ops pr {run_id} --title "{(task.objective or "Villani Ops changes")[:60]}"'}
        decision=Decision(run_id=run_id, mode=('performance_orchestration' if mode == 'performance' else mode), performance_backend_name=performance_backend_name, performance_backend_model=performance_backend.model, accepted=accepted, lifecycle_completed=True, final_state='accepted' if accepted else 'failed', final_action='accept' if accepted else 'fail', winning_attempt_id=winner.get('attempt_id') if winner else None, winning_branch=winner.get('branch_name') if winner else None, winning_worktree_path=winner.get('worktree_path') if winner else None, winning_patch_path=winner.get('patch_path') if winner else None, reviewer_decision=(winner.get('review') or {}).get('decision') if winner else None, reviewer_score=(winner.get('review') or {}).get('score') if winner else None, classification=cls.model_dump(mode='json') if cls else None, investigation=inv.model_dump(mode='json') if inv else None, selection=selection.model_dump(mode='json') if selection else None, selected_attempt_id=selection.selected_attempt_id if selection else None, candidate_attempts_requested=candidate_attempts, candidate_attempts_completed=len(attempts), eligible_candidate_attempts=[a['attempt_id'] for a in attempts if a.get('acceptance_eligible')], orchestration_summary=__import__('json').dumps(graph.summary()), total_cost=sum(costs.values()), coding_cost=costs['coding'], classification_cost=costs['classification'], policy_cost=0, review_cost=costs['review'], total_input_tokens=tin, total_output_tokens=tout, total_coding_input_tokens=sum(a.get('input_tokens') or 0 for a in attempts), total_coding_output_tokens=sum(a.get('output_tokens') or 0 for a in attempts), token_accounting_statuses=dict(Counter(a.get('token_accounting_status') or 'missing' for a in attempts)), attempts=attempts, warnings=warnings, apply_options=apply_opts, controller_steps=recorder.steps, acceptance_blockers=[] if accepted else [b for a in attempts for b in (a.get('acceptance_blockers') or [])], attempts_used=len(attempts), all_attempted_backends=[a.get('backend_name') for a in attempts], failure_reason='' if accepted else (selection.summary if selection else 'No eligible candidate selected.'), reason='Selected eligible candidate.' if accepted else 'No eligible candidate selected.', total_attempts=len(attempts))
        decision_path=run_dir/'decision.json'
        # enrich decision artifact with mode-era orchestration fields without changing the public model
        write_json(decision_path, {**decision.model_dump(mode='json'), 'mode': mode, 'runner': runner, 'orchestration_graph_summary': graph.summary(), 'selected_backend_per_node': {n.id:n.assigned_backend for n in graph.nodes}, 'selector_summary': selection.summary if selection else '', 'winning_patch_path': decision.winning_patch_path, 'winning_worktree_path': decision.winning_worktree_path})
        # keep storage side effects consistent for latest pointers
        report=write_performance_report(run_dir, task, inv, [a['candidate_summary'] for a in attempts], selection, decision, time.time()-start, mode=mode, runner=runner, graph=graph, selected_backend_per_node={n.id:n.assigned_backend for n in graph.nodes}, routing_decisions={k:v.model_dump(mode='json') for k,v in selections.items()})
        return RunResult(run_id=run_id, run_dir=str(run_dir), decision=decision, report_path=str(report), attempts=attempts)
