from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import time, secrets, json, sys
from pydantic import BaseModel
from villani_ops.core.task import Task
from villani_ops.core.decision import Decision
from villani_ops.storage.files import FileStorage
from villani_ops.classification import TaskClassifier
from villani_ops.policy_engine import PolicyEngine, ExecutionStrategy
from villani_ops.policy_engine.engine import _write_controller_error
from villani_ops.isolation.worktree import GitWorktreeIsolation, capture_worktree
from villani_ops.runners.base import RunnerContext
from villani_ops.runners.villani_code import VillaniCodeRunner
from villani_ops.review import LLMReviewer, ReviewResult
from villani_ops.reports.markdown import write_markdown_report
from villani_ops.core.acceptance import is_attempt_acceptance_eligible, human_override_blockers
from villani_ops.controller.state_machine import ControllerDecisionContext, HumanApprovalResult, ControllerAction, ControllerState, ControllerStateRecorder, decide_next_action, next_state
from villani_ops.controller.human_approval import TerminalHumanApprovalProvider, NonInteractiveHumanApprovalProvider, HumanApprovalPromptContext
from villani_ops.controller.progress import RunProgressReporter

class RunResult(BaseModel):
    run_id: str; run_dir: str; decision: Decision; report_path: str; attempts: list[dict]

class CostPolicyVillaniOps:
    def __init__(self, storage: FileStorage, human_approval_provider=None, progress_reporter=None): self.storage=storage; self.human_approval_provider=human_approval_provider; self.progress_reporter=progress_reporter or RunProgressReporter(False)
    @classmethod
    def from_workspace(cls, path: str | Path = '.villani-ops') -> 'CostPolicyVillaniOps': return cls(FileStorage(Path(path).expanduser().resolve()))

    def run(self, repo: str|Path, task: Task, policy: str='balanced', isolation: str='worktree', human_approval: bool=False, non_interactive: bool=False, max_attempts: int|None=None) -> RunResult:
        self.storage.init_workspace(); start=time.time(); repo=Path(repo).resolve(); task.repo_path=str(repo)
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)
        run_dir=self.storage.create_run_dir(run_id); self.storage.save_task(run_dir, task)
        progress=self.progress_reporter
        progress.info("Starting Villani Ops run")
        progress.info(f"Workspace: {self.storage.workspace}")
        progress.info(f"Repo: {repo}")
        progress.info(f"Policy: {policy}")
        progress.info(f"Task: {task.objective or task.instruction}")
        backends=self.storage.load_backends(); warnings=[]; attempts=[]; costs={'classification':0.0,'policy':0.0,'review':0.0,'coding':0.0}; tin=tout=0
        recorder=ControllerStateRecorder(run_id, run_dir)
        strategy=None; cls=None
        def record_failed_controller_call(phase, schema, exc):
            nonlocal tin, tout
            result=getattr(exc, "result", None) or getattr(exc, "llm_result", None)
            backend=getattr(exc, "backend", None)
            if result:
                costs[phase]+=getattr(result, "estimated_cost", 0) or 0
                tin+=getattr(result, "input_tokens", 0) or 0
                tout+=getattr(result, "output_tokens", 0) or 0
            _write_controller_error(run_dir, phase, backend, schema, result, parse_error=getattr(exc, "parse_error", None), validation_error=str(exc) if result else None)
        def finalize(accepted=None, final_action='fail', failure_reason=''):
            apply_opts={}
            if accepted:
                apply_opts={'apply_command':f'villani-ops apply {run_id}','branch_command':f'villani-ops branch {run_id} --name villani-ops/{run_id}','pr_command':f'villani-ops pr {run_id} --title "{(task.objective or "Villani Ops changes")[:60]}"'}
            decision=Decision(run_id=run_id, accepted=bool(accepted), final_action=final_action, final_state=recorder.current_state.value if hasattr(recorder.current_state,'value') else str(recorder.current_state), lifecycle_completed=bool(accepted) or final_action=='fail', winning_attempt_id=accepted.get('attempt_id') if accepted else None, winning_branch=accepted.get('branch_name') if accepted else None, winning_worktree_path=accepted.get('worktree_path') if accepted else None, winning_patch_path=accepted.get('patch_path') if accepted else None, reviewer_decision=(accepted.get('review') or {}).get('decision') if accepted else ((attempts[-1].get('review') or {}).get('decision') if attempts else None), reviewer_score=(accepted.get('review') or {}).get('score') if accepted else ((attempts[-1].get('review') or {}).get('score') if attempts else None), reviewer_evidence=(accepted.get('review') or {}).get('evidence',[]) if accepted else [], classification=cls.model_dump(mode='json') if cls else None, execution_strategy=strategy.model_dump(mode='json') if strategy else None, total_cost=sum(costs.values()), coding_cost=costs['coding'], classification_cost=costs['classification'], policy_cost=costs['policy'], review_cost=costs['review'], total_input_tokens=tin, total_output_tokens=tout, total_coding_input_tokens=sum(a.get('input_tokens') or 0 for a in attempts), total_coding_output_tokens=sum(a.get('output_tokens') or 0 for a in attempts), token_accounting_statuses=dict(Counter(a.get('token_accounting_status') or 'missing' for a in attempts)), attempts=attempts, warnings=warnings, apply_options=apply_opts, decision_steps=decision_steps, controller_steps=recorder.steps, acceptance_blockers=([] if accepted else (decision_steps[-1].get('acceptance_blockers',[]) if decision_steps else [])), retries_used=retries_used, escalations_used=escalations_used, attempts_used=len(attempts), all_attempted_backends=[a.get('backend_name') for a in attempts], failure_reason=('' if accepted else (failure_reason or (decision_steps[-1].get('reason','No attempt satisfied acceptance gates.') if decision_steps else 'No attempt satisfied acceptance gates.'))), reason='Accepted reviewer-passed successful runner attempt.' if accepted else (failure_reason or 'No attempt satisfied acceptance gates.'), total_attempts=len(attempts), human_reviews_requested=sum(1 for a in attempts if (a.get('human_approval') or {}).get('requested')), human_reviews_completed=sum(1 for a in attempts if (a.get('human_approval') or {}).get('prompted')), human_reviews_skipped=sum(1 for a in attempts if (a.get('human_approval') or {}).get('skipped_reason')), human_override_used=bool(accepted and any(s.get('human_override_used') for s in decision_steps)), human_override_reasons=(((accepted.get('human_approval') or {}).get('request_reasons') or []) if accepted else []), human_override_blockers=next((s.get('human_override_blockers') for s in reversed(decision_steps) if s.get('human_override_blockers')), []))
            self.storage.save_decision(run_dir, decision); report=write_markdown_report(run_dir, task, strategy, attempts, decision, time.time()-start)
            progress.info(f"Result: {'ACCEPTED' if decision.accepted else 'FAILED'}")
            progress.info(f'Report: {report}')
            if decision.accepted: progress.info(f'Next: villani-ops apply {run_id}')
            return RunResult(run_id=run_id, run_dir=str(run_dir), decision=decision, report_path=str(report), attempts=attempts)
        progress.step("[1/6] Classifying task...")
        # classify
        recorder.transition(ControllerAction.classify, ControllerState.classifying, 'Starting task classification.')
        try:
            cls, cls_call = TaskClassifier().classify(task, backends, run_dir/'classification.json'); task.classification=cls; self.storage.save_task(run_dir, task)
        except Exception as e:
            record_failed_controller_call('classification', getattr(e, 'schema_name', 'TaskClassification'), e)
            warnings.append(str(e)); recorder.transition(ControllerAction.fail, ControllerState.failed, f'Classification failed: {e}')
            decision_steps=[]; retries_used=escalations_used=0
            return finalize(None, 'fail', f'Classification failed: {e}')
        costs['classification']+=cls_call.estimated_cost; tin+=cls_call.input_tokens; tout+=cls_call.output_tokens
        if cls_call.error:
            warnings.append(cls_call.error)
        progress.info(f"Classification: {cls.difficulty} {cls.category} {cls.risk}, confidence {cls.confidence:.2f}")
        from villani_ops.policy_engine.planner import estimate_required_capability
        progress.info(f"Required capability estimate: {estimate_required_capability(cls)}")
        if cls.adjustment_notes:
            [progress.info(f"  {note}") for note in cls.adjustment_notes]
        sig=cls.task_shape_signals or {}
        progress.info(f"Task shape: relevant files {sig.get('relevant_file_count', len(cls.relevant_file_paths))}, explicit tests {'yes' if sig.get('explicit_tests_mentioned') else 'no'}, target files found {'yes' if sig.get('target_files_found') else 'no'}")
        progress.step("[2/6] Generating policy...")
        # policy
        recorder.transition(ControllerAction.generate_strategy, ControllerState.planning, 'Classification complete; generating strategy.')
        try:
            try:
                strategy, pol_call = PolicyEngine().generate(cls, backends, policy, run_dir/'execution_strategy.json', max_attempts=max_attempts)
            except TypeError as e:
                if "max_attempts" not in str(e):
                    raise
                strategy, pol_call = PolicyEngine().generate(cls, backends, policy, run_dir/'execution_strategy.json')
            (run_dir/'strategy.json').write_text(strategy.model_dump_json(indent=2))
            if strategy.warnings:
                for w in strategy.warnings:
                    if 'fallback' in str(w).lower(): progress.warning(str(w))
            progress.info(f"Policy: {policy}")
            progress.info(f"Max attempts: {strategy.max_attempts or len(strategy.attempts)}")
            progress.info(f"Planning objective: {strategy.planning_objective or strategy.strategy_summary}")
            progress.info('Backend rankings:')
            for r in strategy.backend_rankings:
                progress.info(f"  {r.get('backend')}: capability {r.get('capability_score')}, capability gap {r.get('capability_gap')}, base p_solve {r.get('base_solve_probability')}, shape adjustment {r.get('shape_adjustment')}, final p_solve {r.get('final_solve_probability')}, estimated cost ${float(r.get('estimated_attempt_cost') or 0):.6f}, viable {'yes' if r.get('viable') else 'no'}")
            progress.info('Planned attempts:')
            [progress.info(f'  attempt_{i+1:03d}: {a.backend}, p_solve {a.estimated_solve_probability}, estimated cost ${float(a.estimated_attempt_cost or 0):.6f}, reason: {a.reason}') for i,a in enumerate(strategy.attempts)]
        except Exception as e:
            warnings.append(str(e)); recorder.transition(ControllerAction.fail, ControllerState.failed, f'Policy generation failed: {e}')
            decision_steps=[]; retries_used=escalations_used=0
            return finalize(None, 'fail', f'Policy generation failed: {e}')
        costs['policy']+=pol_call.estimated_cost; tin+=pol_call.input_tokens; tout+=pol_call.output_tokens
        accepted=None; runner=VillaniCodeRunner(); decision_steps=[]; retries_used=0; escalations_used=0
        pending_transition=None
        for current_plan_index, plan in enumerate(strategy.attempts):
            backend=backends[plan.backend]
            for _ in range(plan.max_attempts):
                attempt_id=f"attempt_{len(attempts)+1:03d}"; adir=run_dir/'attempts'/attempt_id; adir.mkdir(parents=True, exist_ok=True)
                meta={'attempt_id':attempt_id,'backend_name':backend.name,'model':backend.model,'runner_name':'villani_code','status':'running','started_at':datetime.now(timezone.utc).isoformat(),'policy_reason':plan.reason}
                try:
                    if pending_transition:
                        state, msg = pending_transition; pending_transition=None
                        recorder.transition(ControllerAction.run_attempt, ControllerState.attempting, msg, attempt_id, {'backend':backend.name,'model':backend.model})
                    else:
                        recorder.transition(ControllerAction.run_attempt, ControllerState.attempting, 'Starting coding attempt.', attempt_id, {'backend':backend.name,'model':backend.model})
                    if isolation!='worktree': raise ValueError('Only worktree isolation is supported by the v0.2 controller loop')
                    progress.step(f'[3/6] Running {attempt_id} on {backend.name} ...')
                    wt=GitWorktreeIsolation().create(repo, run_id, attempt_id, self.storage.workspace); meta.update(wt)
                    progress.info(f"Worktree: {wt['worktree_path']}")
                    result=runner.run(RunnerContext(attempt_id=attempt_id, repo_path=wt['worktree_path'], task_instruction=task.objective or task.instruction or '', success_criteria=task.success_criteria, backend=backend, timeout_seconds=plan.timeout_seconds or backend.timeout_seconds or 1200, run_dir=str(adir)))
                    (adir/'stdout.log').write_text(result.stdout); (adir/'stderr.log').write_text(result.stderr)
                    progress.info(f'Villani Code debug dir: {result.debug_artifact_dir}')
                    progress.info(f'Villani Code exited {result.exit_code} in {((result.duration_ms or 0)/1000):.1f}s')
                    coding_cost=backend.estimate_cost(result.input_tokens,result.output_tokens)
                    meta.update({'exit_code':result.exit_code,'stdout_path':str(adir/'stdout.log'),'stderr_path':str(adir/'stderr.log'),'debug_artifact_dir':result.debug_artifact_dir,'resolved_trace_dir':result.resolved_trace_dir,'telemetry_path':result.telemetry_path,'duration_ms':result.duration_ms,'input_tokens':result.input_tokens,'output_tokens':result.output_tokens,'total_tokens':result.input_tokens+result.output_tokens,'coding_cost':coding_cost,'model_requests':result.model_requests,'model_failures':result.model_failures,'total_tool_calls':result.total_tool_calls,'tool_calls_by_name':result.tool_calls_by_name,'total_file_reads':result.total_file_reads,'total_file_writes':result.total_file_writes,'commands_executed':result.commands_executed,'commands_failed':result.commands_failed,'first_substantive_file_read_tool_index':result.first_substantive_file_read_tool_index,'first_substantive_file_read_seconds':result.first_substantive_file_read_seconds,'first_file_mutation_tool_index':result.first_file_mutation_tool_index,'first_file_mutation_seconds':result.first_file_mutation_seconds,'first_command_tool_index':result.first_command_tool_index,'first_command_seconds':result.first_command_seconds,'token_accounting_status':result.token_accounting_status,'token_accounting_warnings':result.token_accounting_warnings,'runner_telemetry':result.telemetry})
                    if result.token_accounting_status == 'missing':
                        warnings.append(f'Attempt {attempt_id} has token_accounting_status={result.token_accounting_status}. Coding cost may be unreliable.')
                        progress.warning(f'token accounting missing for {attempt_id}')
                    elif result.token_accounting_status == 'mismatch':
                        warnings.append(f'Attempt {attempt_id} has token_accounting_status={result.token_accounting_status}. Coding cost may be unreliable.')
                        progress.warning(f'token accounting mismatch for {attempt_id}; coding cost may be unreliable')
                    elif result.token_accounting_status == 'summary_only':
                        progress.warning(f'token accounting summary-only for {attempt_id}; model response usage was not available for verification')
                    else:
                        progress.info(f'Telemetry: {result.token_accounting_status}, input {result.input_tokens}, output {result.output_tokens}, cost ${coding_cost:.6f}')
                    cap=capture_worktree(wt['worktree_path'], adir); meta.update(cap)
                    if cap.get('filtered_generated_files'):
                        warnings.append('Filtered generated/cache files from patch: ' + ', '.join(cap.get('filtered_generated_files') or []))
                        progress.warning('Filtered generated files from patch:')
                        [progress.info(f'  {x}') for x in cap.get('filtered_generated_files') or []]
                    progress.info('Changed files: ' + (', '.join(cap.get('changed_files') or []) or 'none'))
                    costs['coding']+=coding_cost; tin+=result.input_tokens; tout+=result.output_tokens
                    progress.step(f'[4/6] Reviewing {attempt_id}...')
                    recorder.transition(ControllerAction.review_attempt, ControllerState.reviewing, 'Runner completed; starting review.', attempt_id, {'exit_code':result.exit_code,'changed_files':meta.get('changed_files')})
                    review_input={k:meta.get(k) for k in ['attempt_id','exit_code','stdout_path','stderr_path','changed_files','git_status']}
                    review_input['runner_telemetry']={'duration_ms':result.duration_ms,'input_tokens':result.input_tokens,'output_tokens':result.output_tokens,'coding_cost':coding_cost,'model_requests':result.model_requests,'model_failures':result.model_failures,'total_tool_calls':result.total_tool_calls,'tool_calls_by_name':result.tool_calls_by_name,'total_file_reads':result.total_file_reads,'total_file_writes':result.total_file_writes,'commands_executed':result.commands_executed,'commands_failed':result.commands_failed,'first_substantive_file_read_tool_index':result.first_substantive_file_read_tool_index,'first_file_mutation_tool_index':result.first_file_mutation_tool_index,'token_accounting_status':result.token_accounting_status}
                    review_input['stdout_summary']=result.stdout[-4000:]; review_input['stderr_summary']=result.stderr[-4000:]; review_input['patch']=(Path(cap['patch_path']).read_text(errors='replace')[:50000])
                    try:
                        review, rev_call=LLMReviewer().review(task, cls, backend, review_input, backends, adir/'review.json')
                        costs['review']+=rev_call.estimated_cost; tin+=rev_call.input_tokens; tout+=rev_call.output_tokens
                    except Exception as e:
                        record_failed_controller_call('review', getattr(e, 'schema_name', 'ReviewResult'), e)
                        raw_payload=(getattr(getattr(e, 'llm_result', None), 'parsed_json', {}) or {})
                        normalized_payload=getattr(e, 'normalized_payload', {}) or {}
                        result_obj=getattr(e, 'llm_result', None)
                        fallback_action='fail' if non_interactive else 'ask_human'
                        fallback_payload={'passed':False,'score':0.0,'decision':'uncertain','summary':f'Reviewer validation failed: {e}','evidence':[],'issues':[str(e)],'recommended_action':fallback_action,'confidence':0.0,'requires_human_approval':not non_interactive}
                        (run_dir/'controller_calls').mkdir(exist_ok=True)
                        (run_dir/'controller_calls'/f'review_error_{attempt_id}.json').write_text(json.dumps({'phase':'review','attempt_id':attempt_id,'schema':'ReviewDecision','raw_payload':raw_payload,'normalized_payload':normalized_payload,'validation_error':str(e),'fallback_used':True,'fallback_payload':fallback_payload,'backend':getattr(getattr(e, 'backend', None), 'name', None),'model':getattr(getattr(e, 'backend', None), 'model', None),'url':getattr(result_obj, 'url', None),'max_tokens':getattr(result_obj, 'max_tokens', None),'finish_reason':getattr(result_obj, 'finish_reason', None),'usage':getattr(result_obj, 'usage', {}) if result_obj else {},'message_content':getattr(result_obj, 'raw_text', '') if result_obj else '', 'reasoning_content':getattr(result_obj, 'reasoning_content', None) if result_obj else None}, indent=2))
                        progress.warning(f'Review validation failed for {attempt_id}; using fallback {fallback_action}.')
                        review=ReviewResult(**fallback_payload)
                        (adir/'review.json').write_text(review.model_dump_json(indent=2))
                        warnings.append(f'Review failed for {attempt_id}: {e}')
                    progress.info(f'Review: {review.decision}, score {review.score:.2f}, recommended {review.recommended_action}')
                    meta['review']=review.model_dump(mode='json'); meta['review_path']=str(adir/'review.json')
                    meta['status']='candidate'
                    if result.exit_code!=0:
                        meta['status']='failed'; meta['error']=result.stderr.strip() or f"Runner exited with {result.exit_code}"
                    eligible_before, blockers_before = is_attempt_acceptance_eligible(meta)
                    request_reasons=[]
                    if human_approval: request_reasons.append('cli_human_approval')
                    if strategy.stop_conditions.get('human_approval_enabled'): request_reasons.append('strategy_human_approval_enabled')
                    if strategy.stop_conditions.get('ask_human_on_uncertain_review') and review.decision=='uncertain': request_reasons.append('strategy_ask_human_on_uncertain_review')
                    if review.requires_human_approval: request_reasons.append('reviewer_requires_human_approval')
                    if review.recommended_action=='ask_human': request_reasons.append('reviewer_recommended_ask_human')
                    human_needed = bool(request_reasons)
                    if human_needed:
                        if non_interactive: progress.info('Human approval skipped because --non-interactive is set.')
                        recorder.transition(ControllerAction.ask_human, ControllerState.human_review, 'Human approval requested.', attempt_id, {'request_reasons':request_reasons})
                        provider=self.human_approval_provider or (NonInteractiveHumanApprovalProvider() if (non_interactive or not sys.stdin.isatty()) else TerminalHumanApprovalProvider())
                        approval_model=provider.request_approval(HumanApprovalPromptContext(run_id=run_id, attempt_id=attempt_id, backend_name=backend.name, backend_model=backend.model, runner_exit_code=result.exit_code, review_decision=review.decision, review_score=review.score, review_summary=review.summary, review_evidence=review.evidence, review_issues=review.issues, acceptance_blockers=blockers_before, patch_path=meta.get('patch_path'), changed_files=meta.get('changed_files') or [], cost_so_far=sum(costs.values()), request_reasons=request_reasons))
                        approval=approval_model.model_dump(mode='json')
                        meta['human_approval']=approval; (adir/'human_approval.json').write_text(json.dumps(approval, indent=2))
                        if approval_model.decision=='accept': meta['status']='human_approved'
                        elif approval_model.decision=='retry': meta['status']='rejected'
                        elif approval_model.decision=='escalate': meta['status']='rejected'; meta['escalate_requested']=True
                        elif approval_model.decision=='fail': meta['status']='rejected'; meta['fail_requested']=True
                        else: meta['status']='rejected'
                        recorder.transition(ControllerAction.decide, ControllerState.deciding, 'Human approval completed or skipped.', attempt_id, {'decision':approval_model.decision,'valid_override':approval_model.valid_override})
                    elif result.exit_code==0 and review.passed and review.decision=='pass' and review.recommended_action=='accept':
                        meta['status']='validated'
                        recorder.transition(ControllerAction.decide, ControllerState.deciding, 'Review completed; deciding next action.', attempt_id)
                    else:
                        recorder.transition(ControllerAction.decide, ControllerState.deciding, 'Review completed; deciding next action.', attempt_id)
                    # deterministic controller state-machine decision
                    human_model=HumanApprovalResult.model_validate(meta['human_approval']) if meta.get('human_approval') else None
                    review_model=ReviewResult.model_validate(meta['review'])
                    remaining_for_backend = plan.max_attempts - (_ + 1)
                    escalation_available = current_plan_index < len(strategy.attempts)-1
                    override_ok, override_blockers = human_override_blockers(meta, human_model) if human_model else (False, [])
                    ctx=ControllerDecisionContext(run_id=run_id, attempt=meta, review=review_model, human_approval=human_model, strategy=strategy, current_plan_index=current_plan_index, current_attempt_number=len(attempts)+1, attempts_remaining_for_backend=remaining_for_backend, escalation_available=escalation_available, non_interactive=non_interactive, human_override_allowed=override_ok)
                    action_decision=decide_next_action(ctx)
                    eligible=action_decision.acceptance_eligible; blockers=action_decision.acceptance_blockers
                    if action_decision.action == ControllerAction.accept and human_model and human_model.decision=='accept' and override_ok:
                        meta['status']='human_approved'; eligible=True; blockers=[]
                    elif action_decision.action == ControllerAction.accept:
                        meta['status']='validated'
                    meta['acceptance_eligible']=eligible; meta['acceptance_blockers']=blockers
                    action=action_decision.action.value; reason=action_decision.reason
                    step={'attempt_id':attempt_id,'runner_exit_code':meta.get('exit_code'),'review_decision':review_model.decision,'review_recommended_action':review_model.recommended_action,'human_approval_decision':(human_model.decision if human_model else None),'human_override_used':bool(override_ok and action=='accept'),'human_override_reasons':(human_model.request_reasons if human_model else []),'human_override_blockers':(override_blockers if human_model and not override_ok else []),'acceptance_eligible':eligible,'acceptance_blockers':blockers,'acceptance_blockers_before_override':blockers_before,'acceptance_blockers_after_override':blockers,'controller_action':action,'reason':reason,'created_at':datetime.now(timezone.utc).isoformat()}
                    meta['human_override_blockers']=step['human_override_blockers']
                    meta['controller_action']=action; meta['controller_decision']=step; decision_steps.append(step); (adir/'controller_decision.json').write_text(json.dumps(step, indent=2))
                    progress.step('[5/6] Finalizing decision...')
                    if action_decision.action != ControllerAction.accept:
                        progress.info(f'Attempt rejected: {reason}')
                        progress.info(f'Next action: {action_decision.action.value.replace('_', ' ')}')
                    recorder.transition(action_decision.action, next_state(action_decision.action), reason, attempt_id, step)
                    if action_decision.action == ControllerAction.retry_same_backend: retries_used += 1
                    if action_decision.action == ControllerAction.escalate: escalations_used += 1
                    if action_decision.action == ControllerAction.accept:
                        accepted=meta; attempts.append(meta); (adir/'attempt.json').write_text(json.dumps(meta, indent=2)); break
                    if action_decision.action == ControllerAction.retry_same_backend:
                        pending_transition=(ControllerState.retrying, 'Retrying same backend; starting next attempt.')
                    if action_decision.action == ControllerAction.escalate:
                        pending_transition=(ControllerState.escalating, 'Escalating to next backend; starting next attempt.')
                except Exception as e:
                    meta['status']='failed'; meta['error']=str(e); warnings.append(str(e))
                    recorder.transition(ControllerAction.review_attempt, ControllerState.reviewing, 'Attempt failed before or during review; preserving lifecycle.', attempt_id, {'error':str(e)})
                    recorder.transition(ControllerAction.decide, ControllerState.deciding, 'Attempt error; deciding failure.', attempt_id)
                    recorder.transition(ControllerAction.fail, ControllerState.failed, f'Attempt failed: {e}', attempt_id)
                meta['ended_at']=datetime.now(timezone.utc).isoformat(); attempts.append(meta); (adir/'attempt.json').write_text(json.dumps(meta, indent=2))
                rec=((meta.get('review') or {}).get('recommended_action'))
                if meta.get('fail_requested'): break
                if meta.get('controller_action')=='retry_same_backend' or rec in {'retry_same_backend'} or (meta.get('human_approval') or {}).get('decision')=='retry': continue
                if meta.get('controller_action')=='escalate' or (meta.get('human_approval') or {}).get('decision')=='escalate': break
                break
            if accepted: break
        return finalize(accepted, 'accept' if accepted else (decision_steps[-1]['controller_action'] if decision_steps else 'fail'))
