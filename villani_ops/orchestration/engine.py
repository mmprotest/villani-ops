from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from dataclasses import dataclass, field
import json, secrets, time
from typing import Any
from pydantic import BaseModel
from villani_ops.core.task import Task
from villani_ops.core.decision import Decision
from villani_ops.storage.files import FileStorage
from villani_ops.classification import TaskClassifier
from villani_ops.isolation.worktree import GitWorktreeIsolation, capture_worktree
from villani_ops.review import LLMReviewer, ReviewResult
from villani_ops.core.acceptance import is_attempt_acceptance_eligible, has_non_empty_patch
from villani_ops.controller.progress import RunProgressReporter
from villani_ops.performance.investigator import Investigator
from villani_ops.performance.selector import Selector, deterministic_fallback
from villani_ops.performance.models import CandidateSummary, InvestigationResult
from villani_ops.performance.report import write_performance_report
from villani_ops.orchestration.planner import build_fixed_graph, Planner
from villani_ops.orchestration.artifacts import write_json, write_json_utf8, write_text_utf8
from villani_ops.orchestration.nodes import OrchestrationNode, NodeExecutionResult, NodeResult
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.scheduler import GraphScheduler
from villani_ops.llm.client import LLMClient

class RunResult(BaseModel):
    run_id: str; run_dir: str; decision: Decision; report_path: str; attempts: list[dict]

@dataclass
class EngineContext:
    repo: Path; task: Task; candidate_attempts: int; timeout_seconds: int|None; isolation: str
    run_id: str; run_dir: Path; mode: str; runner: str; graph: Any; scheduler: GraphScheduler
    task_context: TaskContext; start: float
    warnings: list[str]=field(default_factory=list); attempts: list[dict]=field(default_factory=list)
    costs: dict[str,float]=field(default_factory=lambda:{'classification':0.0,'review':0.0,'coding':0.0,'investigation':0.0,'selection':0.0})
    input_tokens:int=0; output_tokens:int=0; routing_decisions:dict[str,dict]=field(default_factory=dict)
    classification:Any=None; investigation:Any=None; plan:Any=None; decomposition:Any=None; selection:Any=None; winner:dict|None=None; final_decision:Decision|None=None

def _now(): return datetime.now(timezone.utc).isoformat()
def _candidate_prompt(task: Task, inv, n: int, total: int, decomposition=None) -> str:
    decomp_lines=[]
    if decomposition and getattr(decomposition, 'should_use_decomposition', False):
        decomp_lines.append('\nDecomposition guidance (advisory):')
        if getattr(decomposition, 'merge_strategy', None): decomp_lines.append(f"Merge/integration strategy: {decomposition.merge_strategy}")
        for st in getattr(decomposition, 'subtasks', []) or []:
            files=', '.join(getattr(st, 'relevant_files', []) or [])
            deps=', '.join(getattr(st, 'dependencies', []) or [])
            sc=getattr(st, 'success_criteria', None)
            line=f"- {st.id}: {st.title}. Objective: {st.objective}"
            if files: line += f" Files: {files}."
            if deps: line += f" Dependencies: {deps}."
            if sc: line += f" Success criteria: {sc}."
            decomp_lines.append(line)
    return f"""Original objective:
{task.objective or task.instruction or ''}

Success criteria:
{task.success_criteria or ''}

Investigation summary:
{inv.summary if inv else ''}

Suspected root cause:
{(getattr(inv,'suspected_root_cause','') if inv else '') or ''}

Relevant files:
{', '.join(getattr(inv,'relevant_files',[]) if inv else [])}

Relevant tests:
{', '.join(getattr(inv,'relevant_tests',[]) if inv else [])}

Implementation plan:
{chr(10).join('- '+x for x in (getattr(inv,'implementation_plan',[]) if inv else []))}
{chr(10).join(decomp_lines)}

You are candidate attempt {n} of {total}.
Work independently.
Produce the smallest correct patch you can.
Run relevant tests when possible.
"""
def _tail(path: str | None, limit: int = 8000) -> str:
    try: return Path(path).read_text(errors='replace')[-limit:] if path else ''
    except Exception: return ''
def _patch_text(path: str | None, limit: int = 60000) -> str:
    try:
        t=Path(path).read_text(errors='replace') if path else ''
        return t[:limit]+('\n...[truncated]' if len(t)>limit else '')
    except Exception: return ''
def _selector_candidate_payload(a: dict) -> dict:
    review=a.get('review') or {}
    return {'attempt_id':a.get('attempt_id'),'backend_name':a.get('backend_name'),'model':a.get('model'),'status':a.get('status'),'exit_code':a.get('exit_code'),'changed_files':a.get('changed_files') or [],'git_status':a.get('git_status') or '','patch_text':_patch_text(a.get('patch_path')),'stdout_tail':_tail(a.get('stdout_path')),'stderr_tail':_tail(a.get('stderr_path')),'review':review,'review_score':review.get('score'),'review_summary':review.get('summary') or '','review_evidence':review.get('evidence') or [],'review_issues':review.get('issues') or [],'review_recommended_action':review.get('recommended_action') or '','acceptance_eligible':bool(a.get('acceptance_eligible')),'acceptance_blockers':a.get('acceptance_blockers') or []}

class OrchestrationEngine:
    def __init__(self, *, backends, execution_policy, runner_adapter, llm_client=None, workspace: Path, non_interactive: bool=False, progress_reporter=None, storage: FileStorage|None=None) -> None:
        self.backends=backends; self.execution_policy=execution_policy; self.runner_adapter=runner_adapter; self.llm_client=llm_client or LLMClient(); self.workspace=Path(workspace); self.non_interactive=non_interactive; self.progress_reporter=progress_reporter or RunProgressReporter(False); self.storage=storage or FileStorage(self.workspace)

    def run(self, *, repo: str|Path, task: Task, candidate_attempts: int=3, timeout_seconds: int|None=None, classify: bool=True, isolation: str='worktree') -> RunResult:
        if candidate_attempts < 1 or candidate_attempts > 8: raise ValueError('candidate_attempts must be between 1 and 8')
        self.storage.init_workspace(); repo=Path(repo).resolve(); task.repo_path=str(repo)
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3); run_dir=self.storage.create_run_dir(run_id); self.storage.save_task(run_dir, task)
        graph=build_fixed_graph(candidate_attempts, runner=self.runner_adapter.name, run_id=run_id, mode=self.execution_policy.mode, classify=classify, include_decompose=True); graph.write(run_dir/'orchestration_graph.json')
        context=EngineContext(repo, task, candidate_attempts, timeout_seconds, isolation, run_id, run_dir, self.execution_policy.mode, self.runner_adapter.name, graph, GraphScheduler(), TaskContext(objective=task.objective or task.instruction or '', success_criteria=task.success_criteria), time.time())
        self.progress_reporter.start_run(run_dir=str(run_dir), mode=context.mode, runner=context.runner, candidate_attempts=candidate_attempts)
        while not graph.is_terminal():
            ready_nodes=context.scheduler.next_ready_nodes(graph)
            if not ready_nodes:
                self.record_controller_step(context, action='no_ready_nodes', status='blocked', summary='No scheduler-ready nodes remain.')
                for n in graph.pending_nodes(): graph.mark_skipped(n.id, 'No scheduler-ready path remained.')
                break
            for node in ready_nodes:
                self.record_controller_step(context, node=node, action='node_ready', status=node.status)
                self.execute_node(node=node, context=context)
                graph.write(run_dir/'orchestration_graph.json')
        decision=self._finalize(context)
        report=write_performance_report(run_dir, task, context.investigation, [a['candidate_summary'] for a in context.attempts], context.selection, decision, time.time()-context.start, mode=context.mode, runner=context.runner, graph=graph, selected_backend_per_node=decision.node_backend_assignments, routing_decisions=context.routing_decisions)
        return RunResult(run_id=run_id, run_dir=str(run_dir), decision=decision, report_path=str(report), attempts=context.attempts)

    def build_task_context(self, *, context: EngineContext) -> TaskContext: return context.task_context
    def collect_prior_results(self, *, node: OrchestrationNode, context: EngineContext) -> list[NodeResult]:
        g=context.graph; out=[]
        def add(nid):
            try:
                n=g.get(nid); data=n.result or {}; out.append(NodeResult(node_id=n.id, kind=n.kind, status=n.status, result_summary=n.result_summary, summary=n.result_summary, confidence=n.confidence, difficulty=n.difficulty, risk=n.risk, has_failure=n.status=='failed', has_review_blocker=bool(data.get('has_review_blocker')), has_acceptance_blocker=bool(data.get('has_acceptance_blocker')), data=data, error=n.error))
            except KeyError: pass
        shared=['classify','investigate','plan','decompose']
        if node.kind=='classify': return []
        if node.kind=='investigate': add('classify'); return out
        if node.kind=='plan': [add(x) for x in ['classify','investigate']]; return out
        if node.kind=='decompose': [add(x) for x in ['classify','investigate','plan']]; return out
        if node.kind=='code': [add(x) for x in shared]; return out
        if node.kind=='review': [add(x) for x in [node.dependencies[0], *shared]]; return out
        if node.kind=='select':
            for n in g.nodes:
                if n.kind in {'code','review'}: add(n.id)
            [add(x) for x in shared]; return out
        if node.kind=='verify':
            add('select'); sel=(context.selection.selected_attempt_id if context.selection else None)
            if sel: add(f'code_{sel}'); add(f'review_{sel}')
            return out
        return out

    def assign_backend_for_node(self, *, node: OrchestrationNode, context: EngineContext):
        selection=self.execution_policy.select_backend(node=node, backends=self.backends, task_context=self.build_task_context(context=context), prior_results=self.collect_prior_results(node=node, context=context))
        node.assigned_backend=selection.backend_name; node.assigned_model=(selection.backend.model if selection.backend else self.backends[selection.backend_name].model)
        context.routing_decisions[node.id]=selection.model_dump(mode='json', exclude={'backend'}) | {'model': node.assigned_model}
        node.artifacts['policy_decision']=str(context.run_dir/'nodes'/node.id/'policy_decision.json')
        self.record_controller_step(context, node=node, action='backend_assigned', status='assigned', summary=selection.reason, details=context.routing_decisions[node.id])
        return selection

    def execute_node(self, *, node: OrchestrationNode, context: EngineContext) -> NodeExecutionResult:
        dispatch={'classify':self._execute_classify_node,'investigate':self._execute_investigate_node,'plan':self._execute_plan_node,'decompose':self._execute_decompose_node,'code':self._execute_code_node,'review':self._execute_review_node,'select':self._execute_select_node,'verify':self._execute_verify_node}
        if node.kind not in dispatch: raise ValueError(f'Unknown orchestration node kind: {node.kind}')
        self.assign_backend_for_node(node=node, context=context)
        self.record_controller_step(context, node=node, action='node_started', status='running')
        self.progress_reporter.node_started(node)
        try: return dispatch[node.kind](node, context)
        except Exception as e:
            context.graph.mark_failed(node.id, str(e)); self._write_node_artifacts(context, node, out={'error':str(e)}); self.record_controller_step(context,node=node,action='node_failed',status='failed',summary=str(e)); self.progress_reporter.node_failed(node, str(e)); return NodeExecutionResult(node_id=node.id,status='failed',error=str(e))

    def _write_node_artifacts(self, context, node, inp=None, out=None, raw=None):
        nd=context.run_dir/'nodes'/node.id; nd.mkdir(parents=True, exist_ok=True); node.artifacts['node_json']=str(nd/'node.json')
        if node.artifacts.get('policy_decision'): write_json_utf8(nd/'policy_decision.json', context.routing_decisions.get(node.id,{}))
        for name,obj in [('input',inp),('output',out)]:
            if obj is not None: write_json_utf8(nd/f'{name}.json', obj); node.artifacts[name]=str(nd/f'{name}.json'); self.record_controller_step(context,node=node,action='artifact_written',status=node.status,summary=f'{name}.json')
        if raw is not None: write_text_utf8(nd/'raw.txt', str(raw)); node.artifacts['raw']=str(nd/'raw.txt'); self.record_controller_step(context,node=node,action='artifact_written',status=node.status,summary='raw.txt')
        write_json_utf8(nd/'node.json', node); context.graph.write(context.run_dir/'orchestration_graph.json')

    def _finish(self, context, node, result:NodeExecutionResult, data=None):
        if result.status=='succeeded': context.graph.mark_succeeded(node.id, summary=result.result_summary, artifacts=result.artifacts, confidence=result.confidence, difficulty=result.difficulty, risk=result.risk)
        elif result.status=='skipped': context.graph.mark_skipped(node.id, result.result_summary or result.error or 'skipped')
        else: context.graph.mark_failed(node.id, result.error or result.result_summary or 'failed')
        node.result=data or result.data; self._write_node_artifacts(context,node,out=data or result.model_dump(mode='json'))
        self.record_controller_step(context,node=node,action=f'node_{result.status}',status=result.status,summary=result.result_summary or result.error)
        if result.status=='succeeded': self.progress_reporter.node_completed(node, data or result.data or {}, result.result_summary)
        elif result.status=='skipped': self.progress_reporter.node_skipped(node, result.result_summary or result.error or 'skipped')
        else: self.progress_reporter.node_failed(node, result.error or result.result_summary or 'failed')
        return result

    def _execute_classify_node(self,node,context):
        context.graph.mark_running(node.id); self._write_node_artifacts(context,node,inp={'task':context.task_context.objective})
        try:
            try:
                cls,call=TaskClassifier().classify(context.task,self.backends,context.run_dir/'classification.json',backend_override=self.backends[node.assigned_backend],estimate_cost=(context.mode!='performance'))
            except TypeError:
                cls,call=TaskClassifier().classify(context.task,self.backends,context.run_dir/'classification.json',backend_override=self.backends[node.assigned_backend])
            context.classification=cls; context.task_context.classification=cls.model_dump(mode='json'); context.task.classification=cls; self.storage.save_task(context.run_dir,context.task); context.input_tokens+=call.input_tokens; context.output_tokens+=call.output_tokens; context.costs['classification']+=0 if context.mode=='performance' else call.estimated_cost
            return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=getattr(cls,'summary','classification complete'),confidence=getattr(cls,'confidence',None),artifacts={'classification':str(context.run_dir/'classification.json')},data=context.task_context.classification),context.task_context.classification)
        except Exception as e:
            context.warnings.append(f'Classification failed: {e}'); return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=f'Classification unavailable: {e}',confidence=0.0,data={'fallback_used':True,'error':str(e)}),{'fallback_used':True,'error':str(e)})

    def _execute_investigate_node(self,node,context):
        context.graph.mark_running(node.id); self._write_node_artifacts(context,node,inp={'task':context.task_context.objective})
        try:
            try:
                inv,call=Investigator().investigate(context.task,context.classification,node.assigned_backend,self.backends[node.assigned_backend],context.run_dir,estimate_cost=(context.mode!='performance'))
            except TypeError:
                inv,call=Investigator().investigate(context.task,context.classification,node.assigned_backend,self.backends[node.assigned_backend],context.run_dir)
        except Exception as e:
            context.warnings.append(f'Investigation failed: {e}'); inv=InvestigationResult(summary=f'Investigation unavailable: {e}', investigation_fallback_used=True, investigation_fallback_reason=str(e)); call=None
            write_text_utf8(context.run_dir/'investigation.raw.txt', f'ERROR: {e}')
            write_json_utf8(context.run_dir/'investigation_normalized.json', {'normalized': False, 'payload': {}, 'notes': [], 'error': str(e)})
        write_json_utf8(context.run_dir/'investigation.json', inv); context.investigation=inv; context.task_context.investigation=inv.model_dump(mode='json')
        if getattr(inv, 'investigation_normalized', False):
            for note in inv.investigation_normalization_notes: self.record_controller_step(context,node=node,action='investigation_normalized',status='normalized',summary=note)
        if getattr(inv, 'investigation_fallback_used', False): self.record_controller_step(context,node=node,action='investigation_fallback_used',status='fallback',summary=inv.investigation_fallback_reason or inv.summary)
        if call: context.input_tokens+=call.input_tokens; context.output_tokens+=call.output_tokens; context.costs['investigation']+=0 if context.mode=='performance' else call.estimated_cost
        self._write_node_artifacts(context,node,raw=(call.raw_text if call else ''))
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=inv.summary,confidence=inv.confidence,artifacts={'raw':'investigation.raw.txt','normalized':'investigation_normalized.json','output':'investigation.json','investigation':str(context.run_dir/'investigation.json')}),context.task_context.investigation)

    def _execute_plan_node(self,node,context):
        context.graph.mark_running(node.id); self._write_node_artifacts(context,node,inp={'task':context.task_context.objective})
        plan,call=Planner(self.llm_client).plan(task=context.task,classification=context.task_context.classification,investigation=context.task_context.investigation,repo_summary=None,candidate_attempts=context.candidate_attempts,mode=context.mode,backend_name=node.assigned_backend,backend=self.backends[node.assigned_backend],run_dir=context.run_dir)
        context.plan=plan; context.task_context.plan=plan.model_dump(mode='json'); context.task_context.overall_difficulty=plan.expected_difficulty; context.task_context.confidence=plan.confidence
        if getattr(plan, 'planner_normalized', False):
            for note in plan.planner_normalization_notes: self.record_controller_step(context,node=node,action='planner_normalized',status='normalized',summary=note)
        if getattr(plan, 'planner_repaired', False):
            for note in plan.planner_repair_notes: self.record_controller_step(context,node=node,action='planner_repaired',status='repaired',summary=note)
            self.progress_reporter.step('[3/8] Plan repaired: strategy=decompose_then_execute, decompose=true, reason=multi-file/multi-subsystem context')
        if getattr(plan, 'planner_fallback_used', False) or getattr(plan, 'fallback_used', False): self.record_controller_step(context,node=node,action='planner_fallback_used',status='fallback',summary=plan.planner_fallback_reason or plan.summary)
        if call: context.input_tokens+=call.input_tokens; context.output_tokens+=call.output_tokens
        self._write_node_artifacts(context,node,raw=(call.raw_text if call else ''))
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=plan.summary,confidence=plan.confidence,difficulty=plan.expected_difficulty,artifacts={'raw':'plan.raw.txt','normalized':'plan_normalized.json','output':'plan.json','plan':str(context.run_dir/'plan.json')}),context.task_context.plan)

    def _execute_decompose_node(self,node,context):
        context.graph.mark_running(node.id); self._write_node_artifacts(context,node,inp={'plan':context.task_context.plan})
        if not context.plan.should_decompose:
            return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='skipped',result_summary='Planner did not request decomposition',data={'intentional_skip':True}),{'intentional_skip':True,'reason':'Planner did not request decomposition'})
        dec,call=Planner(self.llm_client).decompose(task=context.task,plan=context.plan,investigation=context.task_context.investigation,backend=self.backends[node.assigned_backend],run_dir=context.run_dir,estimate_cost=(context.mode!='performance'))
        context.decomposition=dec; context.task_context.decomposition=dec.model_dump(mode='json'); self._write_node_artifacts(context,node,raw=(call.raw_text if call else ''))
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=dec.reason,confidence=dec.confidence,artifacts={'raw':'decomposition.raw.txt','normalized':'decomposition_normalized.json','decomposition':'decomposition.json','output':str(context.run_dir/'nodes'/node.id/'output.json')}),context.task_context.decomposition)

    def _execute_code_node(self,node,context):
        context.graph.mark_running(node.id); aid=node.id.replace('code_',''); idx=int(aid.split('_')[-1]); adir=context.run_dir/'attempts'/aid; adir.mkdir(parents=True,exist_ok=True); b=self.backends[node.assigned_backend]
        self.progress_reporter.candidate_started(aid, idx, context.candidate_attempts, b.name)
        prompt=_candidate_prompt(context.task,context.investigation,idx,context.candidate_attempts, context.decomposition); meta={'attempt_id':aid,'backend_name':b.name,'model':b.model,'runner_name':context.runner,'status':'running','started_at':_now()}; self._write_node_artifacts(context,node,inp={'prompt':prompt, 'decomposition_context': context.task_context.decomposition})
        try:
            if context.isolation!='worktree': raise ValueError('Only worktree isolation is supported')
            wt=GitWorktreeIsolation().create(context.repo,context.run_id,aid,self.storage.workspace); meta.update(wt)
            res=self.runner_adapter.run_task(repo_path=Path(wt['worktree_path']), task=prompt, success_criteria=context.task.success_criteria, backend_name=b.name, backend_config=b, timeout_seconds=context.timeout_seconds or b.timeout_seconds or 1200, context={'attempt_id':aid,'node_id':node.id}, artifacts_dir=adir)
            write_text_utf8(adir/'stdout.txt', res.stdout); write_text_utf8(adir/'stderr.txt', res.stderr); write_text_utf8(adir/'stdout.log', res.stdout); write_text_utf8(adir/'stderr.log', res.stderr); context.input_tokens+=res.input_tokens; context.output_tokens+=res.output_tokens; context.costs['coding']+=0 if context.mode=='performance' else b.estimate_cost(res.input_tokens,res.output_tokens)
            meta.update({'exit_code':res.exit_code,'stdout_path':str(adir/'stdout.txt'),'stderr_path':str(adir/'stderr.txt'),'duration_ms':res.duration_ms,'input_tokens':res.input_tokens,'output_tokens':res.output_tokens,'token_accounting_status':res.token_accounting_status,'runner_telemetry':res.telemetry}); meta.update(capture_worktree(wt['worktree_path'],adir))
            if meta.get('patch_path'):
                compat=adir/'patch.diff'; write_text_utf8(compat, Path(meta['patch_path']).read_text(encoding='utf-8', errors='replace') if Path(meta['patch_path']).exists() else ''); meta['patch_path']=str(compat)
            meta['has_patch']=has_non_empty_patch(meta.get('patch_path'))
            status='succeeded' if res.exit_code==0 else 'failed'
        except Exception as e:
            meta.update({'status':'failed','exit_code':meta.get('exit_code',1),'error':str(e),'changed_files':[],'patch_path':None,'has_patch':False}); write_text_utf8(adir/'stderr.txt', str(e)); status='failed'
        write_json_utf8(adir/'attempt.json', meta); node.result=meta
        self.progress_reporter.candidate_completed(aid, idx, context.candidate_attempts, meta)
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status=status,result_summary=meta.get('error') or f'Candidate {aid} completed',artifacts={'attempt':str(adir/'attempt.json'),'patch':str(meta.get('patch_path') or '')},data=meta,error=meta.get('error')),meta)

    def _execute_review_node(self,node,context):
        context.graph.mark_running(node.id); aid=node.id.replace('review_',''); idx=int(aid.split('_')[-1]); self.progress_reporter.review_started(aid, idx, context.candidate_attempts); cn=context.graph.get(f'code_{aid}'); meta=dict(cn.result or {}); adir=context.run_dir/'attempts'/aid; rb=self.backends[node.assigned_backend]
        review_input={k:meta.get(k) for k in ['attempt_id','exit_code','stdout_path','stderr_path','changed_files','git_status','runner_telemetry']}; review_input.update({'stdout_summary':_tail(meta.get('stdout_path'),4000),'stderr_summary':_tail(meta.get('stderr_path'),4000),'patch':_patch_text(meta.get('patch_path'),50000),'investigation':context.task_context.investigation}); self._write_node_artifacts(context,node,inp=review_input)
        try:
            try:
                review,call=LLMReviewer().review(context.task,context.classification,rb,review_input,self.backends,adir/'review.json',backend_override=rb,estimate_cost=(context.mode!='performance'))
            except TypeError:
                review,call=LLMReviewer().review(context.task,context.classification,rb,review_input,self.backends,adir/'review.json',backend_override=rb)
            context.costs['review']+=0 if context.mode=='performance' else call.estimated_cost; context.input_tokens+=call.input_tokens; context.output_tokens+=call.output_tokens; self._write_node_artifacts(context,node,raw=call.raw_text)
        except Exception as e: review=ReviewResult(decision='fail',summary=f'Reviewer failed: {e}',issues=[str(e)],recommended_action='fail'); write_json_utf8(adir/'review.json', review)
        meta['review']=review.model_dump(mode='json'); meta['status']='validated' if meta.get('exit_code')==0 and review.decision=='pass' and review.recommended_action=='accept' else ('failed' if meta.get('exit_code') else 'completed')
        has_patch=has_non_empty_patch(meta.get('patch_path'))
        eligible,blockers=(False, ['patch is missing or empty'] if not has_patch else [])
        if has_patch and meta.get('changed_files'): eligible,blockers=is_attempt_acceptance_eligible(meta)
        elif not meta.get('changed_files'): blockers.append('changed files are missing')
        meta['has_patch']=has_patch
        meta['acceptance_eligible']=eligible; meta['acceptance_blockers']=blockers
        summary=CandidateSummary(attempt_id=aid,backend_name=meta.get('backend_name'),model=meta.get('model'),status=meta['status'],exit_code=meta.get('exit_code'),changed_files=meta.get('changed_files') or [],patch_path=meta.get('patch_path'),review_decision=review.decision,review_score=review.score,review_recommended_action=review.recommended_action,review_summary=review.summary,review_issues=review.issues,acceptance_eligible=eligible,acceptance_blockers=blockers,has_patch=has_patch, telemetry=meta.get('runner_telemetry') or {})
        meta['candidate_summary']=summary.model_dump(mode='json'); write_json_utf8(adir/'attempt.json', meta); context.attempts.append(meta)
        self.progress_reporter.review_completed(aid, idx, context.candidate_attempts, {**review.model_dump(mode='json'), 'acceptance_eligible': eligible})
        data={'has_review_blocker': review.decision!='pass' or review.recommended_action!='accept','has_acceptance_blocker': bool(blockers),'acceptance_blockers':blockers, **review.model_dump(mode='json')}
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=review.summary,confidence=review.score or 0.0,artifacts={'review':str(adir/'review.json'),'attempt':str(adir/'attempt.json')},data=data),data)

    def _execute_select_node(self,node,context):
        context.graph.mark_running(node.id); self.progress_reporter.selector_started(); candidates=[_selector_candidate_payload(a) for a in context.attempts]; self._write_node_artifacts(context,node,inp={'candidates':candidates})

        try:
            selector_result=Selector().select(context.task,context.investigation,candidates,node.assigned_backend,self.backends[node.assigned_backend],context.run_dir,estimate_cost=(context.mode!='performance'))
        except TypeError:
            selector_result=Selector().select(context.task,context.investigation,candidates,node.assigned_backend,self.backends[node.assigned_backend],context.run_dir)
        if len(selector_result) == 2:
            selection,call=selector_result; normalization_notes=[]
        else:
            selection,call,normalization_notes=selector_result
        eligible_ids={a['attempt_id'] for a in context.attempts if a.get('acceptance_eligible')}
        fallback=selection.fallback_used
        if selection.decision=='select' and selection.selected_attempt_id not in eligible_ids:
            selection=deterministic_fallback(candidates, f'Selector selected invalid or ineligible candidate {selection.selected_attempt_id}.'); selection.selector_backend=node.assigned_backend; write_json_utf8(context.run_dir/'selection.json', selection); fallback=True
        for note in (normalization_notes or []): self.record_controller_step(context,node=node,action='selector_normalized',status='normalized',summary=note)
        if getattr(selection, 'selector_reason_synthesized', False): self.record_controller_step(context,node=node,action='selector_reason_synthesized',status='normalized',summary=(selection.reasons or [''])[0])
        if fallback: self.record_controller_step(context,node=node,action='selector_fallback_used',status='fallback',summary=selection.selector_fallback_reason or selection.fallback_reason or selection.summary)
        self.progress_reporter.selector_completed(selection, normalization_notes)
        context.selection=selection; context.winner=next((a for a in context.attempts if a.get('attempt_id')==selection.selected_attempt_id and a.get('acceptance_eligible')),None) if selection.decision=='select' else None
        raw=Path(context.run_dir/'selection.raw.txt').read_text(errors='replace') if (context.run_dir/'selection.raw.txt').exists() else ''
        self._write_node_artifacts(context,node,raw=raw)
        data=selection.model_dump(mode='json')|{'selector_fallback_used':fallback}
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=selection.summary,confidence=selection.confidence,artifacts={'raw':'selection.raw.txt','normalized':'selection_normalized.json','input':'selection_input.json','output':'selection.json','selection':str(context.run_dir/'selection.json'),'selection_input':str(context.run_dir/'selection_input.json')},data=data),data)

    def _execute_verify_node(self,node,context):
        context.graph.mark_running(node.id); accepted=bool(context.winner); data={'accepted':accepted,'winner':context.winner.get('attempt_id') if context.winner else None}; self._write_node_artifacts(context,node,inp={'selection':context.selection.model_dump(mode='json') if context.selection else None})
        reason=self._failure_reason(context) if not accepted else 'Accepted selected candidate.'
        self.record_controller_step(context,node=node,action='final_decision_accepted' if accepted else 'final_decision_failed',status='accepted' if accepted else 'failed',summary=data['winner'] or reason)
        self.progress_reporter.final_decision(accepted, data['winner'], reason)
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary='Accepted selected candidate.' if accepted else reason,artifacts={'verification':str(context.run_dir/'nodes'/node.id/'output.json')},data=data),data)

    def _compact_controller_steps(self, context):
        p=context.run_dir/'controller_steps.jsonl'
        if not p.exists(): return []
        steps=[]
        for line in p.read_text().splitlines():
            if not line.strip(): continue
            try: rec=json.loads(line)
            except Exception: continue
            compact={k:rec.get(k) for k in ['node_id','node_kind','action','status','backend','model','summary'] if rec.get(k) is not None}
            steps.append(compact)
        return steps


    def _failure_reason(self, context):
        eligible=[a.get('attempt_id') for a in context.attempts if a.get('acceptance_eligible')]
        sel=context.selection
        if not eligible: return 'No candidate passed acceptance gates.'
        if sel and sel.decision=='reject_all' and (sel.summary or sel.reasons): return f'Selector intentionally rejected all candidates despite eligibility: {sel.summary or "; ".join(sel.reasons)}'
        if sel and sel.fallback_used and not sel.selected_attempt_id: return 'Eligible candidates existed, but selector did not return a valid eligible selected_attempt_id and fallback could not select a winner.'
        return 'Eligible candidates existed, but selector did not return a valid eligible selected_attempt_id and fallback could not select a winner.'

    def _finalize(self, context):
        accepted=bool(context.winner); apply_opts={}
        if accepted: apply_opts={'apply_command':f'villani-ops apply {context.run_id}','branch_command':f'villani-ops branch {context.run_id} --name villani-ops/{context.run_id}','pr_command':f'villani-ops pr {context.run_id} --title "{(context.task.objective or "Villani Ops changes")[:60]}"'}
        decision=Decision(run_id=context.run_id, mode=context.mode, runner=context.runner, orchestration_graph_path=str(context.run_dir/'orchestration_graph.json'), node_backend_assignments={n.id:n.assigned_backend for n in context.graph.nodes if n.assigned_backend}, plan=context.task_context.plan, decomposition=context.task_context.decomposition, performance_backend_name=(next((n.assigned_backend for n in context.graph.nodes if n.assigned_backend),None) if context.mode=='performance' else None), performance_backend_model=(next((n.assigned_model for n in context.graph.nodes if n.assigned_model),None) if context.mode=='performance' else None), accepted=accepted, lifecycle_completed=True, final_state='accepted' if accepted else 'failed', final_action='accept' if accepted else 'fail', winning_attempt_id=context.winner.get('attempt_id') if context.winner else None, winning_worktree_path=context.winner.get('worktree_path') if context.winner else None, winning_patch_path=context.winner.get('patch_path') if context.winner else None, reviewer_decision=(context.winner.get('review') or {}).get('decision') if context.winner else None, reviewer_score=(context.winner.get('review') or {}).get('score') if context.winner else None, classification=context.task_context.classification, investigation=context.task_context.investigation, selection=context.selection.model_dump(mode='json') if context.selection else None, selected_attempt_id=context.selection.selected_attempt_id if accepted and context.selection else None, candidate_attempts_requested=context.candidate_attempts, candidate_attempts_completed=len(context.attempts), eligible_candidate_attempts=[a['attempt_id'] for a in context.attempts if a.get('acceptance_eligible')], orchestration_summary=json.dumps(context.graph.summary()), total_cost=0 if context.mode=='performance' else sum(context.costs.values()), coding_cost=0 if context.mode=='performance' else context.costs['coding'], classification_cost=0 if context.mode=='performance' else context.costs['classification'], review_cost=0 if context.mode=='performance' else context.costs['review'], total_input_tokens=context.input_tokens, total_output_tokens=context.output_tokens, total_coding_input_tokens=sum(a.get('input_tokens') or 0 for a in context.attempts), total_coding_output_tokens=sum(a.get('output_tokens') or 0 for a in context.attempts), token_accounting_statuses=dict(Counter(a.get('token_accounting_status') or 'missing' for a in context.attempts)), attempts=context.attempts, warnings=context.warnings, apply_options=apply_opts, controller_steps=self._compact_controller_steps(context), controller_steps_path='controller_steps.jsonl', acceptance_blockers=[] if accepted else [b for a in context.attempts for b in (a.get('acceptance_blockers') or [])], attempts_used=len(context.attempts), all_attempted_backends=[a.get('backend_name') for a in context.attempts], failure_reason='' if accepted else self._failure_reason(context), reason='Selected eligible candidate.' if accepted else self._failure_reason(context), total_attempts=len(context.attempts))
        write_json(context.run_dir/'decision.json', decision.model_dump(mode='json')); context.final_decision=decision; return decision

    def record_controller_step(self, context, *, node=None, action:str, status:str, summary:str|None=None, details:dict|None=None):
        p=context.run_dir/'controller_steps.jsonl'; rec={'timestamp':_now(),'run_id':context.run_id,'node_id':getattr(node,'id',None),'node_kind':getattr(node,'kind',None),'action':action,'status':status,'backend':getattr(node,'assigned_backend',None),'model':getattr(node,'assigned_model',None),'summary':summary,'details':details or {}}
        p.parent.mkdir(parents=True,exist_ok=True); p.open('a', encoding='utf-8').write(json.dumps(rec, ensure_ascii=False, default=str)+'\n')
