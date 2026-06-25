from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from dataclasses import dataclass, field
import json, secrets, time, subprocess, sys, os, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from villani_ops.orchestration.planner import build_fixed_graph, Planner, validate_decomposition_plan, revise_decomposition_plan, semantic_validate_decomposition_plan
from villani_ops.orchestration.artifacts import write_json, write_json_utf8, write_text_utf8
from villani_ops.orchestration.nodes import OrchestrationNode, NodeExecutionResult, NodeResult
from villani_ops.orchestration.context import TaskContext
from villani_ops.orchestration.scheduler import GraphScheduler
from villani_ops.llm.client import LLMClient
from villani_ops.core.concurrency import BackendConcurrencyLimiter

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
    subtasks:list[dict]=field(default_factory=list); accepted_subtasks:list[dict]=field(default_factory=list); rejected_subtasks:list[dict]=field(default_factory=list)
    integration:dict[str,Any]=field(default_factory=dict); decomposed_active:bool=False
    parallel_execution:dict[str,Any]=field(default_factory=dict); controller_step_lock:Any=field(default_factory=threading.Lock)

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

def _subtask_prompt(task: Task, decomposition, st: dict, attempt_idx: int | None = None, attempts_total: int | None = None, failure_memory: str = '') -> str:
    siblings=[]
    summary=[]
    for x in (getattr(decomposition,'subtasks',[]) or []):
        sid=getattr(x,'id','')
        title=getattr(x,'title','')
        obj=getattr(x,'objective','')
        summary.append(f"- {sid}: {title} - {obj}")
        if sid and sid != st.get('id'):
            siblings.append(f"- {sid}")
    relevant=st.get('relevant_files') or []
    return f"""You are executing exactly one decomposed subtask.

Do not solve the full original task.

Do not fix unrelated subtasks.

Only modify files needed for this subtask.

Prefer modifying the listed relevant files only.

If you believe another file must be changed to complete this subtask, explain why in your final output.

If this subtask depends on another unfixed subtask, make the smallest local change possible and leave broader integration to the integration stage.

The final integration stage will combine subtask patches and run the full test suite.

Original task:
{task.objective or task.instruction or ''}

Overall success criteria:
{task.success_criteria or ''}

Full decomposition summary:
{chr(10).join(summary)}

Current subtask objective:
{subtask_label(st)}

Current subtask relevant files:
{chr(10).join(relevant) if relevant else '(none listed)'}

Out-of-scope sibling subtasks:
{chr(10).join(siblings) if siblings else '(none)'}

Integration stage responsibility:
The final integration stage will combine subtask patches, resolve integration glue, and run the full test suite.

Subtask id:
{st.get('id')}

Subtask title:
{st.get('title')}

Subtask success criteria:
{st.get('success_criteria') or ''}

Dependencies:
{', '.join(st.get('dependencies') or [])}
{f'''
Subtask attempt:
attempt {attempt_idx} of {attempts_total}

Previous subtask-local failure context:
{failure_memory}
''' if attempt_idx and attempts_total else ''}
"""

def subtask_label(st: dict) -> str:
    return f"{st.get('id')}: {st.get('objective') or st.get('title') or ''}"

def _subtask_review_prompt(task: Task, st: dict, siblings: list[dict], attempt_idx: int | None = None, attempts_total: int | None = None) -> str:
    return f"""Evaluate this patch only against the current subtask objective.

Do not fail this patch because unrelated sibling subtasks remain unfixed.

Do check whether the patch overreached into unrelated subtasks.

Do check whether changed files are consistent with the subtask relevant files.

Do check whether the patch creates integration risk.

Original task: {task.objective or task.instruction or ''}
Subtask id: {st.get('id')}
Attempt number: {attempt_idx or ''}
Attempts remaining: {max((attempts_total or attempt_idx or 1) - (attempt_idx or 1), 0)}
Current subtask objective: {subtask_label(st)}
Current subtask relevant files: {', '.join(st.get('relevant_files') or []) or '(none listed)'}
Out-of-scope sibling subtasks: {', '.join(s.get('id','') for s in siblings if s.get('id') != st.get('id')) or '(none)'}
Return review JSON with subtask_passed, scope_ok, integration_risk, recommended_action, score, summary, evidence, and issues.
"""
def _selector_candidate_payload(a: dict) -> dict:
    review=a.get('review') or {}
    return {'attempt_id':a.get('attempt_id'),'backend_name':a.get('backend_name'),'model':a.get('model'),'status':a.get('status'),'exit_code':a.get('exit_code'),'changed_files':a.get('changed_files') or [],'git_status':a.get('git_status') or '','patch_text':_patch_text(a.get('patch_path')),'stdout_tail':_tail(a.get('stdout_path')),'stderr_tail':_tail(a.get('stderr_path')),'review':review,'review_score':review.get('score'),'review_summary':review.get('summary') or '','review_evidence':review.get('evidence') or [],'review_issues':review.get('issues') or [],'review_recommended_action':review.get('recommended_action') or '','acceptance_eligible':bool(a.get('acceptance_eligible')),'acceptance_blockers':a.get('acceptance_blockers') or []}

class OrchestrationEngine:
    def __init__(self, *, backends, execution_policy, runner_adapter, llm_client=None, workspace: Path, non_interactive: bool=False, progress_reporter=None, storage: FileStorage|None=None) -> None:
        self.backends=backends; self.backend_limiter=BackendConcurrencyLimiter(backends); self.execution_policy=execution_policy; self.runner_adapter=runner_adapter; self.llm_client=llm_client or LLMClient(); self.workspace=Path(workspace); self.non_interactive=non_interactive; self.progress_reporter=progress_reporter or RunProgressReporter(False); self.storage=storage or FileStorage(self.workspace)

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
            subtask_code=[n for n in ready_nodes if context.decomposed_active and n.kind=='code' and n.id.startswith('subtask_')]
            if len(subtask_code) > 1:
                self._execute_parallel_subtask_code_nodes(subtask_code, context)
                graph.write(run_dir/'orchestration_graph.json')
                continue
            candidate_code=[n for n in ready_nodes if not context.decomposed_active and n.kind=='code' and n.id.startswith('code_attempt_')]
            if len(candidate_code) > 1 and context.candidate_attempts > 1:
                self._execute_parallel_candidate_code_nodes(candidate_code, context)
                graph.write(run_dir/'orchestration_graph.json')
                continue
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
        dispatch={'classify':self._execute_classify_node,'investigate':self._execute_investigate_node,'plan':self._execute_plan_node,'decompose':self._execute_decompose_node,'code':self._execute_code_node,'review':self._execute_review_node,'integrate':self._execute_integrate_node,'integration_validate':self._execute_integration_validate_node,'integration_repair':self._execute_integration_repair_node,'final_review':self._execute_final_review_node,'select':self._execute_select_node,'verify':self._execute_verify_node}
        if node.kind not in dispatch: raise ValueError(f'Unknown orchestration node kind: {node.kind}')
        self.assign_backend_for_node(node=node, context=context)
        self.record_controller_step(context, node=node, action='node_started', status='running')
        self.progress_reporter.node_started(node)
        try: return dispatch[node.kind](node, context)
        except Exception as e:
            context.graph.mark_failed(node.id, str(e)); self._write_node_artifacts(context, node, out={'error':str(e)}); self.record_controller_step(context,node=node,action='node_failed',status='failed',summary=str(e)); self.progress_reporter.node_failed(node, str(e)); return NodeExecutionResult(node_id=node.id,status='failed',error=str(e))



    def _execute_parallel_candidate_code_nodes(self, nodes, context):
        cdir=context.run_dir/'candidates'; cdir.mkdir(parents=True, exist_ok=True)
        for node in nodes:
            self.assign_backend_for_node(node=node, context=context)
            self.record_controller_step(context, node=node, action='candidate_scheduled', status='scheduled', details={'parallel_group':'candidate_code','max_parallel':self.backends[node.assigned_backend].max_parallel})
        context.parallel_execution={'enabled': True, 'candidate_attempts': context.candidate_attempts, 'max_parallel_by_backend': {n:self.backends[n].max_parallel for n in sorted({x.assigned_backend for x in nodes if x.assigned_backend})}, 'started_attempts': [], 'completed_attempts': [], 'max_observed_parallelism': 0, 'results': []}
        write_json_utf8(cdir/'parallel_execution.json', context.parallel_execution)
        max_workers=max(1, min(len(nodes), sum(self.backends[n.assigned_backend].max_parallel for n in nodes if n.assigned_backend)))
        active={'count':0,'max':0}; active_lock=threading.Lock()
        self.record_controller_step(context, action='parallel_group_started', status='running', details={'parallel_group':'candidate_code','nodes':[n.id for n in nodes]})
        def run_one(node):
            b=self.backends[node.assigned_backend]
            def body():
                with active_lock:
                    active['count'] += 1; active['max']=max(active['max'], active['count'])
                    context.parallel_execution['max_observed_parallelism']=max(context.parallel_execution.get('max_observed_parallelism',0), active['max'])
                    context.parallel_execution.setdefault('started_attempts', []).append(node.id)
                    write_json_utf8(cdir/'parallel_execution.json', context.parallel_execution)
                self.record_controller_step(context, node=node, action='backend_parallel_slot_acquired', status='running', details={'parallel_group':'candidate_code','max_parallel':b.max_parallel})
                try:
                    return self._execute_code_node(node, context)
                finally:
                    with active_lock:
                        active['count'] -= 1
                    self.record_controller_step(context, node=node, action='backend_parallel_slot_released', status='released', details={'parallel_group':'candidate_code','max_parallel':b.max_parallel})
            return self.backend_limiter.run(node.assigned_backend, body)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures={pool.submit(run_one,n): n for n in nodes}
            for fut in as_completed(futures):
                node=futures[fut]
                try: res=fut.result()
                except Exception as e:
                    context.graph.mark_failed(node.id, str(e)); res=NodeExecutionResult(node_id=node.id,status='failed',error=str(e))
                meta=node.result or {}
                context.parallel_execution.setdefault('completed_attempts', []).append(node.id)
                context.parallel_execution.setdefault('results', []).append({'node_id': node.id, 'backend': node.assigned_backend, 'status': res.status, 'review_status': None, 'attempt_id': meta.get('attempt_id'), 'worktree_path': meta.get('worktree_path'), 'artifacts_dir': str(context.run_dir/'attempts'/(meta.get('attempt_id') or node.id.replace('code_',''))), 'started_at': meta.get('started_at') or node.started_at, 'completed_at': meta.get('completed_at') or node.completed_at})
                context.parallel_execution['max_observed_parallelism']=max(context.parallel_execution.get('max_observed_parallelism',0), active['max'])
                write_json_utf8(cdir/'parallel_execution.json', context.parallel_execution)
                context.graph.write(context.run_dir/'orchestration_graph.json')
        self.record_controller_step(context, action='parallel_group_completed', status='completed', details={'parallel_group':'candidate_code','max_observed_parallelism':active['max']})

    def _execute_parallel_subtask_code_nodes(self, nodes, context):
        for node in nodes:
            self.assign_backend_for_node(node=node, context=context)
            self.record_controller_step(context, node=node, action='subtask_scheduled', status='scheduled', details={'parallel_group':'decomposed_subtasks','max_parallel':self.backends[node.assigned_backend].max_parallel})
        max_workers=max(1, min(len(nodes), sum(self.backends[n.assigned_backend].max_parallel for n in nodes if n.assigned_backend)))
        self.record_controller_step(context, action='parallel_group_started', status='running', details={'parallel_group':'decomposed_subtasks','nodes':[n.id for n in nodes]})
        active={'count':0,'max':0}; active_lock=threading.Lock()
        def run_one(node):
            b=self.backends[node.assigned_backend]
            def body():
                with active_lock:
                    active['count'] += 1; active['max']=max(active['max'], active['count'])
                self.record_controller_step(context, node=node, action='backend_parallel_slot_acquired', status='running', details={'parallel_group':'decomposed_subtasks','max_parallel':b.max_parallel})
                self.record_controller_step(context, node=node, action='subtask_started', status='running', details={'parallel_group':'decomposed_subtasks','max_parallel':b.max_parallel})
                try:
                    return self._execute_code_node(node, context)
                finally:
                    with active_lock:
                        active['count'] -= 1
                    self.record_controller_step(context, node=node, action='backend_parallel_slot_released', status='released', details={'parallel_group':'decomposed_subtasks','max_parallel':b.max_parallel})
            try:
                return self.backend_limiter.run(node.assigned_backend, body)
            except Exception as e:
                context.graph.mark_failed(node.id, str(e)); return NodeExecutionResult(node_id=node.id,status='failed',error=str(e))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures={pool.submit(run_one,n): n for n in nodes}
            for fut in as_completed(futures):
                node=futures[fut]
                try: res=fut.result()
                except Exception as e:
                    context.graph.mark_failed(node.id, str(e)); res=NodeExecutionResult(node_id=node.id,status='failed',error=str(e))
                self.record_controller_step(context, node=node, action='subtask_completed', status=res.status, details={'parallel_group':'decomposed_subtasks','max_parallel':self.backends[node.assigned_backend].max_parallel})
                meta=node.result or {}
                if context.parallel_execution:
                    context.parallel_execution.setdefault('scheduled', []).append({'subtask_id': meta.get('subtask_id'), 'node_id': node.id, 'backend': node.assigned_backend, 'started_at': meta.get('started_at') or node.started_at, 'completed_at': meta.get('completed_at') or node.completed_at, 'status': res.status})
                    context.parallel_execution['max_observed_concurrency']=max(context.parallel_execution.get('max_observed_concurrency',0), active['max'])
                    self._write_parallel_execution_summary(context)
                context.graph.write(context.run_dir/'orchestration_graph.json')
        self.record_controller_step(context, action='parallel_group_completed', status='completed', details={'parallel_group':'decomposed_subtasks','max_observed_concurrency':active['max']})

    def _write_parallel_execution_summary(self, context):
        data=context.parallel_execution or {'enabled': False, 'reason': 'Backend max_parallel is 1'}
        if not data.get('enabled'):
            data=data | {'enabled': False, 'reason': data.get('reason') or 'Backend max_parallel is 1'}
        write_json_utf8(context.run_dir/'decomposition'/'parallel_execution.json', data)

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
        write_json_utf8(context.run_dir/'investigation.json', inv);
        if getattr(inv, 'validation_plan', None):
            (context.run_dir/'investigation').mkdir(exist_ok=True)
            write_json_utf8(context.run_dir/'investigation'/'validation_plan.json', inv.validation_plan)
        context.investigation=inv; context.task_context.investigation=inv.model_dump(mode='json')
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
        ddir=context.run_dir/'decomposition'; ddir.mkdir(parents=True, exist_ok=True)
        initial=validate_decomposition_plan(dec, task=context.task.objective or context.task.instruction, success_criteria=context.task.success_criteria, backends=self.backends)
        semantic, sem_meta=semantic_validate_decomposition_plan(self.llm_client, dec, task=context.task.objective or context.task.instruction, success_criteria=context.task.success_criteria, deterministic=initial, backend=self.backends[node.assigned_backend])
        write_json_utf8(ddir/'plan_validation_initial.json', {'deterministic': initial.model_dump(mode='json'), 'semantic': semantic.model_dump(mode='json') if semantic else None, 'semantic_status': sem_meta})
        write_json_utf8(ddir/'plan_validation_semantic_initial.json', sem_meta | ({'result': semantic.model_dump(mode='json')} if semantic else {}))
        validation=semantic or initial
        decision='decomposition_accepted' if (initial.accepted and semantic is not None and semantic.accepted) else 'decomposition_rejected_fallback_to_candidates'
        if not initial.accepted:
            decision='decomposition_rejected_fallback_to_candidates'
        if initial.accepted and (semantic is None or not semantic.accepted):
            revised=revise_decomposition_plan(dec, semantic or initial)
            det2=validate_decomposition_plan(revised, task=context.task.objective or context.task.instruction, success_criteria=context.task.success_criteria, backends=self.backends)
            sem2, sem2_meta=semantic_validate_decomposition_plan(self.llm_client, revised, task=context.task.objective or context.task.instruction, success_criteria=context.task.success_criteria, deterministic=det2, backend=self.backends[node.assigned_backend])
            validation=sem2 or det2
            write_json_utf8(ddir/'plan_validation_revised.json', {'deterministic': det2.model_dump(mode='json'), 'semantic': sem2.model_dump(mode='json') if sem2 else None, 'semantic_status': sem2_meta})
            if det2.accepted and sem2 is not None and sem2.accepted:
                dec=revised; context.decomposition=dec; decision='decomposition_revised_and_accepted'
            else:
                decision='decomposition_rejected_fallback_to_candidates'
        write_json_utf8(ddir/'plan_validation_decision.json', {'decision':decision,'accepted':decision in {'decomposition_accepted','decomposition_revised_and_accepted'},'deterministic_accepted':initial.accepted,'semantic_available':semantic is not None,'required_revisions':validation.required_revisions, 'final_decision': 'accepted' if decision in {'decomposition_accepted','decomposition_revised_and_accepted'} else 'rejected'})
        validation.accepted = decision in {'decomposition_accepted','decomposition_revised_and_accepted'}
        context.task_context.decomposition=dec.model_dump(mode='json') | {'plan_validation': validation.model_dump(mode='json'), 'plan_validation_decision': decision}
        usable=[s.model_dump(mode='json') for s in (dec.subtasks or []) if getattr(s,'id',None) and getattr(s,'objective',None)]
        if getattr(dec,'should_use_decomposition',False) and len(usable) >= 2 and validation.accepted:
            context.decomposed_active=True; dec.advisory_only=False; context.task_context.decomposition=dec.model_dump(mode='json') | {'plan_validation': validation.model_dump(mode='json'), 'plan_validation_decision': decision}; context.subtasks=usable
            keep=[n for n in context.graph.nodes if n.id in {'classify','investigate','plan','decompose'}]
            new=[]
            ids={st['id'] for st in usable}
            for st in usable:
                cid=f"subtask_{st['id']}_code"; rid=f"subtask_{st['id']}_acceptance_summary"
                deps=[f"subtask_{d}_acceptance_summary" for d in (st.get('dependencies') or []) if d in ids] or ['decompose']
                new.append(OrchestrationNode(id=cid, kind='code', objective=f"Execute decomposed subtask {st['id']}: {st.get('title')}", dependencies=deps, parallel_group='subtask_code', runner=context.runner))
                new.append(OrchestrationNode(id=rid, kind='review', objective=f"Summarize accepted/failed status for decomposed subtask {st['id']}.", dependencies=[cid], parallel_group='subtask_review'))
            review_ids=[f"subtask_{st['id']}_acceptance_summary" for st in usable]
            new.append(OrchestrationNode(id='integrate_subtasks', kind='integrate', objective='Integrate accepted subtask patches.', dependencies=review_ids))
            new.append(OrchestrationNode(id='integration_validate', kind='integration_validate', objective='Validate integrated patch.', dependencies=['integrate_subtasks']))
            new.append(OrchestrationNode(id='integration_repair', kind='integration_repair', objective='Repair integrated patch once if needed.', dependencies=['integration_validate']))
            new.append(OrchestrationNode(id='final_review', kind='final_review', objective='Review final integrated patch.', dependencies=['integration_repair']))
            new.append(OrchestrationNode(id='verify', kind='verify', objective='Make final acceptance decision and write artifacts.', dependencies=['final_review']))
            context.graph.nodes=keep+new; context.graph.edges=[(d,n.id) for n in context.graph.nodes for d in n.dependencies]
            limits={n: b.max_parallel for n,b in self.backends.items()}
            context.parallel_execution={'enabled': any(v>1 for v in limits.values()), 'backend_limits': limits, 'subtasks_total': len(usable), 'max_observed_concurrency': 0, 'scheduled': []}
            selected=node.assigned_backend or next(iter(self.backends))
            self.progress_reporter.step(f'[5/8] Running decomposed subtasks with {selected}, max_parallel={self.backends[selected].max_parallel}, attempts_per_subtask={context.candidate_attempts}...')
            self._write_parallel_execution_summary(context)
        elif usable:
            d=context.task_context.decomposition; d['advisory_only']=True; context.task_context.decomposition=d
        elif getattr(dec,'should_use_decomposition',False):
            reason=('Decomposition plan validation failed; falling back to candidate path.' if not validation.accepted else 'Planner requested decomposition but decomposition produced no executable subtasks.')
            d=context.task_context.decomposition
            d.update({'decomposition_requested': True, 'decomposition_executed': False, 'decomposition_fallback_to_candidate_path': True, 'decomposition_fallback_used': True, 'fallback_used': True, 'decomposition_fallback_reason': reason})
            context.task_context.decomposition=d
            dec.decomposition_fallback_used=True; dec.fallback_used=True; dec.decomposition_fallback_reason=reason
            self.record_controller_step(context,node=node,action='decomposition_fallback_to_candidate_path',status='fallback',summary=reason)
            self.progress_reporter.step('[4/8] Decomposition fallback to candidate path: no executable subtasks produced')
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=(getattr(dec,'decomposition_fallback_reason',None) or dec.reason),confidence=dec.confidence,artifacts={'raw':'decomposition.raw.txt','normalized':'decomposition_normalized.json','decomposition':'decomposition.json','output':str(context.run_dir/'nodes'/node.id/'output.json')}),context.task_context.decomposition)

    def _failure_memory_for_subtask_retry(self, meta: dict) -> str:
        blockers=meta.get('acceptance_blockers') or []
        review=meta.get('review') or {}
        if not meta.get('has_patch') or 'changed files are missing' in blockers:
            return 'Previous attempt failed because it produced no patch / no changed files. You must produce a concrete code change for this subtask unless it is already fully satisfied.'
        if not review.get('scope_ok', True) or any('scope overreach' in b for b in blockers):
            return 'Previous attempt changed files outside the subtask scope. Stay within relevant files unless absolutely necessary and explain why.'
        return 'Previous attempt did not pass validation/review. Use the review issues below.\n' + json.dumps({'blockers': blockers, 'issues': review.get('issues') or [], 'summary': review.get('summary')}, indent=2)

    def _attempt_summary_row(self, meta: dict) -> dict:
        review=meta.get('review') or {}
        return {'attempt_id':meta.get('attempt_id'),'status':'accepted' if meta.get('acceptance_eligible') else 'rejected','review_decision':review.get('decision'),'review_score':review.get('score'),'eligible':bool(meta.get('acceptance_eligible')),'blockers':meta.get('acceptance_blockers') or []}

    def _write_subtask_summary(self, context, sid: str, attempts: list[dict], accepted: dict | None, requested: int) -> dict:
        sdir=context.run_dir/'subtasks'/sid
        summary={'subtask_id':sid,'attempts_requested':requested,'attempts_completed':len(attempts),'accepted_attempt_id':accepted.get('attempt_id') if accepted else None,'accepted_patch_path':accepted.get('patch_path') if accepted else None,'accepted_worktree_path':accepted.get('worktree_path') if accepted else None,'accepted_changed_files':accepted.get('changed_files') if accepted else [],'status':'accepted' if accepted else 'failed','failure_reason':None if accepted else f'No accepted attempt after {requested} attempts.','attempts':[self._attempt_summary_row(a) for a in attempts]}
        write_json_utf8(sdir/'subtask_summary.json', summary)
        all_summaries=[]
        for st in context.subtasks:
            sp=context.run_dir/'subtasks'/st['id']/'subtask_summary.json'
            if sp.exists():
                try: all_summaries.append(json.loads(sp.read_text()))
                except Exception: pass
        write_json_utf8(context.run_dir/'decomposition'/'subtask_attempts.json', {'attempts_per_subtask':requested,'subtasks_total':len(context.subtasks),'subtasks_accepted':sum(1 for x in all_summaries if x.get('status')=='accepted'),'subtasks_failed':sum(1 for x in all_summaries if x.get('status')=='failed'),'subtask_attempts_completed':sum(int(x.get('attempts_completed') or 0) for x in all_summaries),'subtasks':all_summaries})
        return summary


    def _accepted_dependency_order(self, context, sid: str) -> list[dict]:
        by_id={st.get('id'): st for st in context.subtasks}
        accepted={a.get('subtask_id'): a for a in context.accepted_subtasks if a.get('acceptance_eligible')}
        seen=set(); order=[]
        def visit(x):
            for d in by_id.get(x, {}).get('dependencies') or []:
                if d in seen: continue
                visit(d); seen.add(d)
                if d in accepted: order.append(accepted[d])
        visit(sid)
        return order

    def _materialize_dependency_patches(self, context, sid: str, aid: str, worktree_path: str, adir: Path) -> dict:
        deps=self._accepted_dependency_order(context, sid)
        rows=[]; status='ok'
        for dep in deps:
            pp=dep.get('patch_path')
            row={'subtask_id':dep.get('subtask_id'),'attempt_id':dep.get('attempt_id'),'patch_path':pp,'status':'pending'}
            if not pp or not Path(pp).exists():
                row.update({'status':'failed','stderr':'accepted dependency patch missing'}); status='failed'; rows.append(row); break
            proc=subprocess.run(['git','apply','--whitespace=nowarn',str(Path(pp).resolve())], cwd=worktree_path, text=True, capture_output=True)
            row.update({'stdout':proc.stdout,'stderr':proc.stderr,'status':'applied' if proc.returncode==0 else 'failed','exit_code':proc.returncode})
            rows.append(row)
            if proc.returncode != 0: status='failed'; break
        artifact={'subtask_id':sid,'attempt_id':aid,'applied_dependencies':rows,'status':status}
        write_json_utf8(adir/'applied_dependencies.json', artifact)
        return artifact

    def _run_subtask_attempt(self, node, context, sid: str, st: dict, subtask_idx: int, attempt_idx: int, failure_memory: str) -> dict:
        aid=f"attempt_{attempt_idx:03d}"; adir=context.run_dir/'subtasks'/sid/'attempts'/aid; adir.mkdir(parents=True,exist_ok=True)
        b=self.backends[node.assigned_backend]
        self.progress_reporter.step(f'[5/8] Running subtask {subtask_idx}/{len(context.subtasks)} {sid} attempt {attempt_idx}/{context.candidate_attempts} with {b.name}...')
        prompt=_subtask_prompt(context.task,context.decomposition,st,attempt_idx,context.candidate_attempts,failure_memory)
        meta={'attempt_id':aid,'subtask_id':sid,'subtask_title':st.get('title'),'subtask_objective':st.get('objective'),'backend_name':b.name,'model':b.model,'runner_name':context.runner,'status':'running','started_at':_now()}
        try:
            wt=GitWorktreeIsolation().create(context.repo,context.run_id,f"subtask_{sid}_{aid}",self.storage.workspace); meta.update(wt)
            if st.get('dependencies'):
                dep_art=self._materialize_dependency_patches(context, sid, aid, wt['worktree_path'], adir); meta['applied_dependencies']=dep_art
                if dep_art.get('status') != 'ok':
                    raise RuntimeError('Failed to apply accepted dependency patches before runner execution')
            res=self.runner_adapter.run_task(repo_path=Path(wt['worktree_path']), task=prompt, success_criteria=context.task.success_criteria, backend_name=b.name, backend_config=b, timeout_seconds=context.timeout_seconds or b.timeout_seconds or 1200, context={'attempt_id':aid,'subtask_id':sid,'node_id':node.id}, artifacts_dir=adir)
            write_text_utf8(adir/'stdout.txt', res.stdout); write_text_utf8(adir/'stderr.txt', res.stderr); context.input_tokens+=res.input_tokens; context.output_tokens+=res.output_tokens; context.costs['coding']+=0 if context.mode=='performance' else b.estimate_cost(res.input_tokens,res.output_tokens)
            meta.update({'exit_code':res.exit_code,'stdout_path':str(adir/'stdout.txt'),'stderr_path':str(adir/'stderr.txt'),'duration_ms':res.duration_ms,'input_tokens':res.input_tokens,'output_tokens':res.output_tokens,'token_accounting_status':res.token_accounting_status,'runner_telemetry':res.telemetry}); meta.update(capture_worktree(wt['worktree_path'],adir))
            if meta.get('patch_path'):
                compat=adir/'patch.diff'; write_text_utf8(compat, Path(meta['patch_path']).read_text(encoding='utf-8', errors='replace') if Path(meta['patch_path']).exists() else ''); meta['patch_path']=str(compat)
        except Exception as e:
            meta.update({'exit_code':1,'error':str(e),'changed_files':[],'patch_path':None}); write_text_utf8(adir/'stderr.txt', str(e))
        meta['has_patch']=has_non_empty_patch(meta.get('patch_path')); meta['completed_at']=_now()
        write_json_utf8(adir/'changed_files.json', meta.get('changed_files') or []); write_text_utf8(adir/'git_status.txt', meta.get('git_status') or '')
        self.progress_reporter.step(f'[5/8] Subtask {sid} attempt {attempt_idx} complete: exit={meta.get("exit_code")}, changed_files={len(meta.get("changed_files") or [])}, patch={"yes" if meta["has_patch"] else "no"}')
        self.progress_reporter.step(f'[5/8] Reviewing subtask {sid} attempt {attempt_idx}/{context.candidate_attempts}...')
        rb=self.backends[node.assigned_backend]
        review_input={k:meta.get(k) for k in ['attempt_id','subtask_id','exit_code','stdout_path','stderr_path','changed_files','git_status','runner_telemetry']}; review_input.update({'review_prompt':_subtask_review_prompt(context.task,st,context.subtasks,attempt_idx,context.candidate_attempts),'subtask':st,'patch':_patch_text(meta.get('patch_path'),50000),'stdout_summary':_tail(meta.get('stdout_path'),4000),'stderr_summary':_tail(meta.get('stderr_path'),4000)})
        try:
            review,call=LLMReviewer().review(context.task,context.classification,rb,review_input,self.backends,adir/'review.json',backend_override=rb,estimate_cost=(context.mode!='performance'))
            context.costs['review']+=0 if context.mode=='performance' else call.estimated_cost; context.input_tokens+=call.input_tokens; context.output_tokens+=call.output_tokens
        except TypeError:
            review,call=LLMReviewer().review(context.task,context.classification,rb,review_input,self.backends,adir/'review.json',backend_override=rb)
        except Exception as e:
            review=ReviewResult(decision='fail',summary=f'Reviewer failed: {e}',issues=[str(e)],recommended_action='fail'); write_json_utf8(adir/'review.json', review)
        rdata=review.model_dump(mode='json'); rdata.setdefault('subtask_passed', review.decision == 'pass'); rdata.setdefault('scope_ok', True); rdata.setdefault('integration_risk', 'unknown'); write_json_utf8(adir/'review.json', rdata)
        blockers=[]; eligible=False
        if not meta['has_patch']: blockers.append('patch is missing or empty')
        if not meta.get('changed_files'): blockers.append('changed files are missing')
        if meta['has_patch'] and meta.get('changed_files'): eligible,blockers=is_attempt_acceptance_eligible(meta)
        if not rdata.get('scope_ok', True): eligible=False; blockers.append('subtask scope overreach was not approved')
        if str(rdata.get('integration_risk','unknown')).lower() == 'high': eligible=False; blockers.append('subtask integration risk is high')
        if not rdata.get('subtask_passed', review.decision == 'pass'): eligible=False; blockers.append('subtask objective was not passed')
        if review.decision!='pass' or review.recommended_action!='accept': eligible=False
        meta.update({'review':rdata,'acceptance_eligible':eligible,'acceptance_blockers':blockers,'status':'validated' if eligible else 'failed'})
        meta['candidate_summary']=CandidateSummary(attempt_id=aid,backend_name=meta.get('backend_name'),model=meta.get('model'),status=meta['status'],exit_code=meta.get('exit_code'),changed_files=meta.get('changed_files') or [],patch_path=meta.get('patch_path'),review_decision=review.decision,review_score=review.score,review_recommended_action=review.recommended_action,review_summary=review.summary,review_issues=review.issues,acceptance_eligible=eligible,acceptance_blockers=blockers,has_patch=meta['has_patch'], telemetry=meta.get('runner_telemetry') or {}).model_dump(mode='json')
        write_json_utf8(adir/'attempt.json', meta)
        self.progress_reporter.step(f'[5/8] Subtask review complete: {review.decision}/{review.recommended_action}, eligible={str(eligible).lower()}')
        return meta

    def _execute_code_node(self,node,context):
        context.graph.mark_running(node.id)
        if node.id.startswith('subtask_'):
            sid=node.id[len('subtask_'):-len('_code')]; idx=next((i for i,s in enumerate(context.subtasks,1) if s['id']==sid),1); st=next((s for s in context.subtasks if s['id']==sid),{})
            attempts=[]; accepted=None; failure_memory=''
            for attempt_idx in range(1, context.candidate_attempts+1):
                meta=self._run_subtask_attempt(node, context, sid, st, idx, attempt_idx, failure_memory)
                attempts.append(meta)
                if meta.get('acceptance_eligible'):
                    accepted=meta; context.accepted_subtasks.append(meta)
                    self.progress_reporter.step(f'[5/8] Subtask {sid} accepted on attempt {attempt_idx}/{context.candidate_attempts}')
                    break
                failure_memory=self._failure_memory_for_subtask_retry(meta)
                if attempt_idx < context.candidate_attempts:
                    self.progress_reporter.step(f'[5/8] Scheduling retry for {sid}: attempt {attempt_idx+1}/{context.candidate_attempts}')
            if not accepted:
                context.rejected_subtasks.append(attempts[-1] if attempts else {'subtask_id':sid})
                self.progress_reporter.step(f'[5/8] Subtask {sid} failed after {context.candidate_attempts} attempts')
            summary=self._write_subtask_summary(context, sid, attempts, accepted, context.candidate_attempts)
            node.result={'subtask_id':sid, **summary, 'attempt_artifacts':[a.get('patch_path') for a in attempts]}
            return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=f'Subtask {sid} {summary["status"]}',artifacts={'subtask_summary':str(context.run_dir/'subtasks'/sid/'subtask_summary.json')},data=node.result),node.result)
        else:
            aid=node.id.replace('code_',''); idx=int(aid.split('_')[-1]); st=None; adir=context.run_dir/'attempts'/aid; total=context.candidate_attempts
        adir.mkdir(parents=True,exist_ok=True); b=self.backends[node.assigned_backend]
        if st: self.progress_reporter.step(f'[5/8] Running subtask {idx}/{total} {sid} with {b.name}...')
        else: self.progress_reporter.candidate_started(aid, idx, total, b.name)
        prompt=_subtask_prompt(context.task,context.decomposition,st) if st else _candidate_prompt(context.task,context.investigation,idx,total, context.decomposition)
        meta={'attempt_id':aid,'backend_name':b.name,'model':b.model,'runner_name':context.runner,'status':'running','started_at':_now()}
        if st: meta.update({'subtask_id':sid,'subtask_title':st.get('title'),'subtask_objective':st.get('objective')})
        self._write_node_artifacts(context,node,inp={'prompt':prompt, 'decomposition_context': context.task_context.decomposition})
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
        meta['completed_at']=_now(); write_json_utf8(adir/'attempt.json', meta); node.result=meta
        if st: self.progress_reporter.step(f'[5/8] Subtask {sid} complete: exit={meta.get("exit_code")}, changed_files={len(meta.get("changed_files") or [])}, patch={"yes" if has_non_empty_patch(meta.get("patch_path")) else "no"}')
        else: self.progress_reporter.candidate_completed(aid, idx, total, meta)
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status=status,result_summary=meta.get('error') or f'Candidate {aid} completed',artifacts={'attempt':str(adir/'attempt.json'),'patch':str(meta.get('patch_path') or '')},data=meta,error=meta.get('error')),meta)

    def _execute_review_node(self,node,context):
        context.graph.mark_running(node.id)
        if node.id.startswith('subtask_'):
            suffix='_acceptance_summary' if node.id.endswith('_acceptance_summary') else '_review'; sid=node.id[len('subtask_'):-len(suffix)]; aid=f"subtask_{sid}"; idx=next((i for i,s in enumerate(context.subtasks,1) if s['id']==sid),1); total=len(context.subtasks); cn=context.graph.get(f'subtask_{sid}_code'); adir=context.run_dir/'subtasks'/sid; st=next((s for s in context.subtasks if s['id']==sid),{})
            summary=cn.result or {}
            data={'attempts_requested':summary.get('attempts_requested'),'attempts_completed':summary.get('attempts_completed'),'accepted_attempt_id':summary.get('accepted_attempt_id'),'status':summary.get('status'),'has_review_blocker':summary.get('status')!='accepted','has_acceptance_blocker':summary.get('status')!='accepted'}
            return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=f'Subtask {sid} review summary: {summary.get("status")}',artifacts={'subtask_summary':str(adir/'subtask_summary.json')},data=data),data)
        else:
            aid=node.id.replace('review_',''); idx=int(aid.split('_')[-1]); total=context.candidate_attempts; self.progress_reporter.review_started(aid, idx, total); cn=context.graph.get(f'code_{aid}'); adir=context.run_dir/'attempts'/aid; st=None
        meta=dict(cn.result or {}); rb=self.backends[node.assigned_backend]
        review_input={k:meta.get(k) for k in ['attempt_id','exit_code','stdout_path','stderr_path','changed_files','git_status','runner_telemetry']}; review_input.update({'stdout_summary':_tail(meta.get('stdout_path'),4000),'stderr_summary':_tail(meta.get('stderr_path'),4000),'patch':_patch_text(meta.get('patch_path'),50000),'investigation':context.task_context.investigation});
        if st: review_input.update({'review_prompt': _subtask_review_prompt(context.task, st, context.subtasks), 'subtask': st, 'sibling_subtasks': [s for s in context.subtasks if s.get('id') != st.get('id')]})
        self._write_node_artifacts(context,node,inp=review_input)
        try:
            try:
                review,call=LLMReviewer().review(context.task,context.classification,rb,review_input,self.backends,adir/'review.json',backend_override=rb,estimate_cost=(context.mode!='performance'))
            except TypeError:
                review,call=LLMReviewer().review(context.task,context.classification,rb,review_input,self.backends,adir/'review.json',backend_override=rb)
            context.costs['review']+=0 if context.mode=='performance' else call.estimated_cost; context.input_tokens+=call.input_tokens; context.output_tokens+=call.output_tokens; self._write_node_artifacts(context,node,raw=call.raw_text)
        except Exception as e: review=ReviewResult(decision='fail',summary=f'Reviewer failed: {e}',issues=[str(e)],recommended_action='fail'); write_json_utf8(adir/'review.json', review)
        rdata=review.model_dump(mode='json')
        if st:
            rdata.setdefault('subtask_passed', review.decision == 'pass'); rdata.setdefault('scope_ok', True); rdata.setdefault('integration_risk', 'unknown')
            review.summary=(review.summary or '') + f"\nscope_ok={rdata['scope_ok']} integration_risk={rdata['integration_risk']}"
            rdata=review.model_dump(mode='json') | {'subtask_passed': rdata['subtask_passed'], 'scope_ok': rdata['scope_ok'], 'integration_risk': rdata['integration_risk']}
            write_json_utf8(adir/'review.json', rdata)
        meta['review']=rdata; meta['status']='validated' if meta.get('exit_code')==0 and review.decision=='pass' and review.recommended_action=='accept' else ('failed' if meta.get('exit_code') else 'completed')
        has_patch=has_non_empty_patch(meta.get('patch_path'))
        eligible,blockers=(False, ['patch is missing or empty'] if not has_patch else [])
        if has_patch and meta.get('changed_files'): eligible,blockers=is_attempt_acceptance_eligible(meta)
        elif not meta.get('changed_files'): blockers.append('changed files are missing')
        if st:
            if not bool((meta.get('review') or {}).get('scope_ok', True)):
                eligible=False; blockers.append('subtask scope overreach was not approved')
            if str((meta.get('review') or {}).get('integration_risk','unknown')).lower() == 'high':
                eligible=False; blockers.append('subtask integration risk is high')
            if not bool((meta.get('review') or {}).get('subtask_passed', review.decision == 'pass')):
                eligible=False; blockers.append('subtask objective was not passed')
        meta['has_patch']=has_patch
        meta['acceptance_eligible']=eligible; meta['acceptance_blockers']=blockers
        summary=CandidateSummary(attempt_id=aid,backend_name=meta.get('backend_name'),model=meta.get('model'),status=meta['status'],exit_code=meta.get('exit_code'),changed_files=meta.get('changed_files') or [],patch_path=meta.get('patch_path'),review_decision=review.decision,review_score=review.score,review_recommended_action=review.recommended_action,review_summary=review.summary,review_issues=review.issues,acceptance_eligible=eligible,acceptance_blockers=blockers,has_patch=has_patch, telemetry=meta.get('runner_telemetry') or {})
        meta['candidate_summary']=summary.model_dump(mode='json'); write_json_utf8(adir/'attempt.json', meta)
        if st:
            (context.accepted_subtasks if eligible else context.rejected_subtasks).append(meta)
            self.progress_reporter.step(f'[5/8] Subtask review complete: {review.decision}/{review.recommended_action}, scope_ok={str(meta["review"].get("scope_ok", True)).lower()}, integration_risk={meta["review"].get("integration_risk","unknown")}')
        else:
            context.attempts.append(meta); self.progress_reporter.review_completed(aid, idx, total, {**review.model_dump(mode='json'), 'acceptance_eligible': eligible})
            if context.parallel_execution and context.parallel_execution.get('enabled'):
                for row in context.parallel_execution.get('results', []):
                    if row.get('node_id') == f'code_{aid}': row['review_status']='accepted' if eligible else 'rejected'
                write_json_utf8(context.run_dir/'candidates'/'parallel_execution.json', context.parallel_execution)
        data={'has_review_blocker': review.decision!='pass' or review.recommended_action!='accept','has_acceptance_blocker': bool(blockers),'acceptance_blockers':blockers, **review.model_dump(mode='json')}
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=review.summary,confidence=review.score or 0.0,artifacts={'review':str(adir/'review.json'),'attempt':str(adir/'attempt.json')},data=data),data)

    def _run_cmd(self, args, cwd):
        env=os.environ.copy(); env.update({'GIT_TERMINAL_PROMPT':'0','GIT_EDITOR':'true','PYTHONIOENCODING':'utf-8'})
        p=subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=1200, env=env)
        stderr=p.stderr
        failure_reason=None
        if p.returncode and 'No module named pytest' in (p.stdout + p.stderr):
            failure_reason='environment_failure: pytest is not installed for the active Python executable'
            stderr=(stderr or '') + ('\n' if stderr else '') + failure_reason
        return {'command':args,'python_executable':args[0] if len(args) >= 3 and args[1:3] == ['-m','pytest'] else sys.executable,'exit_code':p.returncode,'stdout':p.stdout,'stderr':stderr,'passed':p.returncode==0,'failure_reason':failure_reason}


    def _analyze_subtask_scope(self, accepted: list[dict], subtasks: list[dict]) -> dict[str, Any]:
        relevant_by_id={st.get('id'): set(st.get('relevant_files') or []) for st in subtasks}
        file_to_subtasks={}
        for st in subtasks:
            for f in st.get('relevant_files') or []:
                file_to_subtasks.setdefault(f,set()).add(st.get('id'))
        changed_to_accepted={}
        rows=[]
        for a in accepted:
            sid=a.get('subtask_id')
            expected=relevant_by_id.get(sid,set())
            changed=set(a.get('changed_files') or [])
            unexpected=sorted(changed-expected) if expected else []
            for f in changed:
                changed_to_accepted.setdefault(f,[]).append(sid)
            sibling_overlap=any((file_to_subtasks.get(f,set())-{sid}) for f in unexpected)
            review=a.get('review') or {}
            risk=str(review.get('integration_risk') or 'unknown').lower()
            over=bool(unexpected)
            approved=bool(review.get('scope_overreach_approved'))
            decision='integrate'
            reason='Patch is scoped to expected files.'
            if over and not approved:
                decision='skip'; reason='Patch changed unexpected files without scope approval.'
            if over and sibling_overlap:
                risk='high'; decision='skip'; reason='Patch changed sibling subtask file without scope approval.'
            rows.append({'subtask_id':sid,'expected_files':sorted(expected),'changed_files':sorted(changed),'unexpected_files':unexpected,'scope_overreach':over,'overlaps_sibling_scope':sibling_overlap,'integration_risk':risk,'integration_decision':decision,'reason':reason})
        overlapping={f:ids for f,ids in changed_to_accepted.items() if len(set(ids))>1}
        return {'subtasks':rows,'overlapping_files':overlapping,'summary':{'accepted':len(accepted),'skipped_for_overreach':sum(1 for r in rows if r['integration_decision']=='skip'),'overlaps':len(overlapping)}}

    def _execute_integrate_node(self,node,context):
        context.graph.mark_running(node.id); idir=context.run_dir/'integration'; idir.mkdir(exist_ok=True)
        wt=GitWorktreeIsolation().create(context.repo,context.run_id,'integration',self.storage.workspace); context.integration={'worktree_path':wt['worktree_path']}
        accepted=list(context.accepted_subtasks); write_json_utf8(idir/'accepted_subtasks.json', accepted)
        failed=[s.get('id') for s in context.subtasks if s.get('id') not in {a.get('subtask_id') for a in accepted}]
        self.progress_reporter.step(f'[6/8] Integrating accepted subtask attempts: accepted={len(accepted)}, failed={len(failed)}')
        scope=self._analyze_subtask_scope(accepted, context.subtasks); write_json_utf8(idir/'scope_analysis.json', scope); context.integration['scope_analysis']=scope
        self.progress_reporter.step(f'[6/8] Integration scope analysis: accepted={len(accepted)}, skipped_for_overreach={scope["summary"]["skipped_for_overreach"]}, overlaps={scope["summary"]["overlaps"]}')
        skip_ids={r['subtask_id'] for r in scope['subtasks'] if r.get('integration_decision')=='skip'}
        ordered=[a for a in accepted if a.get('subtask_id') not in skip_ids]; write_json_utf8(idir/'apply_order.json', [a.get('subtask_id') for a in ordered])
        results=[]; conflicts=0
        for a in ordered:
            pp=a.get('patch_path'); r={'subtask_id':a.get('subtask_id'),'attempt_id':a.get('attempt_id'),'patch_path':pp,'applied':False,'conflicted':False,'stdout':'','stderr':''}
            if pp and Path(pp).exists():
                p=subprocess.run(['git','apply',pp], cwd=wt['worktree_path'], text=True, capture_output=True)
                r.update({'applied':p.returncode==0,'conflicted':p.returncode!=0,'stdout':p.stdout,'stderr':p.stderr}); conflicts += 1 if p.returncode else 0
            results.append(r)
        write_json_utf8(idir/'apply_results.json', results)
        cap=capture_worktree(wt['worktree_path'], idir); combined=idir/'combined.patch'; write_text_utf8(combined, Path(cap['patch_path']).read_text(errors='replace'))
        write_text_utf8(idir/'git_status.txt', cap.get('git_status',''))
        context.integration.update({'accepted_subtasks':accepted,'integrated_subtasks':ordered,'skipped_subtasks':[a for a in accepted if a.get('subtask_id') in skip_ids],'subtasks_failed':failed,'subtasks_missing':failed,'apply_results':results,'conflicts':conflicts,'combined_patch_path':str(combined),'changed_files':cap.get('changed_files',[])})
        self.progress_reporter.step(f'[6/8] Integration apply complete: accepted={len(accepted)}, applied={sum(1 for r in results if r["applied"])}, conflicts={conflicts}')
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary='Integrated accepted subtask patches',data=context.integration),context.integration)

    def _validation_command(self, repo: Path) -> list[str]:
        if (repo/'pytest.ini').exists() or (repo/'tests').exists() or any(repo.glob('test_*.py')): return [sys.executable,'-m','pytest','-q']
        return [sys.executable,'-m','pytest','-q']

    def _validation_plan(self, context) -> dict:
        vp=(context.task_context.investigation or {}).get('validation_plan') if isinstance(context.task_context.investigation, dict) else None
        if isinstance(vp, dict) and isinstance(vp.get('commands'), list) and vp.get('commands'):
            return vp | {'fallback': False}
        cmd=self._validation_command(Path(context.integration['worktree_path']))
        return {'commands':[{'cmd':' '.join(cmd), 'argv':cmd, 'required':True, 'reason':'Conservative fallback validation command'}], 'notes':['fallback validation plan used'], 'success_criteria_mapping':[], 'fallback': True, 'source': 'default'}

    def _run_validation_plan(self, context, phase: str, worktree_path: str) -> dict:
        idir=context.run_dir/'integration'; plan=self._validation_plan(context); write_json_utf8(idir/'validation_plan.json', plan)
        results=[]; passed=True
        for i,c in enumerate(plan.get('commands') or [], 1):
            argv=c.get('argv') or (c.get('cmd') if isinstance(c.get('cmd'), list) else str(c.get('cmd') or '').split())
            required=bool(c.get('required', True))
            self.progress_reporter.step(f'[6/8] Running {phase} validation command {i}/{len(plan.get("commands") or [])}...')
            res=self._run_cmd(argv, worktree_path)
            outp=idir/f'{phase}_{i}_stdout.txt'; errp=idir/f'{phase}_{i}_stderr.txt'
            write_text_utf8(outp, res['stdout']); write_text_utf8(errp, res['stderr'])
            row={'index':i,'cmd':c.get('cmd') or argv,'command':argv,'required':required,'reason':c.get('reason'),'exit_code':res['exit_code'],'passed':res['passed'],'stdout_path':str(outp),'stderr_path':str(errp),'failure_reason':res.get('failure_reason')}
            results.append(row)
            if required and not res['passed']: passed=False
        return {'passed':passed,'commands':results,'fallback':bool(plan.get('fallback')),'source':plan.get('source'),'exit_code':0 if passed else 1,'command':(results[0]['command'] if results else []),'failure_reason':None if passed else 'required validation command failed'}

    def _execute_integration_validate_node(self,node,context):
        context.graph.mark_running(node.id); idir=context.run_dir/'integration'
        val=self._run_validation_plan(context, 'validation_initial', context.integration['worktree_path'])
        write_json_utf8(idir/'validation_initial.json', val); write_json_utf8(idir/'validation.json', val); context.integration['validation_initial']=val; context.integration['validation']=val
        self.progress_reporter.step(f'[6/8] Integration validation {"passed" if val["passed"] else "failed"}')
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary='validation passed' if val['passed'] else 'validation failed',data=val),val)

    def _execute_integration_repair_node(self,node,context):
        context.graph.mark_running(node.id); idir=context.run_dir/'integration'
        need=bool(context.integration.get('conflicts')) or not (context.integration.get('validation') or {}).get('passed')
        if not need:
            return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='skipped',result_summary='No repair needed',data={'repair_used':False,'validation_after_repair':None}),{'repair_used':False})
        b=self.backends[node.assigned_backend]
        init=context.integration.get('validation_initial') or context.integration.get('validation') or {}
        conflicts=context.integration.get('apply_results') or []
        prompt=f"""You are repairing an integrated decomposition patch.

Do not restart from scratch.

The integration worktree already contains applied accepted subtask patches.

Fix conflicts, validation failures, or missing integration glue.

Keep the final diff minimal.

Use the validation failure output below.

Preserve successful subtask fixes where possible.

Original task:
{context.task.objective or context.task.instruction}

Success criteria:
{context.task.success_criteria}

Accepted subtask summaries:
{json.dumps([{'id': a.get('subtask_id'), 'changed_files': a.get('changed_files'), 'review': (a.get('review') or {}).get('summary')} for a in context.integration.get('integrated_subtasks', context.accepted_subtasks)], indent=2)}

Skipped subtask patches and reasons:
{json.dumps(context.integration.get('scope_analysis', {}).get('subtasks', []), indent=2)}

Apply conflicts:
{json.dumps([r for r in conflicts if r.get('conflicted')], indent=2)}

Initial validation command/stdout/stderr:
command={init.get('command')}
stdout={_tail(init.get('stdout_path'), 12000)}
stderr={_tail(init.get('stderr_path'), 12000)}

Current git diff:
{_patch_text(context.integration.get('combined_patch_path'), 60000)}

Current git status:
{_tail(str(idir/'git_status.txt'), 12000)}
"""
        write_text_utf8(idir/'repair_prompt.txt', prompt)
        self.progress_reporter.step('[6/8] Running integration repair attempt 1/1...')
        res=self.runner_adapter.run_task(repo_path=Path(context.integration['worktree_path']), task=prompt, success_criteria=context.task.success_criteria, backend_name=b.name, backend_config=b, timeout_seconds=context.timeout_seconds or b.timeout_seconds or 1200, context={'attempt_id':'integrated_decomposition_repair','node_id':node.id}, artifacts_dir=idir)
        write_text_utf8(idir/'repair_stdout.txt', res.stdout); write_text_utf8(idir/'repair_stderr.txt', res.stderr); cap=capture_worktree(context.integration['worktree_path'], idir); write_text_utf8(idir/'repair.patch', Path(cap['patch_path']).read_text(errors='replace'))
        write_json_utf8(idir/'repair_attempt.json', {'exit_code':res.exit_code,'runner_telemetry':res.telemetry,'changed_files':cap.get('changed_files',[])})
        context.integration['repair_used']=True
        self.progress_reporter.step('[6/8] Running post-repair validation...')
        context.integration['validation_after_repair']=self._run_validation_plan(context, 'validation_after_repair', context.integration['worktree_path'])
        context.integration['validation']=context.integration['validation_after_repair']
        write_json_utf8(idir/'validation_after_repair.json', context.integration['validation_after_repair']); write_json_utf8(idir/'validation.json', context.integration['validation'])
        self.progress_reporter.step(f'[6/8] Post-repair validation {"passed" if context.integration["validation"]["passed"] else "failed"}')
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary='repair attempted',data={'repair_used':True,'validation_after_repair':context.integration['validation_after_repair']}),{'repair_used':True,'validation_after_repair':context.integration['validation_after_repair']})

    def _execute_final_review_node(self,node,context):
        context.graph.mark_running(node.id); idir=context.run_dir/'integration'; cap=capture_worktree(context.integration['worktree_path'], idir)
        final=idir/'final.patch'; write_text_utf8(final, Path(cap['patch_path']).read_text(errors='replace')); write_json_utf8(idir/'final_changed_files.json', cap.get('changed_files',[])); write_text_utf8(idir/'final_git_status.txt', cap.get('git_status',''))
        rb=self.backends[node.assigned_backend]; inp={'attempt_id':'integrated_decomposition','patch':_patch_text(str(final),70000),'changed_files':cap.get('changed_files',[]),'validation':context.integration.get('validation'),'subtask_reviews':[a.get('review') for a in context.accepted_subtasks]}
        try:
            review,call=LLMReviewer().review(context.task,context.classification,rb,inp,self.backends,idir/'final_review.json',backend_override=rb,estimate_cost=(context.mode!='performance'))
        except TypeError:
            review,call=LLMReviewer().review(context.task,context.classification,rb,inp,self.backends,idir/'final_review.json',backend_override=rb)
        context.integration.update({'final_patch_path':str(final),'final_changed_files':cap.get('changed_files',[]),'final_review':review.model_dump(mode='json')})
        self.progress_reporter.step(f'[7/8] Final review complete: {review.decision}/{review.recommended_action}, score={review.score}')
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary=review.summary,confidence=review.score or 0.0,data=context.integration),context.integration)

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
        context.graph.mark_running(node.id)
        if context.decomposed_active:
            val=context.integration.get('validation') or {}; fr=context.integration.get('final_review') or {}; fp=context.integration.get('final_patch_path')
            accepted=bool(val.get('passed') and has_non_empty_patch(fp) and context.integration.get('integrated_subtasks', context.accepted_subtasks) and fr.get('decision')=='pass' and fr.get('recommended_action')=='accept')
            context.winner={'attempt_id':'integrated_decomposition','worktree_path':context.integration.get('worktree_path'),'patch_path':fp,'review':fr} if accepted else None
            data={'accepted':accepted,'winner':'integrated_decomposition' if accepted else None}
            self._write_node_artifacts(context,node,inp={'integration':context.integration})
            reason='' if accepted else 'Integrated decomposition failed validation, final review, patch, or subtask acceptance gates.'
            self.record_controller_step(context,node=node,action='final_decision_accepted' if accepted else 'final_decision_failed',status='accepted' if accepted else 'failed',summary=data['winner'] or reason)
            self.progress_reporter.final_decision(accepted, data['winner'], reason)
            return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',result_summary='Accepted integrated decomposition.' if accepted else reason,data=data),data)
        accepted=bool(context.winner); data={'accepted':accepted,'winner':context.winner.get('attempt_id') if context.winner else None}; self._write_node_artifacts(context,node,inp={'selection':context.selection.model_dump(mode='json') if context.selection else None})
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
        if context.decomposed_active: return 'Integrated decomposition failed validation, final review, patch, or subtask acceptance gates.'
        eligible=[a.get('attempt_id') for a in context.attempts if a.get('acceptance_eligible')]
        sel=context.selection
        if not eligible: return 'No candidate passed acceptance gates.'
        if sel and sel.decision=='reject_all' and (sel.summary or sel.reasons): return f'Selector intentionally rejected all candidates despite eligibility: {sel.summary or "; ".join(sel.reasons)}'
        if sel and sel.fallback_used and not sel.selected_attempt_id: return 'Eligible candidates existed, but selector did not return a valid eligible selected_attempt_id and fallback could not select a winner.'
        return 'Eligible candidates existed, but selector did not return a valid eligible selected_attempt_id and fallback could not select a winner.'

    def _finalize(self, context):
        accepted=bool(context.winner); apply_opts={}
        if accepted: apply_opts={'apply_command':f'villani-ops apply {context.run_id}','branch_command':f'villani-ops branch {context.run_id} --name villani-ops/{context.run_id}','pr_command':f'villani-ops pr {context.run_id} --title "{(context.task.objective or "Villani Ops changes")[:60]}"'}
        dec_meta=context.task_context.decomposition or {}
        advisory=bool(dec_meta.get('subtasks')) and not context.decomposed_active
        decision=Decision(run_id=context.run_id, mode=context.mode, runner=context.runner, orchestration_graph_path=str(context.run_dir/'orchestration_graph.json'), node_backend_assignments={n.id:n.assigned_backend for n in context.graph.nodes if n.assigned_backend}, plan=context.task_context.plan, decomposition=dec_meta, performance_backend_name=(next((n.assigned_backend for n in context.graph.nodes if n.assigned_backend),None) if context.mode=='performance' else None), performance_backend_model=(next((n.assigned_model for n in context.graph.nodes if n.assigned_model),None) if context.mode=='performance' else None), accepted=accepted, lifecycle_completed=True, final_state='accepted' if accepted else 'failed', final_action='accept' if accepted else 'fail', winning_attempt_id=context.winner.get('attempt_id') if context.winner else None, winning_worktree_path=context.winner.get('worktree_path') if context.winner else None, winning_patch_path=context.winner.get('patch_path') if context.winner else None, reviewer_decision=(context.winner.get('review') or {}).get('decision') if context.winner else None, reviewer_score=(context.winner.get('review') or {}).get('score') if context.winner else None, classification=context.task_context.classification, investigation=context.task_context.investigation, selection=context.selection.model_dump(mode='json') if context.selection else None, selected_attempt_id=('integrated_decomposition' if context.decomposed_active and accepted else (context.selection.selected_attempt_id if accepted and context.selection else None)), candidate_attempts_requested=context.candidate_attempts, candidate_attempts_completed=(len(context.attempts) if not context.decomposed_active else 0), eligible_candidate_attempts=[a['attempt_id'] for a in context.attempts if a.get('acceptance_eligible')], orchestration_summary=json.dumps(context.graph.summary()), total_cost=0 if context.mode=='performance' else sum(context.costs.values()), coding_cost=0 if context.mode=='performance' else context.costs['coding'], classification_cost=0 if context.mode=='performance' else context.costs['classification'], review_cost=0 if context.mode=='performance' else context.costs['review'], total_input_tokens=context.input_tokens, total_output_tokens=context.output_tokens, total_coding_input_tokens=sum(a.get('input_tokens') or 0 for a in context.attempts+context.accepted_subtasks+context.rejected_subtasks), total_coding_output_tokens=sum(a.get('output_tokens') or 0 for a in context.attempts+context.accepted_subtasks+context.rejected_subtasks), token_accounting_statuses=dict(Counter(a.get('token_accounting_status') or 'missing' for a in context.attempts+context.accepted_subtasks+context.rejected_subtasks)), attempts=context.attempts or context.accepted_subtasks+context.rejected_subtasks, warnings=context.warnings, apply_options=apply_opts, controller_steps=self._compact_controller_steps(context), controller_steps_path='controller_steps.jsonl', acceptance_blockers=[] if accepted else [b for a in context.attempts+context.rejected_subtasks for b in (a.get('acceptance_blockers') or [])], attempts_used=len(context.attempts) or len(context.accepted_subtasks)+len(context.rejected_subtasks), all_attempted_backends=[a.get('backend_name') for a in context.attempts+context.accepted_subtasks+context.rejected_subtasks], failure_reason='' if accepted else self._failure_reason(context), reason='Selected eligible candidate.' if accepted and not context.decomposed_active else ('Accepted integrated decomposition.' if accepted else self._failure_reason(context)), total_attempts=len(context.attempts) or len(context.accepted_subtasks)+len(context.rejected_subtasks), decomposition_executed=context.decomposed_active, decomposition_advisory_only=advisory, subtask_count=len(context.subtasks), subtasks_executed=[s.get('id') for s in context.subtasks], subtasks_accepted=[a.get('subtask_id') for a in context.accepted_subtasks], subtasks_rejected=[a.get('subtask_id') for a in context.rejected_subtasks], subtasks_failed=[s.get('id') for s in context.subtasks if s.get('id') not in {a.get('subtask_id') for a in context.accepted_subtasks}], attempts_per_subtask=(context.candidate_attempts if context.decomposed_active else 0), subtask_attempts_completed=sum((json.loads((context.run_dir/'subtasks'/s.get('id')/'subtask_summary.json').read_text()).get('attempts_completed',0) if (context.run_dir/'subtasks'/s.get('id')/'subtask_summary.json').exists() else 0) for s in context.subtasks), subtask_attempt_summaries={s.get('id'): {k:v for k,v in (json.loads((context.run_dir/'subtasks'/s.get('id')/'subtask_summary.json').read_text()) if (context.run_dir/'subtasks'/s.get('id')/'subtask_summary.json').exists() else {}).items() if k in {'attempts_requested','attempts_completed','accepted_attempt_id','status'}} for s in context.subtasks}, integration_worktree_path=context.integration.get('worktree_path'), integration_patch_path=context.integration.get('final_patch_path'), integration_validation=context.integration.get('validation'), integration_validation_initial=context.integration.get('validation_initial'), integration_validation_after_repair=context.integration.get('validation_after_repair'), integration_scope_analysis=context.integration.get('scope_analysis'), integration_repair_used=bool(context.integration.get('repair_used')), final_review=context.integration.get('final_review'), parallel_execution=context.parallel_execution)
        write_json(context.run_dir/'decision.json', decision.model_dump(mode='json')); context.final_decision=decision; return decision

    def record_controller_step(self, context, *, node=None, action:str, status:str, summary:str|None=None, details:dict|None=None):
        p=context.run_dir/'controller_steps.jsonl'; rec={'timestamp':_now(),'run_id':context.run_id,'node_id':getattr(node,'id',None),'node_kind':getattr(node,'kind',None),'action':action,'status':status,'backend':getattr(node,'assigned_backend',None),'model':getattr(node,'assigned_model',None),'summary':summary,'details':details or {}}
        p.parent.mkdir(parents=True,exist_ok=True)
        lock=getattr(context, 'controller_step_lock', None)
        if lock:
            with lock: p.open('a', encoding='utf-8').write(json.dumps(rec, ensure_ascii=False, default=str)+'\n')
        else: p.open('a', encoding='utf-8').write(json.dumps(rec, ensure_ascii=False, default=str)+'\n')
