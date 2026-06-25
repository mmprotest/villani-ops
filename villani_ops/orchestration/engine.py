from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import time, secrets, json
from pydantic import BaseModel
from villani_ops.core.task import Task
from villani_ops.core.decision import Decision
from villani_ops.storage.files import FileStorage
from villani_ops.classification import TaskClassifier
from villani_ops.execution_policies import policy_for_mode
from villani_ops.isolation.worktree import GitWorktreeIsolation, capture_worktree
from villani_ops.review import LLMReviewer
from villani_ops.core.acceptance import is_attempt_acceptance_eligible
from villani_ops.controller.state_machine import ControllerStateRecorder
from villani_ops.controller.progress import RunProgressReporter
from villani_ops.performance.investigator import Investigator
from villani_ops.performance.selector import Selector, deterministic_fallback
from villani_ops.performance.models import CandidateSummary, InvestigationResult
from villani_ops.performance.report import write_performance_report
from villani_ops.orchestration.planner import build_fixed_graph, Planner
from villani_ops.orchestration.artifacts import write_json
from villani_ops.orchestration.nodes import NodeResult
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.scheduler import GraphScheduler
from villani_ops.runners import runner_for_name
from villani_ops.llm.client import LLMClient

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


class OrchestrationEngine:
    def __init__(self, *, backends, execution_policy, runner_adapter, llm_client=None, workspace: Path, non_interactive: bool=False, progress_reporter=None, storage: FileStorage|None=None) -> None:
        self.backends=backends; self.execution_policy=execution_policy; self.runner_adapter=runner_adapter; self.llm_client=llm_client or LLMClient(); self.workspace=Path(workspace); self.non_interactive=non_interactive; self.progress_reporter=progress_reporter or RunProgressReporter(False); self.storage=storage or FileStorage(self.workspace)

    def run(self, *, repo: str|Path, task: Task, candidate_attempts: int=3, timeout_seconds: int|None=None, classify: bool=True, isolation: str='worktree') -> RunResult:
        if candidate_attempts < 1 or candidate_attempts > 8: raise ValueError('candidate_attempts must be between 1 and 8')
        mode=self.execution_policy.mode; runner=self.runner_adapter.name
        self.storage.init_workspace(); start=time.time(); repo=Path(repo).resolve(); task.repo_path=str(repo)
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)
        run_dir=self.storage.create_run_dir(run_id); self.storage.save_task(run_dir, task)
        recorder=ControllerStateRecorder(run_id, run_dir); scheduler=GraphScheduler(); warnings=[]; attempts=[]; prior_results=[]
        costs={'classification':0.0,'review':0.0,'coding':0.0,'investigation':0.0,'selection':0.0}; tin=tout=0
        ctx=TaskContext(objective=task.objective or task.instruction or '', success_criteria=task.success_criteria)
        graph=build_fixed_graph(candidate_attempts, runner=runner, run_id=run_id, mode=mode, classify=classify, include_decompose=False)
        graph.write(run_dir/'orchestration_graph.json')
        routing_decisions={}
        def write_node(nid, inp=None, out=None, raw=None):
            nd=run_dir/'nodes'/nid; nd.mkdir(parents=True, exist_ok=True); graph.get(nid).artifacts['node_json']=str(nd/'node.json')
            (nd/'node.json').write_text(graph.get(nid).model_dump_json(indent=2));
            if inp is not None: (nd/'input.json').write_text(json.dumps(inp, indent=2, default=str)); graph.get(nid).artifacts['input']=str(nd/'input.json')
            if out is not None: (nd/'output.json').write_text(json.dumps(out, indent=2, default=str)); graph.get(nid).artifacts['output']=str(nd/'output.json')
            if raw is not None: (nd/'raw.txt').write_text(str(raw)); graph.get(nid).artifacts['raw']=str(nd/'raw.txt')
            graph.write(run_dir/'orchestration_graph.json')
        def assign(n):
            sel=self.execution_policy.select_backend(node=n, backends=self.backends, task_context=ctx, prior_results=prior_results)
            n.assigned_backend=sel.backend_name; n.assigned_model=sel.backend.model if sel.backend else self.backends[sel.backend_name].model; routing_decisions[n.id]=sel.model_dump(mode='json')
            return sel
        cls=None; inv=None; plan=None; dec=None; selection=None
        # classify/investigate/plan/decompose serial graph lifecycle
        if classify:
            n=graph.get('classify'); assign(n); graph.mark_running(n.id); write_node(n.id, {'task':ctx.objective})
            try:
                cls, call=TaskClassifier().classify(task, self.backends, run_dir/'classification.json', backend_override=self.backends[n.assigned_backend]); ctx.classification=cls.model_dump(mode='json'); task.classification=cls; self.storage.save_task(run_dir,task)
                if mode!='performance': costs['classification']+=call.estimated_cost
                tin+=call.input_tokens; tout+=call.output_tokens; graph.mark_succeeded(n.id, summary=getattr(cls,'summary','classification complete'), artifacts={'classification':str(run_dir/'classification.json')}, confidence=getattr(cls,'confidence',None)); write_node(n.id, {'task':ctx.objective}, ctx.classification, getattr(call,'raw_text',''))
            except Exception as e:
                warnings.append(f'Classification failed: {e}'); graph.mark_succeeded(n.id, summary=f'Classification unavailable: {e}', confidence=0.0); write_node(n.id, out={'error':str(e), 'fallback_used': True})
        scheduler.next_ready_nodes(graph); n=graph.get('investigate'); assign(n); graph.mark_running(n.id); write_node(n.id, {'task':ctx.objective})
        try:
            inv, call=Investigator().investigate(task, cls, n.assigned_backend, self.backends[n.assigned_backend], run_dir); (run_dir/'investigation.json').write_text(inv.model_dump_json(indent=2)); ctx.investigation=inv.model_dump(mode='json')
            if mode!='performance': costs['investigation']+=call.estimated_cost
            tin+=call.input_tokens; tout+=call.output_tokens; graph.mark_succeeded(n.id, summary=inv.summary, artifacts={'investigation':str(run_dir/'investigation.json'),'raw':str(run_dir/'investigation.raw.txt')}, confidence=inv.confidence); write_node(n.id, {'task':ctx.objective}, ctx.investigation, getattr(call,'raw_text',''))
        except Exception as e:
            warnings.append(f'Investigation failed: {e}'); inv=InvestigationResult(summary=f'Investigation unavailable: {e}'); (run_dir/'investigation.json').write_text(inv.model_dump_json(indent=2)); (run_dir/'investigation.raw.txt').write_text(''); ctx.investigation=inv.model_dump(mode='json'); graph.mark_succeeded(n.id, summary=inv.summary, artifacts={'investigation':str(run_dir/'investigation.json')}, confidence=0.0); write_node(n.id, out=ctx.investigation)
        scheduler.next_ready_nodes(graph); n=graph.get('plan'); assign(n); graph.mark_running(n.id); planner=Planner(self.llm_client)
        plan, pcall=planner.plan(task=task, classification=ctx.classification, investigation=ctx.investigation, repo_summary=None, candidate_attempts=candidate_attempts, mode=mode, backend_name=n.assigned_backend, backend=self.backends[n.assigned_backend], run_dir=run_dir); ctx.plan=plan.model_dump(mode='json'); ctx.overall_difficulty=plan.expected_difficulty; ctx.confidence=plan.confidence; graph.mark_succeeded(n.id, summary=plan.summary, artifacts={'plan':str(run_dir/'plan.json'),'raw':str(run_dir/'plan.raw.txt')}, confidence=plan.confidence); write_node(n.id, {'task':ctx.objective}, ctx.plan, (pcall.raw_text if pcall else ''))
        if plan.should_decompose:
            graph=build_fixed_graph(candidate_attempts, runner=runner, run_id=run_id, mode=mode, classify=classify, include_decompose=True)
            for nid in ['classify','investigate','plan']:
                if any(x.id==nid for x in graph.nodes): graph.update_node(nid, status='succeeded')
            scheduler.next_ready_nodes(graph); n=graph.get('decompose'); assign(n); graph.mark_running(n.id)
            dec, dcall=planner.decompose(task=task, plan=plan, investigation=ctx.investigation, backend=self.backends[n.assigned_backend], run_dir=run_dir); ctx.decomposition=dec.model_dump(mode='json'); graph.mark_succeeded(n.id, summary=dec.reason, artifacts={'decomposition':str(run_dir/'decomposition.json'),'raw':str(run_dir/'decomposition.raw.txt')}, confidence=dec.confidence); write_node(n.id, {'plan':ctx.plan}, ctx.decomposition, (dcall.raw_text if dcall else ''))
        # assign remaining nodes after context signals
        for n in graph.nodes:
            if not n.assigned_backend:
                assign(n)
        graph.write(run_dir/'orchestration_graph.json')
        # execute code/review nodes in dependency order
        for idx in range(1,candidate_attempts+1):
            scheduler.next_ready_nodes(graph); aid=f'attempt_{idx:03d}'; cn=graph.get(f'code_{aid}'); graph.mark_running(cn.id); b=self.backends[cn.assigned_backend]; adir=run_dir/'attempts'/aid; adir.mkdir(parents=True, exist_ok=True)
            prompt=_candidate_prompt(task, inv, idx, candidate_attempts)
            if ctx.decomposition: prompt += '\nAdvisory decomposition:\n' + json.dumps(ctx.decomposition, indent=2)[:12000]
            meta={'attempt_id':aid,'backend_name':b.name,'model':b.model,'runner_name':runner,'status':'running','started_at':datetime.now(timezone.utc).isoformat()}
            try:
                if isolation!='worktree': raise ValueError('Only worktree isolation is supported')
                wt=GitWorktreeIsolation().create(repo, run_id, aid, self.storage.workspace); meta.update(wt)
                res=self.runner_adapter.run_task(repo_path=Path(wt['worktree_path']), task=prompt, success_criteria=task.success_criteria, backend_name=b.name, backend_config=b, timeout_seconds=timeout_seconds or b.timeout_seconds or 1200, context={'attempt_id':aid,'node_id':cn.id}, artifacts_dir=adir)
                (adir/'stdout.txt').write_text(res.stdout); (adir/'stderr.txt').write_text(res.stderr); (adir/'stdout.log').write_text(res.stdout); (adir/'stderr.log').write_text(res.stderr)
                if mode!='performance': costs['coding']+=b.estimate_cost(res.input_tokens,res.output_tokens)
                tin+=res.input_tokens; tout+=res.output_tokens
                meta.update({'exit_code':res.exit_code,'stdout_path':str(adir/'stdout.txt'),'stderr_path':str(adir/'stderr.txt'),'duration_ms':res.duration_ms,'input_tokens':res.input_tokens,'output_tokens':res.output_tokens,'token_accounting_status':res.token_accounting_status,'runner_telemetry':res.telemetry})
                cap=capture_worktree(wt['worktree_path'], adir); meta.update(cap)
                if meta.get('patch_path'):
                    pp=Path(meta['patch_path']); compat=adir/'patch.diff'; compat.write_text(pp.read_text(errors='replace') if pp.exists() else '') ; meta['patch_path']=str(compat)
                graph.mark_succeeded(cn.id, summary=f'Candidate {aid} completed', artifacts={'attempt':str(adir/'attempt.json'),'patch':str(meta.get('patch_path') or '')})
            except Exception as e:
                meta.update({'status':'failed','exit_code':meta.get('exit_code',1),'error':str(e),'changed_files':[],'patch_path':None}); (adir/'stderr.txt').write_text(str(e)); graph.mark_failed(cn.id, str(e))
            write_node(cn.id, {'prompt':prompt}, meta)
            scheduler.next_ready_nodes(graph); rn=graph.get(f'review_{aid}'); graph.mark_running(rn.id); rb=self.backends[rn.assigned_backend]
            review_input={k:meta.get(k) for k in ['attempt_id','exit_code','stdout_path','stderr_path','changed_files','git_status','runner_telemetry']}; review_input.update({'stdout_summary':_tail(meta.get('stdout_path'),4000),'stderr_summary':_tail(meta.get('stderr_path'),4000),'patch':_patch_text(meta.get('patch_path'),50000),'investigation':ctx.investigation})
            try:
                review, rcall=LLMReviewer().review(task, cls, rb, review_input, self.backends, adir/'review.json', backend_override=rb)
                if mode!='performance': costs['review']+=rcall.estimated_cost
                tin+=rcall.input_tokens; tout+=rcall.output_tokens
            except Exception as e:
                from villani_ops.review import ReviewResult
                review=ReviewResult(decision='fail', summary=f'Reviewer failed: {e}', issues=[str(e)], recommended_action='fail'); (adir/'review.json').write_text(review.model_dump_json(indent=2))
            meta['review']=review.model_dump(mode='json'); meta['status']='validated' if meta.get('exit_code')==0 and review.decision=='pass' and review.recommended_action=='accept' else ('failed' if meta.get('exit_code') else 'completed')
            if not meta.get('patch_path') or not meta.get('changed_files'):
                eligible=False; blockers=[]
                if not meta.get('patch_path'): blockers.append('patch is missing')
                if not meta.get('changed_files'): blockers.append('changed files are missing')
            else: eligible, blockers=is_attempt_acceptance_eligible(meta)
            meta['acceptance_eligible']=eligible; meta['acceptance_blockers']=blockers
            summary=CandidateSummary(attempt_id=aid, backend_name=b.name, model=b.model, status=meta['status'], exit_code=meta.get('exit_code'), changed_files=meta.get('changed_files') or [], patch_path=meta.get('patch_path'), review_decision=review.decision, review_score=review.score, review_recommended_action=review.recommended_action, review_summary=review.summary, review_issues=review.issues, acceptance_eligible=eligible, acceptance_blockers=blockers, telemetry=meta.get('runner_telemetry') or {})
            meta['candidate_summary']=summary.model_dump(mode='json'); (adir/'attempt.json').write_text(json.dumps(meta, indent=2)); attempts.append(meta)
            graph.mark_succeeded(rn.id, summary=review.summary, artifacts={'review':str(adir/'review.json'),'attempt':str(adir/'attempt.json')}, confidence=review.score or 0.0); write_node(rn.id, review_input, review.model_dump(mode='json'))
            prior_results.append(NodeResult(node_id=rn.id, status='failed' if not eligible else 'succeeded', summary=review.summary, data={'acceptance_blockers':blockers}))
        scheduler.next_ready_nodes(graph); sn=graph.get('select'); graph.mark_running(sn.id); select_backend=self.backends[sn.assigned_backend]
        candidates=[_selector_candidate_payload(a) for a in attempts]
        selection, scall=Selector().select(task, inv, candidates, sn.assigned_backend, select_backend, run_dir)
        if scall and mode!='performance': costs['selection']+=scall.estimated_cost
        eligible_ids={a['attempt_id'] for a in attempts if a.get('acceptance_eligible')}
        if selection.decision=='select' and selection.selected_attempt_id not in eligible_ids:
            selection=deterministic_fallback(candidates); selection.selector_backend=sn.assigned_backend; (run_dir/'selection.json').write_text(selection.model_dump_json(indent=2))
        graph.mark_succeeded(sn.id, summary=selection.summary, artifacts={'selection':str(run_dir/'selection.json'),'selection_input':str(run_dir/'selection_input.json')}, confidence=selection.confidence); write_node(sn.id, {'candidates':candidates}, selection.model_dump(mode='json'), Path(run_dir/'selection.raw.txt').read_text(errors='replace') if (run_dir/'selection.raw.txt').exists() else '')
        winner=next((a for a in attempts if a.get('attempt_id')==selection.selected_attempt_id and a.get('acceptance_eligible')), None) if selection.decision=='select' else None
        scheduler.next_ready_nodes(graph); vn=graph.get('verify'); graph.mark_running(vn.id); accepted=bool(winner); graph.mark_succeeded(vn.id, summary='Accepted selected candidate.' if accepted else 'No eligible selected candidate.'); write_node(vn.id, {'selection':selection.model_dump(mode='json')}, {'accepted':accepted})
        apply_opts={}
        if accepted: apply_opts={'apply_command':f'villani-ops apply {run_id}','branch_command':f'villani-ops branch {run_id} --name villani-ops/{run_id}','pr_command':f'villani-ops pr {run_id} --title "{(task.objective or "Villani Ops changes")[:60]}"'}
        decision=Decision(run_id=run_id, mode=mode, runner=runner, orchestration_graph_path=str(run_dir/'orchestration_graph.json'), node_backend_assignments={n.id:n.assigned_backend for n in graph.nodes if n.assigned_backend}, plan=ctx.plan, decomposition=ctx.decomposition, performance_backend_name=(graph.get('investigate').assigned_backend if mode=='performance' else None), performance_backend_model=(self.backends[graph.get('investigate').assigned_backend].model if mode=='performance' else None), accepted=accepted, lifecycle_completed=True, final_state='accepted' if accepted else 'failed', final_action='accept' if accepted else 'fail', winning_attempt_id=winner.get('attempt_id') if winner else None, winning_worktree_path=winner.get('worktree_path') if winner else None, winning_patch_path=winner.get('patch_path') if winner else None, reviewer_decision=(winner.get('review') or {}).get('decision') if winner else None, reviewer_score=(winner.get('review') or {}).get('score') if winner else None, classification=ctx.classification, investigation=ctx.investigation, selection=selection.model_dump(mode='json'), selected_attempt_id=selection.selected_attempt_id if accepted else None, candidate_attempts_requested=candidate_attempts, candidate_attempts_completed=len(attempts), eligible_candidate_attempts=[a['attempt_id'] for a in attempts if a.get('acceptance_eligible')], orchestration_summary=json.dumps(graph.summary()), total_cost=0 if mode=='performance' else sum(costs.values()), coding_cost=0 if mode=='performance' else costs['coding'], classification_cost=0 if mode=='performance' else costs['classification'], review_cost=0 if mode=='performance' else costs['review'], total_input_tokens=tin, total_output_tokens=tout, total_coding_input_tokens=sum(a.get('input_tokens') or 0 for a in attempts), total_coding_output_tokens=sum(a.get('output_tokens') or 0 for a in attempts), token_accounting_statuses=dict(Counter(a.get('token_accounting_status') or 'missing' for a in attempts)), attempts=attempts, warnings=warnings, apply_options=apply_opts, controller_steps=recorder.steps, acceptance_blockers=[] if accepted else [b for a in attempts for b in (a.get('acceptance_blockers') or [])], attempts_used=len(attempts), all_attempted_backends=[a.get('backend_name') for a in attempts], failure_reason='' if accepted else selection.summary, reason='Selected eligible candidate.' if accepted else 'No eligible candidate selected.', total_attempts=len(attempts))
        write_json(run_dir/'decision.json', decision.model_dump(mode='json'))
        graph.write(run_dir/'orchestration_graph.json')
        report=write_performance_report(run_dir, task, inv, [a['candidate_summary'] for a in attempts], selection, decision, time.time()-start, mode=mode, runner=runner, graph=graph, selected_backend_per_node=decision.node_backend_assignments, routing_decisions=routing_decisions)
        return RunResult(run_id=run_id, run_dir=str(run_dir), decision=decision, report_path=str(report), attempts=attempts)
