from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
from .state import CandidateAttemptState, SubtaskState
from villani_ops.storage.files import capture_diff
from villani_ops.validation.base import DiffReviewValidator
import subprocess, json, time, shutil

class StrictModel(BaseModel): model_config=ConfigDict(extra='forbid')
class OpsGetStateInput(StrictModel): include_artifacts:bool=False; include_recent_events:bool=True
class OpsInspectRepoInput(StrictModel): focus:str; max_files:int=200; include_snippets:bool=True
class OpsSubmitClassificationInput(StrictModel): category:str; difficulty:Literal['easy','medium','hard','unknown']; reasoning:str; confidence:float; risk_factors:list[str]=Field(default_factory=list); suggested_backend_tier:str|None=None
class ValidationCommand(StrictModel): cmd:str; cwd:str|None=None; purpose:str|None=None; timeout_seconds:int|None=None
class ValidationPlan(StrictModel): commands:list[ValidationCommand]=Field(default_factory=list); notes:list[str]=Field(default_factory=list)
class OpsSubmitInvestigationInput(StrictModel): summary:str; suspected_root_cause:str|None=None; relevant_files:list[str]=Field(default_factory=list); relevant_tests:list[str]=Field(default_factory=list); implementation_plan:list[str]=Field(default_factory=list); risks:list[str]=Field(default_factory=list); validation_plan:ValidationPlan|None=None; confidence:float
class OpsSubmitPlanInput(StrictModel): summary:str; strategy:Literal['single_task','parallel_candidates','decompose_then_execute']; should_decompose:bool; decomposition_reason:str|None=None; candidate_attempts:int; risks:list[str]=Field(default_factory=list); expected_difficulty:Literal['easy','medium','hard','unknown']; confidence:float
class SubtaskInput(StrictModel): id:str; title:str; objective:str; success_criteria:str|None=None; relevant_files:list[str]=Field(default_factory=list); dependencies:list[str]=Field(default_factory=list); expected_difficulty:Literal['easy','medium','hard','unknown']='unknown'; risk:Literal['low','medium','high','unknown']='unknown'; confidence:float; can_run_parallel:bool; parallel_group:str|None=None; merge_contract:str|None=None
class OpsSubmitDecompositionInput(StrictModel):
    should_use_decomposition:bool; reason:str; subtasks:list[SubtaskInput]=Field(default_factory=list); merge_strategy:str|None=None; confidence:float
    @model_validator(mode='after')
    def check(self):
        if self.should_use_decomposition and len(self.subtasks)<2: raise ValueError('decomposition requires at least 2 subtasks')
        ids=[s.id for s in self.subtasks]
        if len(ids)!=len(set(ids)): raise ValueError('subtask IDs must be unique')
        for s in self.subtasks:
            for d in s.dependencies:
                if d not in ids: raise ValueError(f'unknown dependency {d}')
        return self
class OpsValidateDecompositionInput(StrictModel): decomposition_id:str='current'; semantic:bool=True
class DecompositionValidationResult(StrictModel): accepted:bool; deterministic_accepted:bool; semantic_accepted:bool|None=None; failures:list[str]=Field(default_factory=list); required_revisions:list[str]=Field(default_factory=list); warnings:list[str]=Field(default_factory=list); computed_acceptance_reason:str
class OpsSelectExecutionPathInput(StrictModel): path:Literal['parallel_candidates','decomposed_subtasks']; reason:str
class OpsLaunchCandidatesInput(StrictModel): attempts:int; backend_name:str|None=None; reason:str
class OpsLaunchSubtasksInput(StrictModel): subtask_ids:list[str]; backend_name:str|None=None; attempts_per_subtask:int; reason:str
class OpsReviewAttemptInput(StrictModel): attempt_id:str; scope:Literal['candidate','subtask','integration']
class OpsReviewResult(StrictModel): decision:Literal['pass','fail']; recommended_action:Literal['accept','reject','retry','repair']; score:float; summary:str; evidence:list[str]=Field(default_factory=list); issues:list[str]=Field(default_factory=list); subtask_passed:bool|None=None; scope_ok:bool|None=None; integration_risk:Literal['low','medium','high','unknown']|None=None
class OpsIntegrateSubtasksInput(StrictModel): reason:str
class OpsRunValidationInput(StrictModel): commands:list[ValidationCommand]; target:Literal['candidate','integration','repo']; target_id:str|None=None
class OpsSelectWinnerInput(StrictModel): selected_attempt_id:str|None=None; decision:Literal['select','reject_all']; summary:str; reasons:list[str]=Field(default_factory=list); rejected_attempts:list[str]=Field(default_factory=list); confidence:float
class OpsFinalizeRunInput(StrictModel): decision:Literal['accepted','rejected','failed']; summary:str; selected_attempt_id:str|None=None; selected_patch_path:str|None=None; blockers:list[str]=Field(default_factory=list)
@dataclass
class ToolSpec: name:str; description:str; input_model:type[BaseModel]; handler:Callable; read_only:bool=False

def h_get_state(state, inp, ctx): return {'status':state.status,'phase':state.phase,'execution_path':state.execution_path,'allowed_next_actions':state.allowed_next_actions(),'decomposition_accepted':state.decomposition_accepted,'subtasks':[s.model_dump() for s in state.subtasks],'candidates':[c.model_dump() for c in state.candidates],'warnings':state.warnings,'recovery_count':state.recovery_count}
def h_inspect_repo(state, inp, ctx):
    root=Path(state.repo_path); files=[str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '.git' not in p.parts][:inp.max_files]
    cfg=[f for f in files if Path(f).name in {'pyproject.toml','package.json','Cargo.toml','go.mod','Makefile'}]
    return {'tree_summary':files[:50],'likely_source_files':[f for f in files if f.endswith(('.py','.js','.ts','.rs','.go'))][:50],'likely_test_files':[f for f in files if 'test' in f.lower()][:50],'package_build_config_files':cfg,'detected_validation_commands':[]}
def h_classification(state, inp, ctx): state.classification=inp.model_dump(); return state.classification
def h_investigation(state, inp, ctx): state.investigation=inp.model_dump(); state.phase='planning'; return state.investigation
def h_plan(state, inp, ctx): state.plan=inp.model_dump(); state.decomposition_requested=inp.should_decompose; state.phase='decomposing' if inp.should_decompose else 'choosing_execution_path'; return state.plan
def h_decomposition(state, inp, ctx):
    state.decomposition=inp.model_dump(); state.phase='choosing_execution_path'; state.subtasks=[SubtaskState(subtask_id=s.id,title=s.title,objective=s.objective,success_criteria=s.success_criteria,relevant_files=s.relevant_files,dependencies=s.dependencies,expected_difficulty=s.expected_difficulty,risk=s.risk) for s in inp.subtasks]; return state.decomposition
def h_validate_decomposition(state, inp, ctx):
    failures=[]; ids=[s.subtask_id for s in state.subtasks]
    if not state.decomposition: failures.append('no decomposition exists')
    if state.decomposition and state.decomposition.get('should_use_decomposition') and len(ids)<2: failures.append('at least 2 subtasks required')
    if len(ids)!=len(set(ids)): failures.append('duplicate subtask IDs')
    for s in state.subtasks:
        if not s.objective.strip(): failures.append(f'subtask {s.subtask_id} objective is empty')
        if s.subtask_id in s.dependencies: failures.append(f'subtask {s.subtask_id} depends on itself')
        for d in s.dependencies:
            if d not in ids: failures.append(f'unknown dependency {d}')
    objs=[s.objective.strip() for s in state.subtasks]
    if len(objs)!=len(set(objs)): failures.append('duplicate objectives')
    temp=set(); perm=set()
    by={s.subtask_id:s for s in state.subtasks}
    def visit(x):
        if x in perm: return
        if x in temp: raise ValueError('dependency cycle detected')
        temp.add(x)
        for d in by[x].dependencies: visit(d)
        temp.remove(x); perm.add(x)
    try:
        for x in ids: visit(x)
    except ValueError as e: failures.append(str(e))
    if state.decomposition and state.decomposition.get('should_use_decomposition') and not any(not s.dependencies for s in state.subtasks): failures.append('no executable root subtask')
    if state.decomposition and state.decomposition.get('should_use_decomposition') is False: failures.append('decomposition not requested for execution')
    deterministic=not failures; warnings=[]; semantic=None
    if inp.semantic: warnings.append('semantic_decomposition_validation_unavailable'); state.warnings.append('semantic_decomposition_validation_unavailable')
    accepted=deterministic
    reason='deterministic validation passed; semantic validation unavailable' if accepted and semantic is None else 'deterministic validation failed'
    res=DecompositionValidationResult(accepted=accepted,deterministic_accepted=deterministic,semantic_accepted=semantic,failures=failures,required_revisions=failures,warnings=warnings,computed_acceptance_reason=reason)
    state.decomposition_validated=True; state.decomposition_accepted=accepted; state.phase='choosing_execution_path'; return res.model_dump()

def h_select_path(state, inp, ctx):
    state.execution_path=inp.path
    state.phase='running_subtasks' if inp.path=='decomposed_subtasks' else 'running_candidates'
    state.decomposition_executed=inp.path=='decomposed_subtasks'
    if inp.path=='parallel_candidates' and state.decomposition is not None and state.decomposition_accepted is not True:
        state.decomposition_fallback_used=True; state.decomposition_fallback_reason=inp.reason; state.decomposition_executed=False
    return {'execution_path':state.execution_path,'reason':inp.reason,'decomposition_fallback_used':state.decomposition_fallback_used}
def _attempt(aid, scope, subtask_id=None, backend=None, artifacts=None):
    return CandidateAttemptState(attempt_id=aid,backend_name=backend,status='scheduled',scope=scope,subtask_id=subtask_id,artifacts_dir=str(artifacts) if artifacts else None,acceptance_eligible=False)
def _copy_worktree(src:Path, dst:Path):
    ignore=shutil.ignore_patterns('.git','.villani-ops','.v','__pycache__')
    shutil.copytree(src,dst,ignore=ignore,dirs_exist_ok=True)

def _run_attempt(state, ctx, aid, scope, task, success, subtask_id=None, backend_name=None):
    if ctx.runner_adapter is None and ctx.backend is None:
        adir=Path(state.run_dir)/'attempts'/aid; adir.mkdir(parents=True,exist_ok=True); (adir/'stdout.log').write_text('explicit unit-test fake runner\n')
        a=_attempt(aid,scope,subtask_id=subtask_id,backend='explicit_test_fake',artifacts=adir); a.status='completed'; a.stdout_path=str(adir/'stdout.log')
        if scope in {'subtask','candidate'}: a.acceptance_eligible=True; a.review={'decision':'pass','summary':'explicit unit-test fake review'}
        return a
    if ctx.runner_adapter is None: raise ValueError('agentic runner requires a real configured runner adapter; fake runner is not allowed')
    if ctx.backend is None: raise ValueError('agentic runner requires configured coding backend')
    adir=Path(state.run_dir)/'attempts'/aid; adir.mkdir(parents=True,exist_ok=True)
    wtree=adir/'worktree'; _copy_worktree(Path(state.repo_path), wtree)
    a=_attempt(aid,scope,subtask_id=subtask_id,backend=backend_name or getattr(ctx.backend,'name',None),artifacts=adir); a.status='running'; a.worktree_path=str(wtree)
    ctx.recorder.record(f'{scope}_attempt_started', payload={'attempt_id':aid,'subtask_id':subtask_id,'status':'running','artifact_paths':{'artifacts_dir':str(adir)}})
    res=ctx.runner_adapter.run_task(repo_path=wtree,task=task,success_criteria=success,backend_name=a.backend_name or '',backend_config=ctx.backend,timeout_seconds=ctx.timeout_seconds,context={'attempt_id':aid,'subtask_id':subtask_id,'parent_task':state.task},artifacts_dir=adir)
    (adir/'stdout.log').write_text(getattr(res,'stdout','') or ''); (adir/'stderr.log').write_text(getattr(res,'stderr','') or '')
    diff=capture_diff(Path(state.repo_path), wtree, adir/'diff.patch')
    a.stdout_path=str(adir/'stdout.log'); a.stderr_path=str(adir/'stderr.log'); a.patch_path=str(diff); a.transcript_path=getattr(res,'telemetry_path',None); a.changed_files=[l[4:].strip() for l in diff.read_text(errors='replace').splitlines() if l.startswith(('+++ b/','--- a/'))]
    ok=getattr(res,'exit_code',1)==0
    a.status='completed' if ok else 'failed'; a.completed_at=str(time.time())
    if ok and getattr(ctx.runner_adapter,'name',None)=='explicit-test-fake-runner':
        a.acceptance_eligible=True; a.review={'decision':'pass','summary':'explicit injected fake runner review'}
    ev=f'{scope}_attempt_completed' if ok else f'{scope}_attempt_failed'
    ctx.recorder.record(ev, payload={'attempt_id':aid,'subtask_id':subtask_id,'status':a.status,'exit_code':getattr(res,'exit_code',None),'failure_reason':getattr(res,'stderr','') if not ok else None,'artifact_paths':{'stdout':a.stdout_path,'stderr':a.stderr_path,'patch':a.patch_path}})
    return a

def h_launch_candidates(state, inp, ctx):
    if state.execution_path!='parallel_candidates': raise ValueError('candidates require parallel_candidates execution path')
    made=[]; maxp=max(1, int(getattr(ctx.backend,'max_parallel',1) or ctx.max_parallel or 1))
    for i in range(inp.attempts):
        aid=f'candidate_{len(state.candidates)+1:03d}'
        c=_run_attempt(state,ctx,aid,'candidate',state.task,state.success_criteria,backend_name=inp.backend_name or ctx.backend_name)
        state.candidates.append(c); made.append(aid)
    state.phase='selecting'; return {'launched':made,'max_parallel':maxp,'semantics':'candidate_attempts=N launches N full-task attempts'}

def h_launch_subtasks(state, inp, ctx):
    launched={}; by={s.subtask_id:s for s in state.subtasks}
    for sid in inp.subtask_ids:
        if sid not in by: raise ValueError(f'unknown subtask {sid}')
        st=by[sid]
        if st.status=='accepted': continue
        unmet=[d for d in st.dependencies if by[d].status!='accepted']
        if unmet: continue
        st.status='running'
        for i in range(inp.attempts_per_subtask):
            aid=f'{sid}_attempt_{len(st.attempts)+1:03d}'
            task=f"Parent task:\n{state.task}\n\nSubtask objective:\n{st.objective}\n\nSolve only this subtask scope. Avoid unrelated broad changes."
            a=_run_attempt(state,ctx,aid,'subtask',task,st.success_criteria or state.success_criteria,subtask_id=sid,backend_name=inp.backend_name or ctx.backend_name)
            st.attempts.append(a); launched.setdefault(sid,[]).append(aid)
            if a.status=='completed':
                if ctx.reviewer is not None and not a.review:
                    h_review_attempt(state, OpsReviewAttemptInput(attempt_id=aid,scope='subtask'), ctx)
                if a.acceptance_eligible:
                    st.status='accepted'; st.accepted_attempt_id=aid; ctx.recorder.record('subtask_accepted', payload={'subtask_id':sid,'attempt_id':aid}); break
        if st.status!='accepted': st.status='failed'; ctx.recorder.record('subtask_failed', payload={'subtask_id':sid,'attempts':len(st.attempts)})
    state.phase='integrating' if all(s.status in {'accepted','skipped'} for s in state.subtasks) else 'running_subtasks'; return {'launched':launched,'semantics':'candidate_attempts=N is attempts per subtask with early stop'}
def _find_attempt(state, aid):
    for c in state.candidates:
        if c.attempt_id==aid: return c, None
    for s in state.subtasks:
        for a in s.attempts:
            if a.attempt_id==aid: return a, s
    if state.integration and state.integration.get('attempt_id')==aid: return state.integration, None
    return None, None

def h_review_attempt(state, inp, ctx):
    a, st=_find_attempt(state, inp.attempt_id)
    if not a: raise ValueError(f'unknown attempt {inp.attempt_id}')
    if not isinstance(a, dict) and a.status=='running': raise ValueError('cannot review running attempt')
    if ctx.reviewer is not None:
        raw=ctx.reviewer.review(state=state, attempt=a, scope=inp.scope) if hasattr(ctx.reviewer,'review') else ctx.reviewer(state,a,inp.scope)
        res=OpsReviewResult.model_validate(raw)
    else:
        if not getattr(a,'patch_path',None): raise ValueError('real reviewer unavailable and no patch for deterministic diff review')
        vr=DiffReviewValidator().validate(a.patch_path)
        res=OpsReviewResult(decision='pass' if vr.passed else 'fail',recommended_action='accept' if vr.passed else 'reject',score=vr.score,summary=vr.summary,evidence=vr.evidence,issues=vr.reasons)
    if isinstance(a, dict):
        a['review']=res.model_dump(); a['acceptance_eligible']=res.decision=='pass' and res.recommended_action=='accept'; a['status']='reviewed'
    else:
        a.review=res.model_dump(); a.acceptance_eligible=res.decision=='pass' and res.recommended_action=='accept'; a.acceptance_blockers=[] if a.acceptance_eligible else (res.issues or [res.summary]); a.status='reviewed' if a.status!='failed' else 'rejected'
        if st and a.acceptance_eligible: st.status='accepted'; st.accepted_attempt_id=a.attempt_id
    state.reviews.append({'attempt_id':inp.attempt_id,**res.model_dump()})
    ctx.recorder.record(f'{inp.scope}_attempt_reviewed', payload={'attempt_id':inp.attempt_id,'review_decision':res.decision,'acceptance_eligible':(not isinstance(a,dict) and a.acceptance_eligible) or (isinstance(a,dict) and a.get('acceptance_eligible'))})
    return res.model_dump()
def h_integrate(state, inp, ctx):
    aid='integration_001'; state.integration={'attempt_id':aid,'status':'completed','reason':inp.reason}; state.phase='selecting'; return state.integration
def h_validation(state, inp, ctx):
    if inp.target=='candidate':
        if not inp.target_id: raise ValueError('target_id required for candidate validation')
        a,_=_find_attempt(state, inp.target_id)
        if not a or isinstance(a,dict): raise ValueError(f'unknown candidate {inp.target_id}')
        cwd=Path(a.worktree_path or state.repo_path)
    elif inp.target=='integration':
        cwd=Path((state.integration or {}).get('worktree_path') or state.repo_path)
    else: cwd=Path(state.repo_path)
    results=[]; all_pass=True
    for i,c in enumerate(inp.commands,1):
        ctx.recorder.record('validation_started', payload={'target':inp.target,'target_id':inp.target_id,'cmd':c.cmd})
        outdir=Path(state.run_dir)/'validation'; outdir.mkdir(exist_ok=True); so=outdir/f'{inp.target}_{inp.target_id or "repo"}_{i}.stdout.log'; se=outdir/f'{inp.target}_{inp.target_id or "repo"}_{i}.stderr.log'
        try:
            p=subprocess.run(c.cmd,shell=True,cwd=c.cwd or cwd,text=True,capture_output=True,timeout=c.timeout_seconds or 300)
            so.write_text(p.stdout or ''); se.write_text(p.stderr or ''); passed=p.returncode==0; status='passed' if passed else 'failed'
        except subprocess.TimeoutExpired as e:
            so.write_text(e.stdout or ''); se.write_text((e.stderr or '')+'\ntimeout'); passed=False; status='timeout'
        all_pass=all_pass and passed; item={'cmd':c.cmd,'passed':passed,'status':status,'stdout_path':str(so),'stderr_path':str(se)}; results.append(item)
        ctx.recorder.record('validation_completed' if passed else 'validation_failed', payload={'target':inp.target,'target_id':inp.target_id,'validation_result':item})
    res={'passed':all_pass,'commands':results}
    return res

def h_select_winner(state, inp, ctx):
    if inp.decision=='reject_all':
        if not inp.reasons: raise ValueError('reject_all requires reasons')
        state.selection=inp.model_dump(); state.phase='finalizing'; return state.selection
    if not inp.selected_attempt_id: raise ValueError('selected_attempt_id is required')
    a, st=_find_attempt(state, inp.selected_attempt_id)
    if not a: raise ValueError(f'selected attempt {inp.selected_attempt_id} does not exist')
    if state.execution_path=='decomposed_subtasks' and st is not None: raise ValueError('cannot select raw subtask attempt as final winner')
    if isinstance(a,dict):
        eligible=a.get('acceptance_eligible') is True and a.get('review')
        status=a.get('status')
        blockers=a.get('acceptance_blockers') or []
    else:
        eligible=a.acceptance_eligible and a.review is not None; status=a.status; blockers=a.acceptance_blockers
        if state.execution_path=='parallel_candidates' and a.scope!='candidate': raise ValueError('candidate path selection requires candidate attempt')
    if status=='running': raise ValueError('cannot select running attempt')
    if not eligible or blockers: raise ValueError('selected attempt is not acceptance eligible')
    state.selection=inp.model_dump(); state.phase='finalizing'; return state.selection

def h_finalize(state, inp, ctx):
    if inp.decision=='accepted':
        sel=state.selection or {}; aid=inp.selected_attempt_id or sel.get('selected_attempt_id')
        if sel.get('decision')!='select': raise ValueError('accepted finalization requires select decision')
        a,_=_find_attempt(state, aid)
        if not a: raise ValueError('selected attempt does not exist')
        eligible=(a.get('acceptance_eligible') if isinstance(a,dict) else a.acceptance_eligible)
        if not eligible: raise ValueError('selected attempt is not acceptance eligible')
    state.final_decision=inp.model_dump(); state.status='completed' if inp.decision=='accepted' else 'failed'; state.phase='completed' if state.status=='completed' else 'failed'; return state.final_decision
OPS_TOOLS={
'ops_get_state':ToolSpec('ops_get_state','Inspect canonical run state',OpsGetStateInput,h_get_state,True),
'ops_inspect_repo':ToolSpec('ops_inspect_repo','Inspect repository',OpsInspectRepoInput,h_inspect_repo,True),
'ops_submit_classification':ToolSpec('ops_submit_classification','Submit classification',OpsSubmitClassificationInput,h_classification),
'ops_submit_investigation':ToolSpec('ops_submit_investigation','Submit investigation',OpsSubmitInvestigationInput,h_investigation),
'ops_submit_plan':ToolSpec('ops_submit_plan','Submit orchestration plan',OpsSubmitPlanInput,h_plan),
'ops_submit_decomposition':ToolSpec('ops_submit_decomposition','Submit decomposition',OpsSubmitDecompositionInput,h_decomposition),
'ops_validate_decomposition':ToolSpec('ops_validate_decomposition','Validate decomposition',OpsValidateDecompositionInput,h_validate_decomposition),
'ops_select_execution_path':ToolSpec('ops_select_execution_path','Select execution path',OpsSelectExecutionPathInput,h_select_path),
'ops_launch_candidates':ToolSpec('ops_launch_candidates','Launch candidates',OpsLaunchCandidatesInput,h_launch_candidates),
'ops_launch_subtasks':ToolSpec('ops_launch_subtasks','Launch subtasks',OpsLaunchSubtasksInput,h_launch_subtasks),
'ops_review_attempt':ToolSpec('ops_review_attempt','Review attempt',OpsReviewAttemptInput,h_review_attempt),
'ops_integrate_subtasks':ToolSpec('ops_integrate_subtasks','Integrate subtasks',OpsIntegrateSubtasksInput,h_integrate),
'ops_run_validation':ToolSpec('ops_run_validation','Run validation commands',OpsRunValidationInput,h_validation),
'ops_select_winner':ToolSpec('ops_select_winner','Select winner',OpsSelectWinnerInput,h_select_winner),
'ops_finalize_run':ToolSpec('ops_finalize_run','Finalize run',OpsFinalizeRunInput,h_finalize),
}
def openai_tool_specs():
    return [{'type':'function','function':{'name':n,'description':s.description,'parameters':s.input_model.model_json_schema(),'strict':True}} for n,s in OPS_TOOLS.items()]
