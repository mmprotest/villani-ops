from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
from .state import CandidateAttemptState, SubtaskState, detect_decomposition_deadlock
from .git_artifacts import capture_git_patch, ensure_git_baseline, clean_runner_artifacts_from_worktree, DEFAULT_PATCH_EXCLUDES, is_git_compatible_patch, patch_contains_internal_artifacts
from villani_ops.core.acceptance import is_attempt_acceptance_eligible, attempt_requires_patch
import subprocess, json, time, shutil, os, re
from .artifacts import read_text_utf8, write_text_utf8, write_json_utf8
from concurrent.futures import ThreadPoolExecutor, as_completed


def extract_changed_file_metadata(diff_text:str)->dict[str,list[str]]:
    """Extract stable changed-file metadata from unified/git/binary diffs.

    Prefer ``diff --git a/old b/new`` headers because binary hunks often only
    contain ``Binary files a/foo and b/foo differ`` and older internal formats
    may include ``Binary files differ: rel``.  Never treat grammar words such as
    "files" or "differ:" as paths.
    """
    changed=[]; added=[]; deleted=[]; modified=[]; renamed=[]
    current_old=None; current_new=None; current_status=None; pending_old=None
    def norm(p):
        if not p: return None
        p=p.strip().strip('"')
        if p=='/dev/null': return None
        if p.startswith(('a/','b/')): p=p[2:]
        return p or None
    def add(lst,v):
        v=norm(v)
        if v and v not in lst: lst.append(v)
    def finalize():
        nonlocal current_old,current_new,current_status
        path=current_new or current_old
        if not path: return
        add(changed,path)
        if current_status=='added': add(added,path)
        elif current_status=='deleted': add(deleted,path)
        elif current_status=='renamed': add(renamed,path)
        else: add(modified,path)
    for raw in diff_text.splitlines():
        line=raw.rstrip('\n')
        if line.startswith('diff --git '):
            finalize(); current_status=None
            parts=line.split()
            current_old=norm(parts[2]) if len(parts)>2 else None
            current_new=norm(parts[3]) if len(parts)>3 else current_old
            continue
        if line.startswith('new file mode '): current_status='added'; continue
        if line.startswith('deleted file mode '): current_status='deleted'; continue
        if line.startswith('rename from '):
            current_old=norm(line[len('rename from '):].strip()); current_status='renamed'; continue
        if line.startswith('rename to '):
            current_new=norm(line[len('rename to '):].strip()); current_status='renamed'; add(renamed,current_new); add(changed,current_new); continue
        if line.startswith('--- '):
            pending_old=norm(line[4:].split('\t',1)[0]); continue
        if line.startswith('+++ '):
            new=norm(line[4:].split('\t',1)[0]); old=pending_old; pending_old=None
            if current_old is None: current_old=old
            if current_new is None: current_new=new or old
            if old and not new: current_status=current_status or 'deleted'; add(deleted,old); add(changed,old)
            elif new and not old: current_status=current_status or 'added'; add(added,new); add(changed,new)
            elif new: add(changed,new); add(modified,new)
            elif old: add(changed,old)
            continue
        if line.startswith('Binary files '):
            # Git format: Binary files a/path and b/path differ.  Internal
            # legacy format: Binary files differ: path.
            body=line[len('Binary files '):]
            if ' and ' in body and body.endswith(' differ'):
                left,right=body[:-len(' differ')].split(' and ',1)
                current_old=current_old or norm(left); current_new=current_new or norm(right)
                add(changed,current_new or current_old)
                if current_status=='added': add(added,current_new)
                elif current_status=='deleted': add(deleted,current_old)
                elif current_status=='renamed': add(renamed,current_new)
                else: add(modified,current_new or current_old)
            elif body.startswith('differ:'):
                add(changed, body[len('differ:'):].strip()); add(modified, body[len('differ:'):].strip())
            continue
    finalize()
    return {'changed_files':changed,'added_files':added,'deleted_files':deleted,'modified_files':modified,'renamed_files':renamed}

def _read_text_tail(path, max_chars=12000):
    if not path:
        return None
    try:
        text=read_text_utf8(Path(path), default='')
        return text[-max_chars:]
    except Exception as e:
        return {'error':f'unreadable: {type(e).__name__}: {e}', 'path':str(path)}

def _read_patch_excerpt(path, max_chars=24000):
    return _read_text_tail(path, max_chars=max_chars)

def _attempt_to_dict(a):
    return a.model_dump(mode='json') if hasattr(a,'model_dump') else dict(a)

def build_agentic_review_payload(state, attempt, scope, subtask=None):
    data=_attempt_to_dict(attempt)
    validation=data.get('validation') or {}
    validation_tails=[]
    for item in validation.get('commands') or []:
        validation_tails.append({**item, 'stdout_tail':_read_text_tail(item.get('stdout_path'), max_chars=4000), 'stderr_tail':_read_text_tail(item.get('stderr_path'), max_chars=4000)})
    payload={
        'parent_task':state.task,
        'success_criteria':state.success_criteria,
        'execution_path':state.execution_path,
        'scope':scope,
        'attempt':data,
        'changed_files':data.get('changed_files') or [],
        'patch_excerpt':_read_patch_excerpt(data.get('patch_path')),
        'stdout_tail':_read_text_tail(data.get('stdout_path')),
        'stderr_tail':_read_text_tail(data.get('stderr_path')),
        'transcript_tail':_read_text_tail(data.get('transcript_path')),
        'validation':{**validation, 'commands':validation_tails},
        'failure_reason':data.get('failure_reason') or data.get('error'),
        'exit_code':data.get('exit_code'),
        'artifact_paths':{k:data.get(k) for k in ['artifacts_dir','worktree_path','patch_path','stdout_path','stderr_path','transcript_path']},
        'known_blockers':data.get('acceptance_blockers') or [],
        'requires_patch':attempt_requires_patch(state, attempt),
        'patch_hygiene':data.get('patch_hygiene') or {},
        'investigation_relevant_files':(state.investigation or {}).get('relevant_files') if state.investigation else [],
    }
    if subtask is not None:
        payload['subtask']={'id':subtask.subtask_id,'title':subtask.title,'objective':subtask.objective,'success_criteria':subtask.success_criteria,'relevant_files':subtask.relevant_files}
    return payload

def _set_acceptance_from_gate(state, attempt):
    eligible, blockers=is_attempt_acceptance_eligible(attempt, state=state)
    if isinstance(attempt, dict):
        attempt['acceptance_eligible']=eligible; attempt['acceptance_blockers']=blockers
    else:
        attempt.acceptance_eligible=eligible; attempt.acceptance_blockers=blockers
    return eligible, blockers

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
class OpsStartCandidateFallbackInput(StrictModel): reason:str; attempts:int|None=None
class OpsLaunchSubtasksInput(StrictModel): subtask_ids:list[str]; backend_name:str|None=None; attempts_per_subtask:int; reason:str
class OpsReviewAttemptInput(StrictModel): attempt_id:str; scope:Literal['candidate','subtask','integration']
class OpsReviewResult(StrictModel): decision:Literal['pass','fail']; recommended_action:Literal['accept','reject','retry','repair']; score:float; summary:str; evidence:list[str]=Field(default_factory=list); issues:list[str]=Field(default_factory=list); blockers:list[str]=Field(default_factory=list); confidence:float=0.0; subtask_passed:bool|None=None; scope_ok:bool|None=None; integration_risk:Literal['low','medium','high','unknown']|None=None
class OpsIntegrateSubtasksInput(StrictModel): reason:str
class OpsRunValidationInput(StrictModel): commands:list[ValidationCommand]; target:Literal['candidate','integration','repo']; target_id:str|None=None; allow_cwd_escape:bool=False
class OpsSelectWinnerInput(StrictModel): selected_attempt_id:str|None=None; decision:Literal['select','reject_all']; summary:str; reasons:list[str]=Field(default_factory=list); rejected_attempts:list[str]=Field(default_factory=list); confidence:float
class OpsFinalizeRunInput(StrictModel): decision:Literal['accepted','rejected','failed']; summary:str; selected_attempt_id:str|None=None; selected_patch_path:str|None=None; blockers:list[str]=Field(default_factory=list)
@dataclass
class ToolSpec: name:str; description:str; input_model:type[BaseModel]; handler:Callable; read_only:bool=False

def h_get_state(state, inp, ctx): return {'status':state.status,'phase':state.phase,'execution_path':state.execution_path,'fallback_execution_path':state.fallback_execution_path,'fallback_used':state.fallback_used,'decomposed_execution_status':state.decomposed_execution_status,'decomposed_execution_blockers':state.decomposed_execution_blockers,'allowed_next_actions':state.allowed_next_actions(),'decomposition_accepted':state.decomposition_accepted,'subtasks':[s.model_dump() for s in state.subtasks],'candidates':[c.model_dump() for c in state.candidates],'warnings':state.warnings,'recovery_count':state.recovery_count}
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
    if inp.path=='decomposed_subtasks': state.decomposed_execution_status='running'
    if inp.path=='parallel_candidates' and state.decomposition is not None and state.decomposition_accepted is not True:
        state.decomposition_fallback_used=True; state.decomposition_fallback_reason=inp.reason; state.decomposition_executed=False
    return {'execution_path':state.execution_path,'reason':inp.reason,'decomposition_fallback_used':state.decomposition_fallback_used}
def _attempt(aid, scope, subtask_id=None, backend=None, artifacts=None):
    return CandidateAttemptState(attempt_id=aid,backend_name=backend,status='scheduled',scope=scope,subtask_id=subtask_id,artifacts_dir=str(artifacts) if artifacts else None,acceptance_eligible=False)
def _copy_worktree(src:Path, dst:Path):
    ignore=shutil.ignore_patterns('.git','.villani-ops','.v','__pycache__')
    shutil.copytree(src,dst,ignore=ignore,dirs_exist_ok=True)

def _is_fake_dependency(obj):
    name=(getattr(obj,'name',None) or getattr(obj,'__class__',type('',(),{})).__name__ or '').lower()
    return 'fake' in name or 'placeholder' in name or name.startswith('_test')

def _require_real_execution(ctx):
    if ctx.production and not ctx.allow_fake_dependencies:
        if ctx.runner_adapter is None: raise ValueError('agentic_runner_adapter_missing')
        if _is_fake_dependency(ctx.runner_adapter): raise ValueError('fake runner dependency forbidden in production agentic mode')
        if ctx.coding_backend is None and ctx.backend is None: raise ValueError('agentic_backend_role_unavailable: coding')
        if _is_fake_dependency(ctx.coding_backend or ctx.backend): raise ValueError('fake coding backend forbidden in production agentic mode')

def _run_attempt(state, ctx, aid, scope, task, success, subtask_id=None, backend_name=None, record_events=True):
    _require_real_execution(ctx)
    backend=ctx.coding_backend or ctx.backend
    if ctx.runner_adapter is None: raise ValueError('agentic_runner_adapter_missing')
    if backend is None: raise ValueError('agentic_backend_role_unavailable: coding')
    adir=Path(state.run_dir)/'attempts'/aid; adir.mkdir(parents=True,exist_ok=True)
    wtree=adir/'worktree'
    a=_attempt(aid,scope,subtask_id=subtask_id,backend=backend_name or ctx.coding_backend_name or getattr(backend,'name',None),artifacts=adir)
    a.status='running'; a.worktree_path=str(wtree); a.started_at=str(time.time())
    if record_events:
        ctx.recorder.record(f'{scope}_attempt_started', payload={'attempt_id':aid,'subtask_id':subtask_id,'status':'running','artifact_paths':{'artifacts_dir':str(adir)}})
    try:
        _copy_worktree(Path(state.repo_path), wtree)
        ensure_git_baseline(wtree)
        res=ctx.runner_adapter.run_task(repo_path=wtree,task=task,success_criteria=success,backend_name=a.backend_name or '',backend_config=backend,timeout_seconds=ctx.timeout_seconds,context={'attempt_id':aid,'subtask_id':subtask_id,'parent_task':state.task},artifacts_dir=adir)
        write_text_utf8(adir/'stdout.log', getattr(res,'stdout','') or ''); write_text_utf8(adir/'stderr.log', getattr(res,'stderr','') or '')
        cap=capture_git_patch(wtree, adir/'diff.patch', exclude_patterns=DEFAULT_PATCH_EXCLUDES)
        a.stdout_path=str(adir/'stdout.log'); a.stderr_path=str(adir/'stderr.log'); a.patch_path=cap.patch_path; a.transcript_path=getattr(res,'telemetry_path',None)
        a.changed_files=cap.changed_files; a.added_files=cap.added_files; a.deleted_files=cap.deleted_files; a.modified_files=cap.modified_files; a.renamed_files=cap.renamed_files
        a.patch_hygiene={'format_valid': bool(cap.patch_path and is_git_compatible_patch(cap.patch_path)), 'contains_internal_artifacts': bool(cap.patch_path and patch_contains_internal_artifacts(cap.patch_path)), 'apply_check_passed': None, 'capture_failure_reason': cap.failure_reason, 'changed_files_after_filtering': cap.changed_files}
        if cap.patch_path:
            chk=subprocess.run(['git','apply','--check','--cached',cap.patch_path],cwd=wtree,text=True,capture_output=True)
            a.patch_hygiene['apply_check_passed']=chk.returncode==0
            if chk.returncode!=0:
                a.acceptance_blockers=sorted(set(a.acceptance_blockers+['patch_apply_check_failed']))
                write_text_utf8(adir/'patch_apply_check_stderr.log', chk.stderr or '')
        a.model=getattr(backend,'model',None); a.completed_at=str(time.time())
        ok=getattr(res,'exit_code',1)==0
        a.exit_code=getattr(res,'exit_code',None); a.exit_reason=getattr(res,'exit_reason',None); a.runner_status=getattr(res,'status',None)
        a.status='completed' if ok else 'failed'
        if not ok: a.failure_reason=(getattr(res,'stderr','') or getattr(res,'status',None) or f'runner exit code is {a.exit_code}')[:1000]
        ev=f'{scope}_attempt_completed' if ok else f'{scope}_attempt_failed'
        if record_events:
            ctx.recorder.record(ev, payload={'attempt_id':aid,'subtask_id':subtask_id,'status':a.status,'exit_code':getattr(res,'exit_code',None),'failure_reason':getattr(res,'stderr','') if not ok else None,'artifact_paths':{'stdout':a.stdout_path,'stderr':a.stderr_path,'patch':a.patch_path}})
    except Exception as e:
        a.status='failed'; a.completed_at=str(time.time()); a.duration_seconds=float(a.completed_at)-float(a.started_at or a.completed_at); a.failure_reason=f'{type(e).__name__}: {e}'; a.runner_error_type=type(e).__name__; a.runner_status='exception'; a.acceptance_eligible=False; a.acceptance_blockers=['runner_exception', f'runner_exception: {type(e).__name__}: {e}']
        a.stdout_path=str(adir/'stdout.log'); a.stderr_path=str(adir/'stderr.log')
        if not Path(a.stdout_path).exists(): write_text_utf8(Path(a.stdout_path), '')
        write_text_utf8(Path(a.stderr_path), f'{type(e).__name__}: {e}\n')
        if record_events:
            ctx.recorder.record(f'{scope}_attempt_failed', payload={'attempt_id':aid,'subtask_id':subtask_id,'status':'failed','failure_reason':a.acceptance_blockers[0],'artifact_paths':{'stdout':a.stdout_path,'stderr':a.stderr_path,'artifacts_dir':str(adir)}})
    if a.completed_at and a.started_at and a.duration_seconds is None:
        a.duration_seconds=float(a.completed_at)-float(a.started_at)
    _set_acceptance_from_gate(state,a)
    return a

def h_start_candidate_fallback(state, inp, ctx):
    if state.execution_path!='decomposed_subtasks' or state.decomposed_execution_status not in {'blocked','failed'}:
        raise ValueError('candidate fallback requires blocked or failed decomposed execution')
    state.fallback_used=True; state.fallback_from_execution_path='decomposed_subtasks'; state.fallback_execution_path='parallel_candidates_after_decomposition_deadlock'
    state.fallback_reason=inp.reason or 'required subtask failed and dependent subtasks are blocked'; state.fallback_started_at=str(time.time()); state.phase='running_candidates'
    ctx.recorder.record('candidate_fallback_started', payload={'reason':state.fallback_reason,'fallback_from_execution_path':state.fallback_from_execution_path,'fallback_execution_path':state.fallback_execution_path})
    return {'fallback_used':state.fallback_used,'fallback_execution_path':state.fallback_execution_path,'fallback_reason':state.fallback_reason,'attempts':inp.attempts}

def h_launch_candidates(state, inp, ctx):
    fallback_active=state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock'
    if state.execution_path!='parallel_candidates' and not fallback_active: raise ValueError('candidates require parallel_candidates execution path or explicit decomposition-deadlock fallback')
    made=[]; maxp=max(1, int(getattr(ctx.coding_backend or ctx.backend,'max_parallel',None) or ctx.max_parallel or 1))
    state.max_parallel=maxp
    total=int(inp.attempts); batch_count=0; final_mode='sequential_due_max_parallel_1' if maxp<=1 or total<=1 else 'parallel_candidates'
    next_index=len(state.candidates)+1
    for off in range(0,total,maxp):
        batch=list(range(off, min(off+maxp,total))); batch_count+=1
        ids=[]; futs=[]
        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            for _i in batch:
                aid=f'candidate_{next_index:03d}'; next_index+=1
                ids.append(aid); made.append(aid)
                scheduled=_attempt(aid,'candidate',backend=inp.backend_name or ctx.coding_backend_name or ctx.backend_name,artifacts=Path(state.run_dir)/'attempts'/aid)
                scheduled.status='running'; scheduled.started_at=str(time.time()); scheduled.worktree_path=str(Path(state.run_dir)/'attempts'/aid/'worktree')
                state.candidates.append(scheduled)
                ctx.recorder.record('candidate_attempt_started', payload={'attempt_id':aid,'status':'running','batch_index':batch_count,'fallback':fallback_active,'artifact_paths':{'artifacts_dir':scheduled.artifacts_dir}})
                futs.append(ex.submit(_run_attempt,state,ctx,aid,'candidate',state.task,state.success_criteria,None,inp.backend_name or ctx.coding_backend_name or ctx.backend_name,False))
            byid={}
            for fut in as_completed(futs):
                res=fut.result(); byid[res.attempt_id]=res
        for aid in ids:
            res=byid[aid]
            for idx,c in enumerate(state.candidates):
                if c.attempt_id==aid:
                    state.candidates[idx]=res; break
            ev='candidate_attempt_completed' if res.status=='completed' else 'candidate_attempt_failed'
            ctx.recorder.record(ev, payload={'attempt_id':aid,'status':res.status,'exit_code':res.exit_code,'failure_reason':res.failure_reason,'batch_index':batch_count,'fallback':fallback_active,'artifact_paths':{'stdout':res.stdout_path,'stderr':res.stderr_path,'patch':res.patch_path}})
        state.save(Path(state.run_dir)/'state.json')
    state.concurrency_mode=final_mode; state.batch_count=batch_count
    state.candidate_concurrency={'concurrency_mode':final_mode,'max_parallel':maxp,'batch_count':batch_count,'worker_state_mutation':'disabled'}
    state.execution_concurrency={'candidate_concurrency_mode':final_mode,'max_parallel':maxp,'candidate_batch_count':batch_count}
    state.phase='selecting'; return {'launched':made,'max_parallel':maxp,'batch_count':batch_count,'concurrency_mode':final_mode,'semantics':'attempts execute in isolated worktrees; main thread mutates OpsRunState; batches never exceed max_parallel'}

def _update_decomposed_execution_state(state, ctx=None):
    if state.execution_path!='decomposed_subtasks': return None
    state.decomposed_execution_completed_subtasks=sorted(s.subtask_id for s in state.subtasks if s.status=='accepted')
    state.decomposed_execution_failed_subtasks=sorted(s.subtask_id for s in state.subtasks if s.status=='failed')
    state.decomposed_execution_blocked_subtasks=sorted(s.subtask_id for s in state.subtasks if s.status=='skipped')
    dead=detect_decomposition_deadlock(state)
    if dead:
        state.decomposed_execution_status='blocked'; state.decomposed_execution_failed_subtasks=dead.failed_subtasks; state.decomposed_execution_blocked_subtasks=dead.blocked_subtasks
        state.decomposed_execution_blockers=sorted(set((state.decomposed_execution_blockers or []) + dead.reason.split(',') + ['decomposition_deadlocked']))
        state.partial_progress={'accepted_subtasks':dead.accepted_subtasks,'failed_subtasks':dead.failed_subtasks,'blocked_subtasks':dead.blocked_subtasks}
        for st in state.subtasks:
            if st.status=='accepted' and st.accepted_attempt_id: state.best_partial_attempt_id=st.accepted_attempt_id
        if ctx: ctx.recorder.record('decomposition_deadlock_detected', payload=dead.model_dump())
        return dead
    if state.subtasks and all(s.status in {'accepted','skipped'} for s in state.subtasks): state.decomposed_execution_status='completed'
    elif any(s.status in {'running','pending'} for s in state.subtasks): state.decomposed_execution_status='running'
    return None

def h_launch_subtasks(state, inp, ctx):
    by={s.subtask_id:s for s in state.subtasks}
    for sid in inp.subtask_ids:
        if sid not in by: raise ValueError(f'unknown subtask {sid}')
    maxp=max(1,int(getattr(ctx.coding_backend or ctx.backend,'max_parallel',None) or ctx.max_parallel or 1)); state.max_parallel=maxp
    pending={sid for sid in inp.subtask_ids if by[sid].status not in {'accepted','failed','skipped'}}
    launched={}; waves=[]; wave_index=0
    mode='sequential_due_max_parallel_1' if maxp<=1 else 'parallel_runner_sequential_review'
    while pending:
        ready=[sid for sid in inp.subtask_ids if sid in pending and all(by[d].status=='accepted' for d in by[sid].dependencies)]
        blocked=[sid for sid in pending if sid not in ready]
        if not ready:
            for sid in list(pending):
                st=by[sid]; st.status='skipped'
                ctx.recorder.record('subtask_blocked', payload={'subtask_id':sid,'reason':'unmet_dependencies','dependencies':st.dependencies,'blocked_dependencies':[d for d in st.dependencies if by[d].status!='accepted']})
            waves.append({'wave_index':wave_index+1,'ready_subtasks':[],'blocked_subtasks':sorted(blocked),'batch_size':0})
            break
        # Attempt loop for this dependency wave.  A subtask remains in the wave
        # until accepted or attempts are exhausted; reviews are serialized on
        # the main thread while runner attempts in a batch are concurrent.
        wave_active=list(ready); wave_index+=1
        for sid in wave_active: by[sid].status='running'
        for attempt_round in range(inp.attempts_per_subtask):
            runnable=[sid for sid in wave_active if by[sid].status=='running']
            if not runnable: break
            for off in range(0,len(runnable),maxp):
                batch=runnable[off:off+maxp]
                waves.append({'wave_index':wave_index,'ready_subtasks':batch,'blocked_subtasks':sorted(blocked),'batch_size':len(batch),'attempt_round':attempt_round+1})
                futs={}; ids={}
                with ThreadPoolExecutor(max_workers=len(batch)) as ex:
                    for sid in batch:
                        st=by[sid]; aid=f'{sid}_attempt_{len(st.attempts)+1:03d}'
                        task=f"Parent task:\n{state.task}\n\nParent success criteria:\n{state.success_criteria or ''}\n\nSubtask title:\n{st.title}\n\nSubtask objective:\n{st.objective}\n\nSubtask success criteria:\n{st.success_criteria or ''}\n\nRelevant files:\n{json.dumps(st.relevant_files, ensure_ascii=False)}\n\nDependency context:\n{json.dumps(st.dependencies, ensure_ascii=False)}\n\nMerge contract:\n{(state.decomposition or {}).get('merge_strategy') or ''}\n\nSolve only this subtask scope. Avoid unrelated broad changes. Do not perform the entire parent task unless this subtask explicitly requires it."
                        scheduled=_attempt(aid,'subtask',subtask_id=sid,backend=inp.backend_name or ctx.coding_backend_name or ctx.backend_name,artifacts=Path(state.run_dir)/'attempts'/aid)
                        scheduled.status='running'; scheduled.started_at=str(time.time()); scheduled.worktree_path=str(Path(state.run_dir)/'attempts'/aid/'worktree')
                        st.attempts.append(scheduled); launched.setdefault(sid,[]).append(aid); ids[aid]=sid
                        ctx.recorder.record('subtask_attempt_started', payload={'attempt_id':aid,'subtask_id':sid,'wave_index':wave_index,'attempt_round':attempt_round+1})
                        futs[ex.submit(_run_attempt,state,ctx,aid,'subtask',task,st.success_criteria or state.success_criteria,subtask_id=sid,backend_name=inp.backend_name or ctx.coding_backend_name or ctx.backend_name,record_events=False)]=aid
                    results={}
                    for fut in as_completed(futs):
                        res=fut.result(); results[res.attempt_id]=res
                for aid,res in results.items():
                    sid=ids[aid]; st=by[sid]
                    for i,a in enumerate(st.attempts):
                        if a.attempt_id==aid: st.attempts[i]=res; break
                    ctx.recorder.record('subtask_attempt_completed' if res.status=='completed' else 'subtask_attempt_failed', payload={'attempt_id':aid,'subtask_id':sid,'status':res.status,'exit_code':res.exit_code,'failure_reason':res.failure_reason,'wave_index':wave_index})
                # Serialized review on main thread.
                for aid in [a for a in ids if a in results]:
                    sid=ids[aid]; st=by[sid]
                    a,_=_find_attempt(state, aid)
                    if a and not isinstance(a,dict) and a.status=='completed' and ctx.reviewer is not None and not a.review:
                        h_review_attempt(state, OpsReviewAttemptInput(attempt_id=aid,scope='subtask'), ctx)
                    if st.accepted_attempt_id:
                        st.status='accepted'; ctx.recorder.record('subtask_accepted', payload={'subtask_id':sid,'attempt_id':st.accepted_attempt_id,'wave_index':wave_index})
                state.save(Path(state.run_dir)/'state.json')
        for sid in wave_active:
            st=by[sid]
            if st.status!='accepted':
                st.status='failed'; ctx.recorder.record('subtask_failed', payload={'subtask_id':sid,'attempts':len(st.attempts),'reason':'attempts_exhausted'})
            pending.discard(sid)
        # Block dependents immediately if any dependency failed.
        for sid in list(pending):
            failed_deps=[d for d in by[sid].dependencies if by[d].status in {'failed','skipped'}]
            if failed_deps:
                by[sid].status='skipped'; pending.remove(sid); ctx.recorder.record('subtask_blocked', payload={'subtask_id':sid,'reason':'failed_dependencies','blocked_dependencies':failed_deps})
    state.wave_count=wave_index; state.concurrency_mode=mode
    state.subtask_concurrency={'concurrency_mode':mode,'max_parallel':maxp,'wave_count':wave_index,'waves':waves,'review':'sequential_main_thread'}
    state.execution_concurrency={**(state.execution_concurrency or {}),'subtask_concurrency_mode':mode,'subtask_wave_count':wave_index,'max_parallel':maxp}
    dead=_update_decomposed_execution_state(state, ctx)
    state.phase='running_subtasks' if dead else ('integrating' if all(s.status in {'accepted','skipped'} for s in state.subtasks) else 'running_subtasks')
    return {'launched':launched,'max_parallel':maxp,'wave_count':wave_index,'concurrency_mode':mode,'waves':waves,'semantics':'dependency waves; runner attempts concurrent up to max_parallel; reviews serialized on main thread'}

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
    if isinstance(a, dict) and a.get('status')=='running': raise ValueError('cannot review running attempt')
    if ctx.reviewer is None: raise ValueError('agentic_real_reviewer_not_configured')
    if ctx.production and not ctx.allow_fake_dependencies and _is_fake_dependency(ctx.reviewer): raise ValueError('fake reviewer dependency forbidden in production agentic mode')
    payload=build_agentic_review_payload(state, a, inp.scope, st)
    raw=None
    try:
        try:
            raw=ctx.reviewer.review(state=state, attempt=payload, scope=inp.scope) if hasattr(ctx.reviewer,'review') else ctx.reviewer(state,payload,inp.scope)
        except TypeError:
            raw=ctx.reviewer.review(state=state, attempt=a, scope=inp.scope) if hasattr(ctx.reviewer,'review') else ctx.reviewer(state,a,inp.scope)
        res=OpsReviewResult.model_validate(raw)
    except Exception as e:
        res=OpsReviewResult(decision='fail',recommended_action='reject',score=0.0,summary='structured review failed',evidence=[],issues=[f'{type(e).__name__}: {e}'],blockers=['review_malformed'],confidence=0.0)
        rdir=Path(state.run_dir)/'reviews'; rdir.mkdir(parents=True,exist_ok=True)
        raw_path=rdir/f'{inp.attempt_id}_malformed_review.json'
        write_json_utf8(raw_path, {'raw_response':raw,'error':f'{type(e).__name__}: {e}'})
        if isinstance(a, dict): a.setdefault('review_artifacts',[]).append(str(raw_path))
        else: a.acceptance_blockers=sorted(set(a.acceptance_blockers+['review_malformed']))
    if isinstance(a, dict):
        a['review']=res.model_dump(); a['status']='reviewed' if a.get('status') not in {'failed','completed'} else a.get('status')
        eligible, blockers=_set_acceptance_from_gate(state, a)
        if res.blockers:
            blockers=sorted(set(blockers+res.blockers)); a['acceptance_blockers']=blockers; eligible=False; a['acceptance_eligible']=False
    else:
        a.review=res.model_dump(); a.status='reviewed' if a.status!='failed' else 'rejected'
        eligible, blockers=_set_acceptance_from_gate(state, a)
        if res.blockers:
            blockers=sorted(set(blockers+res.blockers)); a.acceptance_blockers=blockers; eligible=False; a.acceptance_eligible=False
        if st and eligible:
            st.status='accepted'; st.accepted_attempt_id=a.attempt_id
    state.reviews.append({'attempt_id':inp.attempt_id,**res.model_dump(),'acceptance_eligible':eligible,'acceptance_blockers':blockers})
    if not eligible and blockers:
        state.blockers=sorted(set(state.blockers+blockers))
    ctx.recorder.record(f'{inp.scope}_attempt_reviewed', payload={'attempt_id':inp.attempt_id,'review_decision':res.decision,'review_recommended_action':res.recommended_action,'central_acceptance_eligible':eligible,'acceptance_eligible':eligible,'acceptance_blockers':blockers,'validation_blocked':any(b.startswith('validation_') for b in blockers),'artifact_blocked':any(b in {'missing_patch','empty_changed_files','patch_unreadable'} for b in blockers)})
    return {**res.model_dump(),'acceptance_eligible':eligible,'acceptance_blockers':blockers,'review_payload_included':['patch_excerpt','stdout_tail','stderr_tail','validation']}
def _accepted_subtask_attempt(state, st):
    for a in st.attempts:
        if a.attempt_id==st.accepted_attempt_id:
            return a
    return None

def _subtasks_dependency_order(subtasks):
    by={s.subtask_id:s for s in subtasks}; order=[]; temp=set(); perm=set()
    def visit(sid):
        if sid in perm: return
        if sid in temp: raise ValueError('dependency cycle detected during integration ordering')
        temp.add(sid)
        for dep in by[sid].dependencies:
            if dep in by: visit(dep)
        temp.remove(sid); perm.add(sid); order.append(by[sid])
    for s in subtasks: visit(s.subtask_id)
    return order

def _git_apply_patch_path(patch_path, idir, subtask_id):
    text=read_text_utf8(Path(patch_path), default='')
    if any(line.startswith(('Added file: ','Deleted file: ','Modified file: ','Binary files differ:')) for line in text.splitlines()):
        cleaned='\n'.join(line for line in text.splitlines() if not line.startswith(('Added file: ','Deleted file: ','Modified file: ')))+'\n'
        tmp=idir/f'git_apply_input_{subtask_id}.patch'; write_text_utf8(tmp, cleaned); return str(tmp)
    return patch_path

def _write_integration_failure_artifacts(idir, subtask_id, patch_path, check=None, apply=None):
    arts={}
    if patch_path:
        dst=idir/f'failed_patch_{subtask_id}.patch'
        try: write_text_utf8(dst, read_text_utf8(Path(patch_path)))
        except Exception as e: write_text_utf8(dst, f'unreadable patch {patch_path}: {type(e).__name__}: {e}\n')
        arts['failed_patch']=str(dst)
    if check is not None:
        so=idir/'git_apply_check_stdout.log'; se=idir/'git_apply_check_stderr.log'; write_text_utf8(so, check.stdout or ''); write_text_utf8(se, check.stderr or '')
        arts['git_apply_check_stdout']=str(so); arts['git_apply_check_stderr']=str(se)
    if apply is not None:
        so=idir/'git_apply_stdout.log'; se=idir/'git_apply_stderr.log'; write_text_utf8(so, apply.stdout or ''); write_text_utf8(se, apply.stderr or '')
        arts['git_apply_stdout']=str(so); arts['git_apply_stderr']=str(se)
    return arts

def _integration_failure_reason(failed, conflicts):
    items=failed or conflicts or []
    if not items: return 'integration_failed'
    first=items[0]
    reason=first.get('reason') or 'integration_failed'
    aid=first.get('attempt_id') or first.get('subtask_id') or ''
    if reason=='patch_apply_check_failed': return f'patch apply failed for {aid}'.strip()
    if reason=='invalid_patch_format': return f'invalid patch format in {aid}'.strip()
    if reason=='patch_contains_internal_artifacts': return 'patch contains internal artifacts'
    return reason

def h_integrate(state, inp, ctx):
    if state.execution_path!='decomposed_subtasks': raise ValueError('integration requires decomposed_subtasks execution path')
    if state.decomposition_accepted is not True: raise ValueError('integration requires accepted decomposition')
    running=[s.subtask_id for s in state.subtasks if s.status=='running']
    if running: raise ValueError(f'cannot integrate; subtasks still running: {running}')
    unaccepted=[s.subtask_id for s in state.subtasks if s.status not in {'accepted','skipped'}]
    if unaccepted:
        state.integration={'attempt_id':'integration_001','scope':'integration','status':'failed','reason':inp.reason,'failure_reason':'subtasks_incomplete','failed_subtasks':unaccepted,'acceptance_eligible':False,'acceptance_blockers':['subtasks_incomplete']}
        ctx.recorder.record('integration_failed', payload=state.integration); return state.integration
    ctx.recorder.record('integration_started', payload={'accepted_subtasks':sum(1 for s in state.subtasks if s.status=='accepted')})
    iid='integration_001'; idir=Path(state.run_dir)/'integration'/iid; idir.mkdir(parents=True,exist_ok=True)
    wtree=idir/'worktree'; started=str(time.time()); conflicts=[]; applied=[]; failed=[]
    _copy_worktree(Path(state.repo_path), wtree)
    ensure_git_baseline(wtree)
    try:
        ordered_subtasks=_subtasks_dependency_order(state.subtasks)
    except ValueError as e:
        state.integration={'attempt_id':iid,'scope':'integration','status':'failed','reason':inp.reason,'failure_reason':str(e),'failed_subtasks':[{'reason':str(e)}],'acceptance_eligible':False,'acceptance_blockers':['integration_ordering_failed'], 'conflict_artifacts':[]}
        write_json_utf8(idir/'integration_failure.json', state.integration, atomic=True)
        ctx.recorder.record('integration_failed', payload=state.integration); return state.integration
    for st in ordered_subtasks:
        if st.status=='skipped': continue
        a=_accepted_subtask_attempt(state, st)
        if not a or not a.patch_path:
            failed.append({'subtask_id':st.subtask_id,'reason':'missing accepted patch'}); continue
        apply_path=a.patch_path
        if not Path(apply_path).exists():
            failed.append({'subtask_id':st.subtask_id,'attempt_id':a.attempt_id,'patch_path':a.patch_path,'reason':'missing_patch'}); continue
        if patch_contains_internal_artifacts(apply_path):
            arts=_write_integration_failure_artifacts(idir, st.subtask_id, a.patch_path)
            item={'subtask_id':st.subtask_id,'attempt_id':a.attempt_id,'patch_path':a.patch_path,'reason':'patch_contains_internal_artifacts','artifact_paths':arts}
            conflicts.append(item); failed.append(item); continue
        if not is_git_compatible_patch(apply_path):
            arts=_write_integration_failure_artifacts(idir, st.subtask_id, a.patch_path)
            item={'subtask_id':st.subtask_id,'attempt_id':a.attempt_id,'patch_path':a.patch_path,'reason':'invalid_patch_format','artifact_paths':arts}
            conflicts.append(item); failed.append(item); continue
        check=subprocess.run(['git','apply','--check','--whitespace=nowarn',apply_path],cwd=wtree,text=True,capture_output=True)
        if check.returncode!=0:
            arts=_write_integration_failure_artifacts(idir, st.subtask_id, a.patch_path, check=check)
            item={'subtask_id':st.subtask_id,'attempt_id':a.attempt_id,'patch_path':a.patch_path,'exit_code':check.returncode,'stderr_tail':(check.stderr or '')[-4000:],'artifact_paths':arts}
            conflicts.append(item); failed.append({**item,'reason':'patch_apply_check_failed'}); continue
        apply=subprocess.run(['git','apply','--whitespace=nowarn',apply_path],cwd=wtree,text=True,capture_output=True)
        if apply.returncode!=0:
            arts=_write_integration_failure_artifacts(idir, st.subtask_id, a.patch_path, check=check, apply=apply)
            item={'subtask_id':st.subtask_id,'attempt_id':a.attempt_id,'patch_path':a.patch_path,'exit_code':apply.returncode,'stderr_tail':(apply.stderr or '')[-4000:],'artifact_paths':arts}
            conflicts.append(item); failed.append({**item,'reason':'patch_apply_failed'}); continue
        applied.append({'subtask_id':st.subtask_id,'attempt_id':a.attempt_id,'patch_path':a.patch_path})
    cap=capture_git_patch(wtree, idir/'diff.patch', exclude_patterns=DEFAULT_PATCH_EXCLUDES)
    patch=cap.patch_path or str(idir/'diff.patch')
    meta={'changed_files':cap.changed_files,'added_files':cap.added_files,'deleted_files':cap.deleted_files,'modified_files':cap.modified_files,'renamed_files':cap.renamed_files}
    status='failed' if failed or conflicts else 'completed'
    blockers=[]
    if failed: blockers.append('integration_failed')
    if conflicts: blockers.append('merge_conflicts')
    integ={'attempt_id':iid,'scope':'integration','status':status,'reason':inp.reason,'worktree_path':str(wtree),'artifacts_dir':str(idir),'patch_path':str(patch),'changed_files':meta['changed_files'],'added_files':meta['added_files'],'deleted_files':meta['deleted_files'],'modified_files':meta['modified_files'],'renamed_files':meta['renamed_files'],'merge_conflicts':conflicts,'conflict_artifacts':[p for c in conflicts for p in ((c.get('artifact_paths') or {}).values())],'applied_subtasks':applied,'applied_subtask_order':[x['subtask_id'] for x in applied],'failed_subtasks':failed,'failure_reason':(_integration_failure_reason(failed, conflicts) if status=='failed' else None),'started_at':started,'completed_at':str(time.time()),'acceptance_eligible':False,'acceptance_blockers':blockers or ['review_missing'],'review':None,'validation':None}
    state.integration=integ; _set_acceptance_from_gate(state, state.integration)
    if status=='failed':
        write_json_utf8(idir/'integration_failure.json', state.integration, atomic=True)
        write_text_utf8(idir/'integration_conflicts.txt', '\n'.join((c.get('stderr_tail') or c.get('stderr') or '') for c in conflicts))
    state.phase='selecting' if status=='completed' else 'failed'
    if status=='failed': state.last_error=state.integration.get('failure_reason')
    ctx.recorder.record('integration_completed' if status=='completed' else 'integration_failed', payload=state.integration)
    return state.integration

def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve()); return True
    except Exception:
        return False

def _target_label(target: str, target_id: str | None) -> str:
    return target_id if target in {'candidate','integration'} and target_id else target

def _resolve_validation_target(state, inp):
    if inp.target=='candidate':
        if not inp.target_id: raise ValueError('target_id required for candidate validation')
        a, st=_find_attempt(state, inp.target_id)
        if not a or isinstance(a,dict) or getattr(a,'scope',None) not in {'candidate','subtask'}: raise ValueError(f'unknown candidate/subtask attempt {inp.target_id}')
        if not a.worktree_path: raise ValueError(f'attempt {inp.target_id} has no worktree_path')
        return a, Path(a.worktree_path), st
    if inp.target=='integration':
        integ=state.integration
        if not integ: raise ValueError('integration validation requires integration result')
        w=integ.get('worktree_path')
        if not w: raise ValueError('integration result has no worktree_path')
        return integ, Path(w), None
    return None, Path(state.repo_path), None

def _resolve_command_cwd(cwd_value: str | None, base: Path, *, target: str, allow_escape: bool) -> Path:
    if not cwd_value:
        return base.resolve()
    cwd=Path(cwd_value)
    resolved=(cwd if cwd.is_absolute() else base/cwd).resolve()
    if target in {'candidate','integration'} and not allow_escape and not _is_relative_to(resolved, base.resolve()):
        raise ValueError(f'targeted validation cwd must stay inside the {target} worktree')
    return resolved

def _embedded_cd_error(cmd: str) -> str | None:
    c=cmd.strip()
    patterns=[r'(?is)^cd\s+.+?(?:&&|;)', r'(?is)^set-location\s+.+?(?:;|&&)', r'(?is)^pushd\s+.+?(?:&&|;)']
    if any(re.match(p,c) for p in patterns):
        return 'targeted validation runs in the target worktree automatically; remove embedded cd from the command'
    return None

def _validation_platform_is_windows() -> bool:
    return os.name=='nt'

def _platform_command_error(cmd: str) -> tuple[str,str] | None:
    if not _validation_platform_is_windows():
        return None
    checks=[('head', r'(?i)(?:\|\s*head\b|^\s*head\b)'),('tail', r'(?i)(?:\|\s*tail\b|^\s*tail\b)'),('grep', r'(?i)(?:^|[|&;]\s*)grep\b'),('sed', r'(?i)(?:^|[|&;]\s*)sed\b'),('awk', r'(?i)(?:^|[|&;]\s*)awk\b'),('cat', r'(?i)(?:^|[|&;]\s*)cat\b'),('rm -rf', r'(?i)\brm\s+-rf\b'),('export', r'(?i)^\s*export\s+\w+=')]
    for name,pat in checks:
        if re.search(pat, cmd):
            return name, f"validation command uses Unix-only utility '{name}' on Windows; use a Python one-liner or pytest flags instead"
    return None

def _attach_validation(state, target_obj, target, result):
    status=result.get('status') or ('passed' if result.get('passed') else 'failed')
    if target=='candidate' and target_obj is not None:
        target_obj.validation=result; target_obj.validation_status=status; target_obj.validation_results=list(getattr(target_obj,'validation_results',[]) or [])+[result]
        _set_acceptance_from_gate(state,target_obj)
    elif target=='integration' and isinstance(target_obj,dict):
        target_obj['validation']=result; target_obj['validation_status']=status; target_obj['validation_results']=(target_obj.get('validation_results') or [])+[result]
        _set_acceptance_from_gate(state,target_obj)
    elif target=='repo':
        vals=list(getattr(state,'repo_validation_results',[]) or []); vals.append(result); state.repo_validation_results=vals

def h_validation(state, inp, ctx):
    target_obj, base_cwd, _st = _resolve_validation_target(state, inp)
    results=[]; all_pass=True; overall_status='passed'; first_cwd=None
    outdir=Path(state.run_dir)/'validation'; outdir.mkdir(exist_ok=True)
    label=_target_label(inp.target, inp.target_id)
    for i,c in enumerate(inp.commands,1):
        try:
            cmd_cwd=_resolve_command_cwd(c.cwd, base_cwd, target=inp.target, allow_escape=inp.allow_cwd_escape)
            first_cwd=first_cwd or str(cmd_cwd)
            if inp.target in {'candidate','integration'}:
                msg=_embedded_cd_error(c.cmd)
                if msg: raise ValueError(msg)
            perr=_platform_command_error(c.cmd)
            if perr:
                util,msg=perr; raise RuntimeError(msg)
        except Exception as e:
            reason='platform_unsupported_command' if isinstance(e,RuntimeError) else 'targeted_cwd_rejected'
            item={'cmd':c.cmd,'passed':False,'status':'command_rejected','reason':reason,'error':str(e),'cwd':str(base_cwd.resolve())}
            results.append(item); all_pass=False; overall_status='command_rejected'
            ctx.recorder.record('validation_command_rejected', payload={'target':inp.target,'target_id':inp.target_id,'cmd':c.cmd,'reason':reason,'message':str(e)})
            continue
        ctx.recorder.record('validation_started', payload={'target':inp.target,'target_id':inp.target_id,'target_label':label,'cmd':c.cmd,'cwd':str(cmd_cwd),'worktree_path':str(base_cwd) if inp.target in {'candidate','integration'} else None})
        so=outdir/f'{inp.target}_{inp.target_id or "repo"}_{i}.stdout.log'; se=outdir/f'{inp.target}_{inp.target_id or "repo"}_{i}.stderr.log'
        try:
            p=subprocess.run(c.cmd,shell=True,cwd=cmd_cwd,text=True,capture_output=True,timeout=c.timeout_seconds or 300)
            write_text_utf8(so, p.stdout or ''); write_text_utf8(se, p.stderr or ''); passed=p.returncode==0; status='passed' if passed else 'failed'
            if not passed: overall_status='failed'
        except subprocess.TimeoutExpired as e:
            write_text_utf8(so, e.stdout or ''); write_text_utf8(se, (e.stderr or '')+'\ntimeout'); passed=False; status='timed_out'; overall_status='timed_out'
        except Exception as e:
            write_text_utf8(so, ''); write_text_utf8(se, f'{type(e).__name__}: {e}\n'); passed=False; status='infrastructure_error'; overall_status='error'
        all_pass=all_pass and passed
        item={'cmd':c.cmd,'passed':passed,'status':status,'cwd':str(cmd_cwd),'stdout_path':str(so),'stderr_path':str(se)}; results.append(item)
        ctx.recorder.record('validation_completed' if passed else 'validation_failed', payload={'target':inp.target,'target_id':inp.target_id,'passed':passed,'command_count':len(inp.commands),'cwd':str(cmd_cwd),'artifact_paths':{'stdout':str(so),'stderr':str(se)},'validation_result':item})
    res={'passed':all_pass,'status':overall_status if not all_pass else 'passed','commands':results,'target':inp.target,'target_id':inp.target_id,'cwd':first_cwd or str(base_cwd.resolve())}
    _attach_validation(state,target_obj,inp.target,res)
    ctx.recorder.record('validation_attached', payload={'target':inp.target,'target_id':inp.target_id,'passed':all_pass,'status':res['status'],'command_count':len(inp.commands),'cwd':res['cwd'],'artifact_paths':[p for r in results for p in [r.get('stdout_path'),r.get('stderr_path')] if p]})
    return res

def h_select_winner(state, inp, ctx):
    if inp.decision=='reject_all':
        if not inp.reasons: raise ValueError('reject_all requires reasons')
        state.selection=inp.model_dump(); state.phase='finalizing'; return state.selection
    if not inp.selected_attempt_id: raise ValueError('selected_attempt_id is required')
    a, st=_find_attempt(state, inp.selected_attempt_id)
    if not a: raise ValueError(f'selected attempt {inp.selected_attempt_id} does not exist')
    if state.execution_path=='decomposed_subtasks' and state.fallback_execution_path!='parallel_candidates_after_decomposition_deadlock':
        if st is not None: raise ValueError('cannot select raw subtask attempt as final winner')
        if not isinstance(a,dict) or inp.selected_attempt_id!='integration_001': raise ValueError('decomposed final selection requires integration result')
    if (state.execution_path=='parallel_candidates' or state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock') and (isinstance(a,dict) or getattr(a,'scope',None)!='candidate'):
        raise ValueError('candidate path selection requires candidate attempt')
    status=a.get('status') if isinstance(a,dict) else a.status
    if status=='running': raise ValueError('cannot select running attempt')
    eligible, blockers=is_attempt_acceptance_eligible(a, state=state)
    stored=a.get('acceptance_eligible') if isinstance(a,dict) else a.acceptance_eligible
    if isinstance(a,dict):
        a['acceptance_eligible']=eligible; a['acceptance_blockers']=blockers
    else:
        a.acceptance_eligible=eligible; a.acceptance_blockers=blockers
    if not eligible:
        state.blockers=sorted(set(state.blockers+blockers))
        ctx.recorder.record('selection_rejected', payload={'selected_attempt_id':inp.selected_attempt_id,'stored_acceptance_eligible':stored,'recomputed_acceptance_eligible':eligible,'acceptance_blockers':blockers})
        raise ValueError('selected attempt is not acceptance eligible: '+', '.join(blockers))
    state.selection={**inp.model_dump(),'selection_evidence':{'stored_acceptance_eligible':stored,'recomputed_acceptance_eligible':eligible,'acceptance_blockers':blockers}}
    state.phase='finalizing'; ctx.recorder.record('winner_selected', payload=state.selection); return state.selection

def h_finalize(state, inp, ctx):
    final_payload=inp.model_dump()
    if inp.decision!='accepted' and inp.selected_attempt_id:
        a0, st0=_find_attempt(state, inp.selected_attempt_id)
        if st0 is not None:
            final_payload['best_partial_attempt_id']=inp.selected_attempt_id; final_payload['selected_attempt_id']=None
    if state.partial_progress: final_payload['partial_progress']=state.partial_progress
    if inp.decision!='accepted' and state.decomposed_execution_status in {'blocked','failed'}:
        fs=', '.join(state.decomposed_execution_failed_subtasks); bs=', '.join(state.decomposed_execution_blocked_subtasks)
        extra='Decomposed execution deadlocked because required subtasks failed: ' + (fs or 'unknown') + '.' + ((' Blocked dependent subtasks: ' + bs + '.') if bs else '')
        if state.fallback_used: extra += ' Villani Ops fell back to full-task candidates.'
        final_payload['summary']=extra if final_payload.get('summary') in {'','x','failed'} else final_payload.get('summary') + ' ' + extra
        final_payload['blockers']=sorted(set((final_payload.get('blockers') or []) + state.decomposed_execution_blockers + ['decomposition_deadlocked']))
    if inp.decision!='accepted' and state.execution_path=='decomposed_subtasks' and state.integration and state.integration.get('status')=='failed':
        final_payload['summary']='Subtasks were individually accepted by scoped review, but Villani Ops did not produce an integrated patch. Final validation failed, so no accepted solution was produced.'
        final_payload['blockers']=sorted(set((final_payload.get('blockers') or []) + ['integration_failed']))
    if inp.decision=='accepted':
        sel=state.selection or {}; aid=inp.selected_attempt_id or sel.get('selected_attempt_id')
        if sel.get('decision')!='select': raise ValueError('accepted finalization requires select decision')
        if not aid or (inp.selected_attempt_id and sel.get('selected_attempt_id') and inp.selected_attempt_id!=sel.get('selected_attempt_id')):
            raise ValueError('final selected attempt does not match selection')
        a, st=_find_attempt(state, aid)
        if not a: raise ValueError('selected attempt does not exist')
        if st is not None: raise ValueError('raw subtask attempt cannot be finalized as accepted')
        if state.execution_path=='decomposed_subtasks':
            if not isinstance(a,dict) or aid!='integration_001': raise ValueError('accepted finalization in decomposed mode requires integration result')
            if a.get('status')!='completed': raise ValueError('accepted finalization in decomposed mode requires completed eligible integration')
        if any(c.status=='running' for c in state.candidates) or any(a2.status=='running' for s in state.subtasks for a2 in s.attempts):
            raise ValueError('accepted finalization requires no running work')
        eligible, blockers=is_attempt_acceptance_eligible(a, state=state)
        if isinstance(a,dict):
            a['acceptance_eligible']=eligible; a['acceptance_blockers']=blockers
        else:
            a.acceptance_eligible=eligible; a.acceptance_blockers=blockers
        if not eligible:
            state.blockers=sorted(set(state.blockers+blockers))
            ctx.recorder.record('finalization_blocked', payload={'selected_attempt_id':aid,'acceptance_blockers':blockers})
            raise ValueError('selected attempt is not acceptance eligible: '+', '.join(blockers))
        if inp.selected_patch_path and (a.get('patch_path') if isinstance(a,dict) else a.patch_path) != inp.selected_patch_path:
            raise ValueError('final selected patch does not match selected attempt')
    state.final_decision=final_payload; state.status='completed' if inp.decision=='accepted' else 'failed'; state.phase='completed' if state.status=='completed' else 'failed'; ctx.recorder.record('run_finalized', payload=state.final_decision); return state.final_decision
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
'ops_start_candidate_fallback':ToolSpec('ops_start_candidate_fallback','Start full-task candidate fallback after decomposition deadlock',OpsStartCandidateFallbackInput,h_start_candidate_fallback),
'ops_launch_subtasks':ToolSpec('ops_launch_subtasks','Launch subtasks',OpsLaunchSubtasksInput,h_launch_subtasks),
'ops_review_attempt':ToolSpec('ops_review_attempt','Review attempt',OpsReviewAttemptInput,h_review_attempt),
'ops_integrate_subtasks':ToolSpec('ops_integrate_subtasks','Integrate subtasks',OpsIntegrateSubtasksInput,h_integrate),
'ops_run_validation':ToolSpec('ops_run_validation','Run validation commands in the selected target workspace automatically. For candidate/integration targets, provide target_id and commands without cd/pushd/Set-Location; cwd defaults to the target worktree and relative cwd is resolved inside it. Keep commands cross-platform; do not use Unix-only utilities like head, tail, grep, sed, awk, cat, rm -rf, or export. Prefer python -m pytest --tb=short -v or Python one-liners.',OpsRunValidationInput,h_validation),
'ops_select_winner':ToolSpec('ops_select_winner','Select winner',OpsSelectWinnerInput,h_select_winner),
'ops_finalize_run':ToolSpec('ops_finalize_run','Finalize run',OpsFinalizeRunInput,h_finalize),
}
def openai_tool_specs():
    return [{'type':'function','function':{'name':n,'description':s.description,'parameters':s.input_model.model_json_schema(),'strict':True}} for n,s in OPS_TOOLS.items()]
