from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
from .state import CandidateAttemptState, SubtaskState, AttemptObservation, detect_decomposition_deadlock, CandidateSummary, CandidateEvidencePacket, CandidateImplementationSignature, CommandEvidence, ChangedFileEvidence, CandidateRiskReview, PairwiseComparisonDraft, PairwiseCandidateComparison, RankedCandidate, TournamentRanking, CandidateAgreementSummary, normalize_score
from .git_artifacts import capture_git_patch, ensure_git_baseline, clean_runner_artifacts_from_worktree, DEFAULT_PATCH_EXCLUDES, is_git_compatible_patch, patch_contains_internal_artifacts, clean_untracked_scratch_artifacts, is_scratch_artifact_path
from villani_ops.core.acceptance import is_attempt_acceptance_eligible, attempt_requires_patch
import subprocess, json, time, shutil, os, re, hashlib
from datetime import datetime, timezone
from .artifacts import read_text_utf8, write_text_utf8, write_json_utf8
from concurrent.futures import ThreadPoolExecutor, as_completed
from villani_ops.telemetry.usage import usage_record_from_runner, usage_record_from_response


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
    current_validation=validation if validation.get('validation_source')!='villani_code_debug_trace' else {}
    debug_hist=[r for r in (data.get('validation_results') or []) if r.get('validation_source')=='villani_code_debug_trace']
    debug_cmds=[c for r in debug_hist for c in (r.get('commands') or [])]
    debug_summary={'label':'NON-BLOCKING RUNNER TRACE HISTORY','instruction':'These commands were executed during the runner repair process. They are diagnostic only and must not be used as the sole reason to reject the final patch.','source':'runner_trace','authority':'diagnostic_only','status':'historical','validation_like_command_count':len(debug_cmds)}
    if debug_cmds:
        first, final=debug_cmds[0], debug_cmds[-1]
        debug_summary.update({'first_relevant_validation':{'status':first.get('status'),'passed':first.get('passed'),'cmd':first.get('cmd')},'final_relevant_validation':{'status':final.get('status'),'passed':final.get('passed'),'cmd':final.get('cmd')},'final_command':final.get('cmd')})
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
        'validation':{**current_validation, 'commands':validation_tails, 'authoritative': bool(current_validation)},
        'validation_decision':(current_validation or {}).get('decision') or {},
        'current_validation':{**current_validation, 'source':'ops_run_validation'} if current_validation else {},
        'debug_validation_history':debug_summary,
        'non_blocking_runner_trace_history': debug_summary if debug_cmds else {},
        'imported_debug_validation': debug_hist[:1],
        'scope_assessment': data.get('scope_assessment'),
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
        dec=(current_validation or {}).get('decision') or {}
        nonblocking=(dec.get('supporting_failures') or []) + (dec.get('diagnostic_failures') or [])
        if nonblocking:
            payload['non_blocking_diagnostic_supporting_failures']={'label':'NON-BLOCKING DIAGNOSTIC/SUPPORTING FAILURES','instruction':'These must not be used as the sole reason to reject this subtask.','failures':nonblocking}
        payload['subtask_review_criteria']=[
            'Prioritize ValidationDecision.status and failed/passed authoritative validations over raw command failure counts.',
            'Judge only whether this patch satisfies the specific subtask contract.',
            'Do not reject solely because unrelated sibling subtasks or the global suite still fail.',
            'Validation commands have authority levels; only acceptance-blocking validation should block acceptance.',
            'Diagnostic and exploratory failures are evidence, not blockers.',
            'Global validation is reserved for integration/final acceptance.',
            'Require focused subtask validation to pass when available.',
            'Check scope compliance, merge safety, no unrelated test/source edits, and no broad rewrites.',
        ]
    return payload

class ScopeAssessment(BaseModel):
    compliant: bool
    extra_files: list[str]=Field(default_factory=list)
    allowed_files: list[str]=Field(default_factory=list)
    scope_exception_used: bool=False
    scope_exception_adequate: bool=False
    blockers: list[str]=Field(default_factory=list)
    warnings: list[str]=Field(default_factory=list)

def _validation_like_command(cmd:str)->bool:
    return bool(re.search(r'(?i)(\bpytest\b|python\s+-m\s+pytest|\bnpm\s+test\b|\bpnpm\s+test\b|\byarn\s+test\b|\bgo\s+test\b|\bcargo\s+test\b|\bmvn\s+test\b|\bgradle\s+test\b)', cmd or ''))

def assess_scope_compliance(*, scope:Literal['candidate','subtask','integration'], changed_files:list[str], allowed_files:list[str], scope_exception_text:str|None, subtask:SubtaskState|None)->ScopeAssessment:
    changed=[str(f).replace('\\','/') for f in (changed_files or [])]
    allowed=[str(f).replace('\\','/') for f in (allowed_files or [])]
    blockers=[]; warnings=[]
    internal=[f for f in changed if f.startswith(('.villani/','.villani_code/')) or f in {'.villani','.villani_code'}]
    if internal: blockers.append('internal_artifacts_modified')
    if scope=='candidate':
        return ScopeAssessment(compliant=not blockers, allowed_files=allowed, blockers=blockers, warnings=warnings)
    extra=[f for f in changed if allowed and f not in allowed]
    used=bool(scope_exception_text and 'SCOPE_EXCEPTION' in scope_exception_text)
    adequate=bool(used and re.search(r'(?is)Extra files modified:.*Why each extra file was necessary:.*Why the change is minimal:', scope_exception_text or ''))
    if scope=='subtask':
        if not allowed: warnings.append('allowed_files_unknown')
        if extra and not adequate: blockers.append('subtask_scope_overreach')
        elif extra and adequate: warnings.append('scope_exception_used')
    return ScopeAssessment(compliant=not blockers, extra_files=extra, allowed_files=allowed, scope_exception_used=used, scope_exception_adequate=adequate, blockers=blockers, warnings=warnings)

def _scope_exception_text(attempt)->str|None:
    parts=[]
    for path in [getattr(attempt,'stdout_path',None), getattr(attempt,'stderr_path',None), getattr(attempt,'transcript_path',None)]:
        t=_read_text_tail(path, max_chars=20000)
        if isinstance(t,str) and 'SCOPE_EXCEPTION' in t: parts.append(t[t.find('SCOPE_EXCEPTION'):])
    return '\n'.join(parts) or None

def _validation_plan_commands(state, subtask=None):
    cmds=[]
    if state.investigation and (state.investigation.get('validation_plan') or {}).get('commands'):
        cmds=[c.get('cmd') for c in state.investigation['validation_plan']['commands'] if c.get('cmd')]
    return cmds or ['python -m pytest --tb=short -v']

def build_subtask_runner_prompt(*, parent_task:str, parent_success_criteria:str|None, subtask:SubtaskState, allowed_files:list[str], forbidden_files:list[str]|None, validation_commands:list[ValidationCommand]|list[str], dependency_context:str|None, merge_contract:str|None)->str:
    cmds=[c.cmd if hasattr(c,'cmd') else str(c) for c in (validation_commands or [])] or ['python -m pytest --tb=short -v']
    allowed='\n'.join(f'- {f}' for f in allowed_files) if allowed_files else 'Allowed files were not confidently identified. Make the smallest possible change and explain changed files.'
    forbidden='\n'.join(f'- {f}' for f in (forbidden_files or ['.villani','.villani_code']))
    return ("You are executing ONE Villani Ops subtask, not the whole parent task.\n\n"
        "Your job is to complete only the subtask below.\nDo not solve unrelated parts of the parent task.\nDo not broaden scope unless the subtask is impossible without a minimal cross-file change.\n\n"
        f"PARENT TASK CONTEXT\nThis is background only. Do not solve the whole parent task unless required by the subtask.\n{parent_task}\n\n"
        "Parent success criteria are provided only so you understand the larger system. Your acceptance is based on the subtask objective and subtask validation, not solving the entire parent task.\n"
        f"{parent_success_criteria or ''}\n\nSUBTASK OBJECTIVE\n{subtask.title}\n{subtask.objective}\n\nSUBTASK SUCCESS CRITERIA\n{subtask.success_criteria or subtask.objective}\n\n"
        f"ALLOWED FILES\n{allowed}\n\nFORBIDDEN FILES / ARTIFACTS\n{forbidden}\nDo not create helper scripts, scratch files, logs, checkpoints, or temporary fix files in the repo.\nDo not modify or create Villani internal directories such as .villani or .villani_code.\nOnly product code and necessary tests should change.\n\n"
        f"DEPENDENCY CONTEXT\n{dependency_context or 'No accepted dependency context was provided.'}\n\nMERGE CONTRACT\n{merge_contract or 'Keep changes minimal and merge-friendly.'}\n\n"
        "EXPECTED VALIDATION\nRun the narrowest relevant tests for this subtask first. Then, if cheap enough, run broader parent validation.\nValidation commands have authority levels: only acceptance-blocking validation should block acceptance; diagnostic and exploratory failures are evidence, not blockers. Component subtasks are accepted on subtask-scoped evidence. Global validation is reserved for integration/final acceptance.\nSuggested commands:\n" + '\n'.join(f'- {c}' for c in cmds) +
        "\n\nSCOPE RULES\nDo not modify files outside the allowed list unless absolutely required.\n\nSCOPE EXCEPTION\nIf the subtask cannot be completed without modifying a file outside the allowed list, you may make the smallest necessary cross-file change.\nIf you do this, your final response must include a section:\nSCOPE_EXCEPTION:\n- Extra files modified:\n- Why each extra file was necessary:\n- Why the change is minimal:\n- Why this does not solve unrelated subtasks:\n\nAt the end of your run, report:\nSUBTASK_RESULT:\n- Status: completed / blocked / impossible_in_isolation\n- Files changed:\n- Tests run:\n- Test results:\n- Scope exception used: yes/no\n- If blocked or impossible, explain why:\n")

def build_subtask_attempt_learning_brief(state, subtask:SubtaskState)->str:
    failed=[o for o in state.attempt_observations if o.scope=='subtask' and o.subtask_id==subtask.subtask_id and o.outcome!='accepted'][-3:]
    if not failed: return ''
    parts=['PREVIOUS SUBTASK ATTEMPT LEARNING', '', f'Subtask: {subtask.subtask_id}']
    for o in failed:
        parts += [f'Attempt {o.attempt_id} failed {o.outcome}.', '', 'What changed:']
        parts += [f'- Edited {", ".join(o.changed_files)}.' if o.changed_files else '- No product files were changed.']
        if o.validation_status and o.validation_status not in {'passed','not_run'}:
            parts += ['Focused validation:'] + [f'- {e}' for e in (o.evidence or [o.validation_status])[:4]]
        if o.blockers:
            parts += ['Review blocker:'] + [f'- {b}' for b in o.blockers[:5]]
        if o.next_attempt_directives:
            parts += ['Do differently:'] + [f'- {d}' for d in o.next_attempt_directives[:6]]
        parts.append('')
    return '\n'.join(parts).strip()

def import_villani_code_debug_evidence(attempt: CandidateAttemptState)->list[dict]:
    root=Path(attempt.artifacts_dir or '')
    if not root.exists(): return []
    files=[]
    for name in ['commands.jsonl','events.jsonl','trace.jsonl','tool_calls.jsonl','debug.jsonl','transcript.json']:
        files += list(root.rglob(name))
    out=[]
    def walk(x):
        if isinstance(x,dict):
            cmd=x.get('cmd') or x.get('command') or x.get('input')
            if isinstance(cmd,dict): cmd=cmd.get('cmd') or cmd.get('command')
            if isinstance(cmd,str) and _validation_like_command(cmd):
                code=x.get('exit_code', x.get('returncode', x.get('return_code')))
                status=str(x.get('status') or ('passed' if code==0 else 'failed' if code is not None else 'error')).lower()
                passed=(code==0) if code is not None else status in {'passed','success','completed'}
                out.append({'cmd':cmd,'cwd':x.get('cwd'),'exit_code':code,'passed':passed,'status':'passed' if passed else ('timed_out' if 'timeout' in status else 'failed'),'duration_seconds':x.get('duration_seconds') or x.get('duration'),'timestamp':x.get('timestamp'),'source':'villani_code_debug_trace','scope':'subtask' if attempt.scope=='subtask' else 'candidate','stdout_tail':str(x.get('stdout') or '')[-4000:],'stderr_tail':str(x.get('stderr') or '')[-4000:]})
            for v in x.values(): walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    for f in files:
        try:
            txt=read_text_utf8(f, default='')
            if f.suffix=='.jsonl':
                for line in txt.splitlines():
                    if line.strip(): walk(json.loads(line))
            else: walk(json.loads(txt))
        except Exception: continue
    return out

def _attach_imported_validation(state, attempt:CandidateAttemptState):
    ev=import_villani_code_debug_evidence(attempt)
    if not ev: return []
    commands=[]
    for item in ev:
        row=dict(item)
        row['source']='runner_trace'
        row['authority']='diagnostic_only'
        row.setdefault('scope', attempt.scope)
        commands.append(row)
    result={'passed':False,'status':'inconclusive','commands':commands,'target':attempt.scope,'target_id':attempt.attempt_id,'validation_source':'villani_code_debug_trace'}
    result['decision']=make_validation_decision(result); result['decision_status']=result['decision']['status']
    attempt.validation_results=list(attempt.validation_results or [])+[result]
    if not attempt.validation or attempt.validation_source!='ops_run_validation':
        attempt.validation=result; attempt.validation_status='inconclusive'; attempt.validation_source='villani_code_debug_trace'
    return ev

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
class ValidationCommand(StrictModel):
    cmd:str; cwd:str|None=None; purpose:str|None=None; timeout_seconds:int|None=None
    source:Literal['user_success_criteria','investigation_discovered','subtask_focused','runner_suggested','diagnostic','exploratory','integration','final']|None=None
    authority:Literal['acceptance_blocking','supporting_evidence','diagnostic_only']|None=None
    scope:Literal['subtask','integration','candidate','final','repo']|None=None
    subtask_id:str|None=None
    reason:str|None=None
class ValidationDecision(StrictModel):
    status:Literal['passed','failed','inconclusive']
    scope:Literal['subtask','integration','candidate','final','repo']
    subtask_id:str|None=None
    blocking_failures:list[dict]=Field(default_factory=list)
    supporting_failures:list[dict]=Field(default_factory=list)
    diagnostic_failures:list[dict]=Field(default_factory=list)
    passed_blocking_checks:list[dict]=Field(default_factory=list)
    passed_supporting_checks:list[dict]=Field(default_factory=list)
    rationale:str
class ValidationPlan(StrictModel):
    scope:Literal['subtask','integration','candidate','final','repo']='candidate'
    subtask_id:str|None=None
    authoritative_commands:list[ValidationCommand]=Field(default_factory=list)
    supporting_commands:list[ValidationCommand]=Field(default_factory=list)
    diagnostic_commands:list[ValidationCommand]=Field(default_factory=list)
    commands:list[ValidationCommand]=Field(default_factory=list)
    rationale:str='Explicit validation contract for this decision.'
    confidence:Literal['high','medium','low']='medium'
    notes:list[str]=Field(default_factory=list)

    def all_commands(self)->list[ValidationCommand]:
        return [*self.authoritative_commands,*self.supporting_commands,*self.diagnostic_commands,*self.commands]

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
class OpsSelectExecutionPathInput(StrictModel): path:Literal['single_task','parallel_candidates','decomposed_subtasks','candidate_tournament']; reason:str
class OpsLaunchCandidatesInput(StrictModel): attempts:int; backend_name:str|None=None; reason:str
class OpsLaunchTournamentCandidatesInput(StrictModel): attempts:int|None=None; backend_name:str|None=None; reason:str='Launch independent adaptive tournament candidates.'
class OpsEvaluateTournamentInput(StrictModel): reason:str='Evaluate completed tournament candidates.'
class OpsRunSingleTaskAttemptsInput(StrictModel): attempts:int; backend_name:str|None=None; reason:str
class OpsRunNextCandidateAttemptInput(StrictModel): backend_name:str|None=None; base_attempt_id:str|None=None; repair:bool=False; reason:str
class OpsRunNextFallbackCandidateAttemptInput(StrictModel): backend_name:str|None=None; base_attempt_id:str|None=None; repair:bool=False; reason:str
class OpsRunNextSubtaskAttemptInput(StrictModel): subtask_id:str|None=None; backend_name:str|None=None; base_attempt_id:str|None=None; repair:bool=False; reason:str
class OpsObserveCompletedAttemptInput(StrictModel): attempt_id:str; reason:str='Create missing adaptive observation before retry.'
class OpsRunNextIntegrationRepairAttemptInput(StrictModel): backend_name:str|None=None; base_attempt_id:str|None=None; repair:bool=True; reason:str
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
def _adaptive_warning(state, ctx, message, payload=None):
    state.warnings.append(message)
    if ctx is not None and getattr(ctx, 'recorder', None) is not None:
        ctx.recorder.record('adaptive_plan_constraint_warning', payload={'warning': message, **(payload or {})})

def _is_adaptive(state):
    return getattr(state, 'orchestrator', None) == 'adaptive'

def h_plan(state, inp, ctx):
    plan=inp.model_dump()
    if _is_adaptive(state):
        tournament=state.candidate_attempts>1
        invalid=bool(inp.should_decompose or (inp.strategy!='parallel_candidates' if tournament else inp.strategy!='single_task'))
        if invalid:
            _adaptive_warning(state, ctx, 'adaptive_orchestrator_forced_single_task_plan', {'original_plan': plan})
        plan.update({'strategy':('parallel_candidates' if tournament else 'single_task'),'should_decompose':False,'decomposition_reason':None,'candidate_attempts':state.candidate_attempts})
        plan['execution_path']='candidate_tournament' if tournament else 'single_task'
        plan['plan_kind']='candidate_tournament' if tournament else 'single_task'
        state.subtasks=[]; state.decomposition=None; state.decomposition_requested=False; state.decomposition_validated=False; state.decomposition_accepted=None; state.decomposition_executed=False
        state.plan=plan; state.phase='choosing_execution_path'; return state.plan
    state.plan=plan; state.decomposition_requested=inp.should_decompose; state.phase='decomposing' if inp.should_decompose else 'choosing_execution_path'; return state.plan
def h_decomposition(state, inp, ctx):
    if _is_adaptive(state):
        _adaptive_warning(state, ctx, 'adaptive_orchestrator_rejected_decomposition', {'requested_decomposition': inp.model_dump()})
        state.decomposition=None; state.subtasks=[]; state.decomposition_requested=False; state.decomposition_executed=False; state.phase='choosing_execution_path'
        raise ValueError('adaptive orchestrator cannot decompose; use execution_path=single_task')
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
    if _is_adaptive(state):
        desired='candidate_tournament' if state.candidate_attempts>1 else 'single_task'
        if inp.path!=desired:
            _adaptive_warning(state, ctx, 'adaptive_orchestrator_forced_tournament_execution_path' if desired=='candidate_tournament' else 'adaptive_orchestrator_forced_single_task_execution_path', {'requested_path': inp.path, 'reason': inp.reason})
        state.execution_path=desired; state.phase='running_candidates'; state.candidate_execution_mode='parallel' if desired=='candidate_tournament' else 'sequential'; state.decomposition_executed=False; state.subtasks=[]; state.decomposition=None
        return {'execution_path':state.execution_path,'reason':'adaptive selected '+desired,'decomposition_fallback_used':False,'warning':None if inp.path==desired else 'adaptive_orchestrator_forced_execution_path'}
    strategy=(state.plan or {}).get('strategy')
    if strategy=='single_task' and inp.path in {'parallel_candidates','candidate_tournament'}:
        raise ValueError('plan strategy is single_task; use execution_path=single_task for sequential attempts, not parallel_candidates')
    if strategy=='parallel_candidates' and inp.path=='single_task':
        raise ValueError('plan strategy is parallel_candidates; use execution_path=parallel_candidates')
    state.execution_path=inp.path
    state.phase='running_subtasks' if inp.path=='decomposed_subtasks' else 'running_candidates'
    state.candidate_execution_mode='sequential' if inp.path=='single_task' else ('parallel' if inp.path in {'parallel_candidates','candidate_tournament'} else state.candidate_execution_mode)
    state.decomposition_executed=inp.path=='decomposed_subtasks'
    if inp.path=='decomposed_subtasks':
        state.decomposed_execution_status='running'
        _ensure_decomposition_integration_worktree(state, ctx)
    if inp.path=='parallel_candidates' and state.decomposition is not None and state.decomposition_accepted is not True:
        state.decomposition_fallback_used=True; state.decomposition_fallback_reason=inp.reason; state.decomposition_executed=False
    return {'execution_path':state.execution_path,'reason':inp.reason,'decomposition_fallback_used':state.decomposition_fallback_used}
def _attempt(aid, scope, subtask_id=None, backend=None, artifacts=None):
    return CandidateAttemptState(attempt_id=aid,backend_name=backend,status='scheduled',scope=scope,subtask_id=subtask_id,artifacts_dir=str(artifacts) if artifacts else None,acceptance_eligible=False)
def _copy_worktree(src:Path, dst:Path):
    ignore=shutil.ignore_patterns('.git','.villani-ops','.v','__pycache__')
    shutil.copytree(src,dst,ignore=ignore,dirs_exist_ok=True)



def _ensure_decomposition_integration_worktree(state, ctx=None):
    if state.execution_path != 'decomposed_subtasks':
        return None
    if state.decomposition_integration_worktree and Path(state.decomposition_integration_worktree).exists():
        return Path(state.decomposition_integration_worktree)
    root=Path(state.run_dir)/'decomposition'/'rolling_integration_worktree'
    if root.exists():
        shutil.rmtree(root)
    _copy_worktree(Path(state.repo_path), root)
    ensure_git_baseline(root)
    rev=subprocess.run(['git','rev-parse','HEAD'],cwd=root,text=True,capture_output=True)
    state.decomposition_integration_worktree=str(root)
    state.integration_base_revision=(rev.stdout or '').strip() or None
    state.accepted_patch_application_status=state.accepted_patch_application_status or {}
    if ctx is not None:
        ctx.recorder.record('decomposition_integration_worktree_initialized', payload={'worktree_path':str(root),'base_revision':state.integration_base_revision})
    return root

def _attempt_base_worktree(state, scope):
    if state.execution_path=='decomposed_subtasks' and scope in {'subtask','integration'} and state.decomposition_integration_worktree:
        p=Path(state.decomposition_integration_worktree)
        if p.exists(): return p
    if state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock' and state.decomposition_integration_worktree:
        p=Path(state.decomposition_integration_worktree)
        if p.exists(): return p
    return Path(state.repo_path)

def _patch_hash(path):
    import hashlib
    try: return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception: return None

def _apply_accepted_patch_to_integration(state, st, attempt, ctx=None):
    wtree=_ensure_decomposition_integration_worktree(state, ctx)
    sid=st.subtask_id
    existing=(state.accepted_patch_application_status or {}).get(sid)
    if existing and existing.get('status')=='applied' and existing.get('attempt_id')==attempt.attempt_id:
        return existing
    idir=Path(state.run_dir)/'decomposition'/'patch_applications'; idir.mkdir(parents=True,exist_ok=True)
    row={'subtask_id':sid,'attempt_id':attempt.attempt_id,'patch_path':attempt.patch_path,'files_changed':attempt.changed_files,'patch_hash':_patch_hash(attempt.patch_path) if attempt.patch_path else None,'integration_worktree_path':str(wtree),'applied_at':None,'status':'pending'}
    if not attempt.patch_path or not Path(attempt.patch_path).exists():
        row.update({'status':'failed','patch_application_error':'accepted patch missing','recommended_next_action':'rerun or repair the accepted subtask before downstream subtasks'})
    elif patch_contains_internal_artifacts(attempt.patch_path) or not is_git_compatible_patch(attempt.patch_path):
        row.update({'status':'failed','patch_application_error':'accepted patch is not a clean git-compatible product patch','recommended_next_action':'repair patch hygiene before continuing'})
    else:
        check=subprocess.run(['git','apply','--check','--whitespace=nowarn',attempt.patch_path],cwd=wtree,text=True,capture_output=True)
        if check.returncode!=0:
            row.update({'status':'failed','exit_code':check.returncode,'patch_application_error':(check.stderr or check.stdout or '')[-4000:],'conflicting_files':attempt.changed_files,'recommended_next_action':'stop decomposed execution and run integration repair from rolling worktree'})
        else:
            proc=subprocess.run(['git','apply','--whitespace=nowarn',attempt.patch_path],cwd=wtree,text=True,capture_output=True)
            if proc.returncode==0:
                row.update({'status':'applied','applied_at':datetime.now(timezone.utc).isoformat(),'stdout':proc.stdout,'stderr':proc.stderr})
                ctx.recorder.record('accepted_subtask_patch_applied', payload=row) if ctx is not None else None
            else:
                row.update({'status':'failed','exit_code':proc.returncode,'patch_application_error':(proc.stderr or proc.stdout or '')[-4000:],'conflicting_files':attempt.changed_files,'recommended_next_action':'stop decomposed execution and run integration repair from rolling worktree'})
    state.accepted_patch_application_status[sid]=row
    write_json_utf8(idir/f'{sid}.json', row, atomic=True)
    if row.get('status')!='applied':
        state.decomposed_execution_status='blocked'; state.decomposed_execution_blockers=sorted(set(state.decomposed_execution_blockers+['accepted_patch_integration_failed', sid])); state.last_error=row.get('patch_application_error')
        if ctx is not None: ctx.recorder.record('accepted_subtask_patch_application_failed', payload=row)
    return row

def _is_fake_dependency(obj):
    name=(getattr(obj,'name',None) or getattr(obj,'__class__',type('',(),{})).__name__ or '').lower()
    return 'fake' in name or 'placeholder' in name or name.startswith('_test')

def _require_real_execution(ctx):
    if ctx.production and not ctx.allow_fake_dependencies:
        if ctx.runner_adapter is None: raise ValueError('agentic_runner_adapter_missing')
        if _is_fake_dependency(ctx.runner_adapter): raise ValueError('fake runner dependency forbidden in production agentic mode')
        if ctx.coding_backend is None and ctx.backend is None: raise ValueError('agentic_backend_role_unavailable: coding')
        if _is_fake_dependency(ctx.coding_backend or ctx.backend): raise ValueError('fake coding backend forbidden in production agentic mode')

def resolve_coding_backend(ctx, backend_name:str|None):
    if backend_name:
        registry=getattr(ctx,'backends',None) or {}
        current=ctx.coding_backend or ctx.backend
        current_name=ctx.coding_backend_name or ctx.backend_name or getattr(current,'name',None)
        if backend_name==current_name and current is not None:
            return current_name, current
        if backend_name not in registry:
            raise ValueError(f"unknown coding backend '{backend_name}'")
        backend=registry[backend_name]
        if not getattr(backend,'enabled',True):
            raise ValueError(f"coding backend '{backend_name}' is disabled")
        if 'coding' not in (getattr(backend,'roles',[]) or ['coding']):
            raise ValueError(f"backend '{backend_name}' is not usable for coding")
        return backend_name, backend
    backend=ctx.coding_backend or ctx.backend
    name=ctx.coding_backend_name or ctx.backend_name or getattr(backend,'name',None)
    if backend is None: raise ValueError('agentic_backend_role_unavailable: coding')
    return name, backend

def _capture_runner_telemetry(res):
    fields=['model_requests','model_failures','total_tool_calls','tool_calls_by_name','total_file_reads','total_file_writes','commands_executed','commands_failed','first_substantive_file_read_tool_index','first_substantive_file_read_seconds','first_file_mutation_tool_index','first_file_mutation_seconds','first_command_tool_index','first_command_seconds','token_accounting_status','token_accounting_warnings','telemetry','debug_artifact_dir','resolved_trace_dir','duration_ms','input_tokens','output_tokens','total_tokens','total_cost']
    return {f:getattr(res,f) for f in fields if hasattr(res,f)}

def _brief_validation(validation):
    lines=[]
    for c in (validation or {}).get('commands') or []:
        if not c.get('passed'):
            lines.append(f"{c.get('cmd')} -> {c.get('status')}")
            tail=_read_text_tail(c.get('stderr_path'), max_chars=600)
            if isinstance(tail,str) and tail.strip(): lines.append(tail.strip()[-600:])
            break
    return lines

def _attempt_observation_snapshots(attempt):
    return (f"{attempt.validation_status}:{len(attempt.validation_results or [])}", f"{attempt.review_status}:{attempt.review_retry_count}:{bool(attempt.review)}")

def _attempt_observed_stage(attempt):
    parts=['completed']
    if attempt.validation: parts.append('validated')
    if attempt.review: parts.append('reviewed')
    return '+'.join(parts)

def create_attempt_observation(state, attempt):
    eligible, blockers=is_attempt_acceptance_eligible(attempt,state=state)
    telemetry=attempt.runner_telemetry or {}
    val=attempt.validation or {}; review=attempt.review or {}; hygiene=attempt.patch_hygiene or {}; scope=attempt.scope_assessment or {}
    evidence=[]; directives=[]; outcome='unknown'; failure_class=None
    vdec=(val or {}).get('decision') or {}
    def _cmds(items):
        return [str((x or {}).get('cmd') or (x or {}).get('purpose') or '') for x in (items or []) if isinstance(x,dict)]
    if review:
        blockers=sorted(set(blockers + list(review.get('blockers') or []) + list(review.get('issues') or [])))
    if attempt.failure_reason or attempt.runner_status=='exception':
        outcome='runner_failed'; failure_class='runner'; evidence.append((attempt.failure_reason or 'runner failed')[:300]); directives.append('Inspect the repository and make a concrete product-code patch before finishing.')
    elif not attempt.patch_path or not attempt.changed_files:
        outcome='no_patch'; failure_class='no_progress'; directives.append('Do not finish without editing the relevant repository files.')
    elif hygiene.get('contains_internal_artifacts') or hygiene.get('scratch_artifacts_in_patch') or hygiene.get('apply_check_passed') is False:
        outcome='patch_failed'; failure_class='patch_hygiene'; directives.append('Do not create scratch files or internal artifacts; produce a clean git-applicable patch.')
    elif scope.get('blockers'):
        outcome='scope_failed'; failure_class='scope'; directives.append('Stay within the allowed scope and avoid unrelated files.')
    elif vdec.get('status')=='failed' or (val and val.get('passed') is False and not vdec):
        outcome='validation_failed'; failure_class='validation'; evidence += _brief_validation(val); directives.append('Focus on the failing validation command/test before changing unrelated code.')
    elif review and (review.get('decision')!='pass' or review.get('blockers')):
        outcome='review_failed'; failure_class='review'; evidence.append(str(review.get('summary') or 'review failed')[:400]); directives.append('Address the review blocker directly and avoid repeating the rejected strategy.')
        blockers=sorted(set(blockers + list(review.get('blockers') or []) + list(review.get('issues') or [])))
    elif eligible:
        outcome='accepted'; evidence.append('central acceptance gate passed')
    elif attempt.changed_files:
        outcome='partial_progress'; failure_class='partial'; directives.append('Build on useful changed files but target the remaining blockers narrowly.')
    reads=telemetry.get('total_file_reads') or 0; writes=telemetry.get('total_file_writes') or len(attempt.changed_files or [])
    if reads==0 and writes==0:
        directives.append('Previous attempt showed no substantive repo reads or writes; inspect source files first.')
    prior_other_observations=[o for o in state.attempt_observations if o.attempt_id!=attempt.attempt_id]
    prev_same=[o for o in prior_other_observations if o.backend_name==attempt.backend_name and o.outcome in {'no_patch','runner_failed'}]
    should_escalate=len(prev_same)>=1 and outcome in {'no_patch','runner_failed'}
    if should_escalate: directives.append('Consider a different coding backend because this backend shows repeated no-progress or runner failure.')
    val_snap, review_snap=_attempt_observation_snapshots(attempt)
    obs=AttemptObservation(attempt_id=attempt.attempt_id,scope=attempt.scope,subtask_id=attempt.subtask_id,backend_name=attempt.backend_name,model=attempt.model,outcome=outcome,progress_score=(1.0 if eligible else 0.4 if attempt.changed_files else 0.0),failure_class=failure_class,evidence=evidence[:8],blockers=sorted(set(blockers)),changed_files=attempt.changed_files,validation_status=attempt.validation_status,validation_decision_status=vdec.get('status'),validation_decision_rationale=vdec.get('rationale'),blocking_validation_failures=_cmds(vdec.get('blocking_failures')),diagnostic_validation_failures=_cmds(vdec.get('diagnostic_failures')),supporting_validation_failures=_cmds(vdec.get('supporting_failures')),passed_blocking_validations=_cmds(vdec.get('passed_blocking_checks')),review_status=attempt.review_status,runner_signals=telemetry,backend_signals={},next_attempt_directives=list(dict.fromkeys(directives))[:8],should_retry_same_plan=outcome in {'validation_failed','review_failed','partial_progress','no_patch'},should_repair=outcome in {'validation_failed','review_failed','patch_failed'},should_decompose=outcome=='partial_progress' and len(prior_other_observations)>=1,should_escalate_backend=should_escalate,observed_at_stage=_attempt_observed_stage(attempt),validation_snapshot_id=val_snap,review_snapshot_id=review_snap,updated_at=datetime.now(timezone.utc).isoformat())
    state.attempt_observations=[o for o in state.attempt_observations if o.attempt_id!=attempt.attempt_id]+[obs]
    return obs

def _adaptive_capability_signal(a):
    if not a.get('attempts'):
        return 'unknown'
    if a.get('accepted_candidates') or a.get('validation_passes',0) >= 2 or a.get('review_passes',0) >= 2:
        return 'strong'
    if a.get('validation_passes') or a.get('review_passes') or a.get('runner_successes') or a.get('progress_attempts'):
        return 'adequate'
    if a.get('no_progress_attempts',0) >= 2 or a.get('runner_failures',0) >= 2 or not a.get('progress_attempts'):
        return 'weak'
    return 'unknown'

def recompute_adaptive_assessments(state):
    attempts={}
    for c in getattr(state,'candidates',[]) or []:
        attempts[c.attempt_id]=c
    for st in getattr(state,'subtasks',[]) or []:
        for a in getattr(st,'attempts',[]) or []:
            attempts[a.attempt_id]=a
    observations={}
    for o in getattr(state,'attempt_observations',[]) or []:
        observations[o.attempt_id]=o
    if len(observations) != len(getattr(state,'attempt_observations',[]) or []):
        state.attempt_observations=list(observations.values())
    backend_assessments={}
    runner={'attempts':0,'runner_successes':0,'validation_passes':0,'review_passes':0,'accepted_candidates':0,'no_progress_attempts':0,'runner_failures':0,'progress_attempts':0,'total_cost':0.0,'total_tokens':0}
    for aid, obs in observations.items():
        attempt=attempts.get(aid)
        name=obs.backend_name or (getattr(attempt,'backend_name',None) if attempt is not None else None) or 'unknown'
        b=backend_assessments.setdefault(name, {'attempts':0,'runner_successes':0,'validation_passes':0,'review_passes':0,'accepted_candidates':0,'no_progress_attempts':0,'runner_failures':0,'progress_attempts':0,'total_cost':0.0,'total_tokens':0})
        runner_success=bool(attempt is not None and getattr(attempt,'status',None) in {'completed','reviewed','accepted'})
        runner_failure=obs.outcome in {'runner_failed','infra_failed'} or bool(attempt is not None and (getattr(attempt,'status',None)=='failed' or getattr(attempt,'runner_status',None)=='exception'))
        progress=bool(obs.changed_files or (obs.progress_score or 0) > 0 or obs.outcome in {'accepted','validation_failed','review_failed','partial_progress','scope_failed','patch_failed'})
        cost=float(getattr(attempt,'cost',None) or 0.0) if attempt is not None else 0.0
        tokens=int(((getattr(attempt,'token_usage',None) or {}).get('total_tokens')) or 0) if attempt is not None else 0
        for d in (b, runner):
            d['attempts']+=1
            d['runner_successes']+=int(runner_success)
            d['validation_passes']+=int(obs.validation_status=='passed')
            d['review_passes']+=int(obs.review_status=='passed')
            d['accepted_candidates']+=int(obs.outcome=='accepted')
            d['no_progress_attempts']+=int(obs.outcome in {'no_patch','runner_failed','infra_failed'})
            d['runner_failures']+=int(runner_failure)
            d['progress_attempts']+=int(progress)
            d['total_cost']+=cost
            d['total_tokens']+=tokens
    for d in list(backend_assessments.values())+[runner]:
        d['average_cost']=d['total_cost']/max(1,d['attempts'])
        d['average_tokens']=d['total_tokens']/max(1,d['attempts'])
        d['capability_signal']=_adaptive_capability_signal(d)
    state.backend_assessments=backend_assessments
    state.runner_assessment=runner if runner['attempts'] else {}

def update_backend_runner_assessments(state, obs=None, attempt=None):
    recompute_adaptive_assessments(state)



def _commit_subtask_acceptance(state, st, attempt, ctx=None, reason='accepted'):
    existing=(state.accepted_patch_application_status or {}).get(st.subtask_id)
    already=st.status=='accepted' and st.accepted_attempt_id==attempt.attempt_id and existing and existing.get('status')=='applied'
    if already:
        attempt.status='accepted'; attempt.acceptance_eligible=True; attempt.acceptance_blockers=[]
        return True, [], existing
    st.status='accepted'; st.accepted_attempt_id=attempt.attempt_id
    attempt.status='accepted'; attempt.acceptance_eligible=True; attempt.acceptance_blockers=[]
    app=_apply_accepted_patch_to_integration(state, st, attempt, ctx)
    if app.get('status')!='applied':
        blockers=sorted(set((attempt.acceptance_blockers or [])+['accepted_patch_integration_failed']))
        attempt.acceptance_eligible=False; attempt.acceptance_blockers=blockers
        return False, blockers, app
    if ctx is not None:
        ctx.recorder.record('subtask_accepted', payload={'subtask_id':st.subtask_id,'attempt_id':attempt.attempt_id,'reason':reason,'patch_application_status':app.get('status')})
    _update_decomposed_execution_state(state, ctx)
    return True, [], app

def _subtask_commit_ready(state, st):
    for a in reversed(st.attempts or []):
        vdec=((a.validation or {}).get('decision') or {})
        validation_ok=vdec.get('status')=='passed'
        if validation_ok and a.review_status=='passed' and not ((a.scope_assessment or {}).get('blockers')):
            return a
    return None

def select_next_subtask(state):
    by={s.subtask_id:s for s in state.subtasks}
    budget=max(1,int(state.candidate_attempts or 1))
    for st in state.subtasks:
        if st.status!='accepted' and _subtask_commit_ready(state, st) is not None and all(by[d].status=='accepted' for d in st.dependencies):
            return st, 'commit_ready'
    for st in state.subtasks:
        complete=[a for a in st.attempts if a.status in {'completed','failed','reviewed','rejected','accepted'}]
        last=next((o for o in reversed(state.attempt_observations) if o.scope=='subtask' and o.subtask_id==st.subtask_id), None)
        if st.status=='pending' and complete and last and last.outcome!='accepted' and len(complete)<budget and all(by[d].status=='accepted' for d in st.dependencies) and _subtask_commit_ready(state, st) is None:
            return st, last
    ready=[st for st in state.subtasks if st.status=='pending' and not st.attempts and all(by[d].status=='accepted' for d in st.dependencies)]
    if ready: return ready[0], None
    return None, None

def build_decomposition_progress_brief(state, current_subtask=None):
    accepted=[]; risky=[]; constraints=[]
    for st in state.subtasks:
        if st.status=='accepted':
            a=_accepted_subtask_attempt(state, st) if '_accepted_subtask_attempt' in globals() else None
            files=(a.changed_files if a else []) or []
            accepted.append(f'- {st.subtask_id}: accepted. Changed {", ".join(files) or "no files recorded"}.')
            constraints.append(f'- Preserve {st.subtask_id} accepted behavior' + ((f' in {", ".join(files)}') if files else '') + '.')
        obs=[o for o in state.attempt_observations if o.scope=='subtask' and o.subtask_id==st.subtask_id and o.outcome!='accepted']
        if obs:
            o=obs[-1]; risky.append(f'- {st.subtask_id}: previous attempt {o.attempt_id} ended {o.outcome}; blockers: {", ".join((o.blockers or o.evidence or [])[:4]) or "unknown"}.')
    if not accepted and not risky: return ''
    deps=[]
    if current_subtask:
        deps=[f'- Depends on {d}; keep its accepted contract intact.' for d in current_subtask.dependencies]
    parts=['DECOMPOSITION PROGRESS SO FAR']
    if accepted: parts += ['', 'Accepted subtasks:'] + accepted
    if risky: parts += ['', 'Failed or risky subtasks:'] + risky
    imps=(constraints+deps)[:8]
    if imps: parts += ['', 'Implications for this subtask:'] + imps
    return '\n'.join(parts)

def build_attempt_learning_brief(state):
    failed=[o for o in state.attempt_observations if o.scope=='candidate' and o.outcome!='accepted'][-2:]
    if not failed: return ''
    parts=['PREVIOUS ATTEMPT LEARNING']
    for o in failed:
        parts += [f'\nAttempt {o.attempt_id} failed.', 'What changed:']
        parts += [f'- Edited {", ".join(o.changed_files)}.' if o.changed_files else '- No product files were changed.']
        if o.evidence:
            parts += ['Still failing:'] + [f'- {e}' for e in o.evidence[:3]]
        if o.blockers:
            parts += ['Review/acceptance blockers:'] + [f'- {b}' for b in o.blockers[:4]]
        if o.next_attempt_directives:
            parts += ['Do differently:'] + [f'- {d}' for d in o.next_attempt_directives[:5]]
    return '\n'.join(parts)

def build_candidate_runner_prompt(state, *, reason, repair=False, base_attempt_id=None):
    cmds=_validation_plan_commands(state)
    changed=sorted({f for o in state.attempt_observations for f in o.changed_files})
    brief=build_attempt_learning_brief(state)
    sections=[f'TASK\n{state.task}', f'SUCCESS CRITERIA\n{state.success_criteria or "Complete the task with a minimal correct patch."}', f'CURRENT EXECUTION PATH\n{state.execution_path or "single_task"}. Run exactly this one adaptive candidate attempt.']
    if state.investigation: sections.append('INVESTIGATION SUMMARY\n'+str({k:state.investigation.get(k) for k in ['summary','suspected_root_cause','relevant_files','relevant_tests','implementation_plan'] if k in state.investigation}))
    if changed: sections.append('CHANGED FILES FROM PREVIOUS ATTEMPTS\n'+'\n'.join(f'- {f}' for f in changed))
    if brief: sections.append(brief)
    sections.append('NEXT ATTEMPT DIRECTIVES\n- Make a focused product-code change; do not create scratch/internal artifacts.\n- Do not repeat previously rejected broad rewrites.\n- Rerun relevant validation before finishing when possible.\n'+'\n'.join(f'- Rerun: {c}' for c in cmds))
    if repair and base_attempt_id: sections.append(f'REPAIR MODE\nRepair the previous attempt {base_attempt_id} by addressing the blockers above.')
    sections.append(f'REASON FOR THIS ATTEMPT\n{reason}')
    return '\n\n'.join(sections)

def _run_attempt(state, ctx, aid, scope, task, success, subtask_id=None, backend_name=None, record_events=True):
    _require_real_execution(ctx)
    backend_name_resolved, backend=resolve_coding_backend(ctx, backend_name)
    if ctx.runner_adapter is None: raise ValueError('agentic_runner_adapter_missing')
    adir=Path(state.run_dir)/'attempts'/aid; adir.mkdir(parents=True,exist_ok=True)
    write_text_utf8(adir/'attempt_prompt.txt', task or '')
    wtree=adir/'worktree'
    a=_attempt(aid,scope,subtask_id=subtask_id,backend=backend_name_resolved,artifacts=adir)
    a.status='running'; a.worktree_path=str(wtree); a.started_at=str(time.time())
    if record_events:
        ctx.recorder.record(f'{scope}_attempt_started', payload={'attempt_id':aid,'subtask_id':subtask_id,'status':'running','execution_path':state.execution_path,'artifact_paths':{'artifacts_dir':str(adir)}})
    try:
        _copy_worktree(_attempt_base_worktree(state, scope), wtree)
        ensure_git_baseline(wtree)
        res=ctx.runner_adapter.run_task(repo_path=wtree,task=task,success_criteria=success,backend_name=a.backend_name or '',backend_config=backend,timeout_seconds=ctx.timeout_seconds,context={'attempt_id':aid,'subtask_id':subtask_id,'parent_task':state.task},artifacts_dir=adir)
        usage_record=None
        if getattr(ctx, 'usage_recorder', None):
            usage_record=usage_record_from_runner(run_id=state.run_id,phase='candidate_attempt' if scope=='candidate' else 'subtask_attempt',role='coding',backend=backend,result=res,attempt_id=aid,subtask_id=subtask_id)
            ctx.usage_recorder.record(usage_record)
            summary=ctx.usage_recorder.summarize(); state.usage_summary=summary.model_dump(mode='json'); state.usage_records_count=summary.calls_count; state.total_input_tokens=summary.input_tokens; state.total_output_tokens=summary.output_tokens; state.total_tokens=summary.total_tokens; state.total_cost=summary.total_cost; state.usage_unavailable_count=summary.unavailable_calls_count; state.input_tokens=summary.input_tokens; state.output_tokens=summary.output_tokens; state.costs={'total':summary.total_cost,'input':summary.input_cost,'output':summary.output_cost}
        write_text_utf8(adir/'stdout.log', getattr(res,'stdout','') or ''); write_text_utf8(adir/'stderr.log', getattr(res,'stderr','') or '')
        removed_scratch=clean_untracked_scratch_artifacts(wtree)
        cap=capture_git_patch(wtree, adir/'diff.patch', exclude_patterns=DEFAULT_PATCH_EXCLUDES)
        a.stdout_path=str(adir/'stdout.log'); a.stderr_path=str(adir/'stderr.log'); a.patch_path=cap.patch_path; a.transcript_path=getattr(res,'telemetry_path',None)
        a.changed_files=cap.changed_files; a.added_files=cap.added_files; a.deleted_files=cap.deleted_files; a.modified_files=cap.modified_files; a.renamed_files=cap.renamed_files
        scratch_in_patch=[p for p in cap.changed_files if is_scratch_artifact_path(p)]
        a.patch_hygiene={'format_valid': bool(cap.patch_path and is_git_compatible_patch(cap.patch_path)), 'contains_internal_artifacts': bool(cap.patch_path and patch_contains_internal_artifacts(cap.patch_path)), 'apply_check_passed': None, 'capture_failure_reason': cap.failure_reason, 'changed_files_after_filtering': cap.changed_files, 'removed_scratch_artifacts':removed_scratch, 'scratch_artifacts_in_patch':scratch_in_patch, 'scratch_hygiene_passed':not scratch_in_patch}
        if scratch_in_patch:
            a.acceptance_blockers=sorted(set(a.acceptance_blockers+['scratch_artifact_in_patch','patch_hygiene_failed']))
        if removed_scratch and record_events:
            ctx.recorder.record('scratch_artifacts_removed', payload={'attempt_id':aid,'removed_scratch_artifacts':removed_scratch})
        if cap.patch_path:
            chk=subprocess.run(['git','apply','--check','--cached',cap.patch_path],cwd=wtree,text=True,capture_output=True)
            a.patch_hygiene['apply_check_passed']=chk.returncode==0
            if chk.returncode!=0:
                a.acceptance_blockers=sorted(set(a.acceptance_blockers+['patch_apply_check_failed']))
                write_text_utf8(adir/'patch_apply_check_stderr.log', chk.stderr or '')
        if scope=='subtask':
            st_obj=next((s for s in state.subtasks if s.subtask_id==subtask_id), None)
            a.scope_assessment=assess_scope_compliance(scope='subtask', changed_files=a.changed_files, allowed_files=(st_obj.relevant_files if st_obj else []), scope_exception_text=_scope_exception_text(a), subtask=st_obj).model_dump()
            if a.scope_assessment.get('blockers'):
                a.acceptance_blockers=sorted(set(a.acceptance_blockers+a.scope_assessment.get('blockers',[])))
        else:
            a.scope_assessment=assess_scope_compliance(scope='candidate', changed_files=a.changed_files, allowed_files=[], scope_exception_text=None, subtask=None).model_dump()
        imported=_attach_imported_validation(state,a)
        if imported and record_events:
            ctx.recorder.record('debug_validation_imported', payload={'attempt_id':aid,'imported_validation_count':len(imported),'validation_source':'villani_code_debug_trace'})
        a.model=getattr(backend,'model',None); a.runner_telemetry=_capture_runner_telemetry(res); a.completed_at=str(time.time())
        ok=getattr(res,'exit_code',1)==0
        a.exit_code=getattr(res,'exit_code',None); a.exit_reason=getattr(res,'exit_reason',None); a.runner_status=getattr(res,'status',None)
        if usage_record is not None:
            a.token_usage=usage_record.model_dump(mode='json'); a.cost=usage_record.total_cost
        a.status='completed' if ok else 'failed'
        if not ok: a.failure_reason=(getattr(res,'stderr','') or getattr(res,'status',None) or f'runner exit code is {a.exit_code}')[:1000]
        ev=f'{scope}_attempt_completed' if ok else f'{scope}_attempt_failed'
        if record_events:
            ctx.recorder.record(ev, payload={'attempt_id':aid,'subtask_id':subtask_id,'status':a.status,'exit_code':getattr(res,'exit_code',None),'failure_reason':getattr(res,'stderr','') if not ok else None,'execution_path':state.execution_path,'artifact_paths':{'stdout':a.stdout_path,'stderr':a.stderr_path,'patch':a.patch_path}, **(({k:getattr(usage_record,k) for k in ['input_tokens','output_tokens','total_tokens','total_cost','usage_source']} if 'usage_record' in locals() and usage_record is not None else {}))})
    except Exception as e:
        a.status='failed'; a.completed_at=str(time.time()); a.duration_seconds=float(a.completed_at)-float(a.started_at or a.completed_at); a.failure_reason=f'{type(e).__name__}: {e}'; a.runner_error_type=type(e).__name__; a.runner_status='exception'; a.acceptance_eligible=False; a.acceptance_blockers=['runner_exception', f'runner_exception: {type(e).__name__}: {e}']
        a.stdout_path=str(adir/'stdout.log'); a.stderr_path=str(adir/'stderr.log')
        if not Path(a.stdout_path).exists(): write_text_utf8(Path(a.stdout_path), '')
        write_text_utf8(Path(a.stderr_path), f'{type(e).__name__}: {e}\n')
        if record_events:
            ctx.recorder.record(f'{scope}_attempt_failed', payload={'attempt_id':aid,'subtask_id':subtask_id,'status':'failed','failure_reason':a.acceptance_blockers[0],'execution_path':state.execution_path,'artifact_paths':{'stdout':a.stdout_path,'stderr':a.stderr_path,'artifacts_dir':str(adir)}})
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
    return {'fallback_used':state.fallback_used,'fallback_execution_path':state.fallback_execution_path,'fallback_reason':state.fallback_reason,'next_allowed_actions':state.allowed_next_actions()}



def _compact_text(value, limit=1200):
    text=value if isinstance(value,str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text)<=limit else text[:limit]+chr(10)+'...[truncated]'

def _budget_prompt(sections, max_chars=20000):
    out=[]; used=0
    for sec in sections:
        if used+len(sec)+2 > max_chars:
            remain=max_chars-used-80
            if remain>200: out.append(sec[:remain]+chr(10)+'...[context budget reached]')
            break
        out.append(sec); used += len(sec)+2
    return (chr(10)*2).join(out)

def build_decomposition_fallback_prompt(state, *, reason:str|None=None, repair:bool=False, base_attempt_id:str|None=None)->str:
    sub_obs=[o.model_dump(mode='json') for o in state.attempt_observations if o.scope=='subtask'][-8:]
    fb_obs=[o for o in state.attempt_observations if o.scope=='candidate']
    accepted=[{'subtask_id':st.subtask_id,'accepted_attempt_id':st.accepted_attempt_id,'changed_files':(_accepted_subtask_attempt(state,st).changed_files if _accepted_subtask_attempt(state,st) else []),'summary':st.objective} for st in state.subtasks if st.status=='accepted']
    failed=[{'subtask_id':st.subtask_id,'status':st.status,'attempts':[a.attempt_id for a in st.attempts],'observations':[o for o in sub_obs if o.get('subtask_id')==st.subtask_id]} for st in state.subtasks if st.status in {'failed','skipped'}]
    dead=detect_decomposition_deadlock(state)
    cmds=_validation_plan_commands(state)
    sections=[
        f'TASK\n{state.task}',
        f'SUCCESS CRITERIA\n{state.success_criteria or "Complete the task with a minimal correct patch."}',
        'DECOMPOSITION FALLBACK CONTEXT\nThe decomposed path deadlocked. Run exactly one full-task adaptive fallback candidate. This is not a cold start.',
        'DECOMPOSITION SUMMARY\n'+_compact_text({'decomposition':state.decomposition,'deadlock':dead.model_dump() if dead else None,'partial_progress':state.partial_progress}, 2500),
        'ACCEPTED SUBTASK SUMMARIES AND CHANGED FILES\n'+_compact_text(accepted, 2500),
        'FAILED SUBTASK OBSERVATIONS AND BLOCKED DEPENDENTS\n'+_compact_text({'failed_or_blocked_subtasks':failed,'blocked_dependents':state.decomposed_execution_blocked_subtasks,'remaining_broken_areas':state.decomposed_execution_blockers}, 3500),
        'FOCUSED VALIDATION FAILURES AND REVIEW BLOCKERS\n'+_compact_text(sub_obs, 5000),
        'PRESERVE / DO NOT REGRESS\n- Preserve accepted subtask work and behavior where compatible.\n- Do not regress accepted changed files listed above.\n- Do not repeat failed subtask approaches or broad unrelated rewrites.',
    ]
    if fb_obs:
        sections.append('PREVIOUS FALLBACK ATTEMPT FEEDBACK\n'+build_attempt_learning_brief(state))
    sections.append('WHAT TO FOCUS ON NEXT\n- Produce one integrated product-code patch for the original task.\n- Fix remaining broken areas, validation failures, review blockers, patch/scope/hygiene blockers.\n- Rerun known relevant commands where possible.\n'+'\n'.join(f'- Rerun: {c}' for c in cmds))
    if repair and base_attempt_id: sections.append(f'REPAIR MODE\nRepair previous fallback attempt {base_attempt_id}; do not repeat its failed strategy.')
    if reason: sections.append(f'REASON FOR THIS FALLBACK ATTEMPT\n{reason}')
    max_chars=int((state.adaptive_context or {}).get('fallback_prompt_max_chars') or 20000)
    return _budget_prompt(sections, max_chars=max_chars)


def build_tournament_candidate_prompt(state, *, reason:str|None=None)->str:
    return '\n\n'.join([
        f'TASK\n{state.task}',
        f'SUCCESS CRITERIA\n{state.success_criteria or "Complete the task with a minimal correct patch."}',
        'RUNNER BOILERPLATE\nProduce one minimal, correct product patch for the task. Do not create Villani internal artifacts or scratch files in the repository. Run relevant validation when practical.',
    ])



def _path_list_existing(root:Path|None, names:tuple[str,...])->list[str]:
    if not root or not root.exists(): return []
    out=[]
    for name in names:
        out += [str(x) for x in root.rglob(name) if x.is_file()]
    return sorted(dict.fromkeys(out))[:50]

def _extract_command_evidence_from_artifacts(attempt:CandidateAttemptState, *, limit:int=20)->list[CommandEvidence]:
    rows=[]
    for ev in import_villani_code_debug_evidence(attempt):
        out='\n'.join(x for x in [ev.get('stdout_tail'), ev.get('stderr_tail')] if x)
        rows.append(CommandEvidence(command=str(ev.get('cmd') or ''), exit_code=ev.get('exit_code'), purpose='validation-like command observed in runner debug trace', output_excerpt=_compact_text(out, 1000) if out else None, artifact_path=ev.get('source')))
    for r in (attempt.validation_results or []):
        for c in (r.get('commands') or []):
            cmd=c.get('cmd') or c.get('command')
            if cmd:
                out='\n'.join(str(c.get(k) or '') for k in ('stdout','stderr','stdout_tail','stderr_tail') if c.get(k))
                rows.append(CommandEvidence(command=str(cmd), exit_code=c.get('exit_code') if c.get('exit_code') is not None else c.get('returncode'), purpose=c.get('purpose') or r.get('validation_source'), output_excerpt=_compact_text(out, 1000) if out else None, artifact_path=r.get('artifact_path')))
    roots=[Path(p) for p in [attempt.artifacts_dir, (attempt.runner_telemetry or {}).get('debug_artifact_dir'), (attempt.runner_telemetry or {}).get('resolved_trace_dir')] if p]
    files=[]
    for root in roots:
        if root.exists():
            for pat in ('*.jsonl','*.json','*.trace'):
                files += [x for x in root.rglob(pat) if x.is_file()]
    def dig(obj, path):
        if isinstance(obj, dict):
            cmd=obj.get('command') or obj.get('cmd')
            args=obj.get('arguments') or obj.get('input') or {}
            if not cmd and isinstance(args, dict): cmd=args.get('command') or args.get('cmd')
            tc=obj.get('tool_call') or {}
            if not cmd and isinstance(tc, dict):
                a=tc.get('arguments') or {}
                cmd=(a.get('command') or a.get('cmd')) if isinstance(a, dict) else None
            tool=str(obj.get('tool') or obj.get('name') or (tc.get('name') if isinstance(tc,dict) else '')).lower()
            if cmd:
                out='\n'.join(str(obj.get(k) or '') for k in ('stdout','stderr','output','stdout_tail','stderr_tail') if obj.get(k))
                rows.append(CommandEvidence(command=str(cmd), exit_code=obj.get('exit_code') if obj.get('exit_code') is not None else obj.get('returncode'), purpose=tool or None, output_excerpt=_compact_text(out, 1000) if out else None, artifact_path=str(path)))
            for v in obj.values(): dig(v, path)
        elif isinstance(obj, list):
            for v in obj: dig(v, path)
    for f in sorted(dict.fromkeys(files))[:100]:
        txt=read_text_utf8(f, default='')
        if not txt: continue
        try:
            if f.suffix=='.jsonl':
                for line in txt.splitlines()[:1000]:
                    if line.strip(): dig(json.loads(line), f)
            else: dig(json.loads(txt), f)
        except Exception:
            continue
    seen=set(); uniq=[]
    for row in rows:
        if not row.command: continue
        key=(row.command,row.exit_code,row.output_excerpt,row.artifact_path)
        if key not in seen:
            seen.add(key); uniq.append(row)
    return uniq[:limit]

def summarize_candidate_debug_artifacts(attempt:CandidateAttemptState, *, max_chars:int=4000)->dict:
    telemetry=attempt.runner_telemetry or {}
    roots=[Path(p) for p in [attempt.artifacts_dir, telemetry.get('debug_artifact_dir'), telemetry.get('resolved_trace_dir')] if p]
    roots=[r for r in roots if r.exists()]
    artifact_paths=[]; trace_paths=[]
    for r in roots:
        artifact_paths += _path_list_existing(r, ('commands.jsonl','events.jsonl','tool_calls.jsonl','debug.jsonl','transcript.json','final_summary.json','summary.json'))
        trace_paths += _path_list_existing(r, ('trace.jsonl','*.trace.jsonl'))
    cmds=_extract_command_evidence_from_artifacts(attempt)
    failed=[c for c in cmds if c.exit_code not in (None,0)]
    facts=[]
    if telemetry:
        facts.append(f"telemetry: reads={telemetry.get('total_file_reads')}, writes={telemetry.get('total_file_writes')}, tool_calls={telemetry.get('total_tool_calls')}, commands={telemetry.get('commands_executed')}, failed_commands={telemetry.get('commands_failed')}")
    if cmds:
        facts.append('commands observed: '+', '.join(f"{c.command} -> {c.exit_code}" for c in cmds[:6]))
    if failed: facts.append('failed commands observed: '+', '.join(c.command for c in failed[:6]))
    if attempt.changed_files: facts.append('edited files: '+', '.join(attempt.changed_files[:20]))
    if not roots: facts.append('debug artifacts missing or inaccessible')
    text=_compact_text('\n'.join(facts), max_chars)
    return {'summary':text,'debug_artifact_paths':sorted(dict.fromkeys(artifact_paths))[:50],'trace_artifact_paths':sorted(dict.fromkeys(trace_paths))[:50],'commands':cmds,'failed_commands':failed}


def _normalize_patch_for_signature(patch_diff:str)->str:
    lines=[]
    for line in (patch_diff or '').splitlines():
        if line.startswith(('index ','+++ ','--- ')) or re.match(r'@@ .* @@', line):
            continue
        if line.startswith(('diff --git','new file mode','deleted file mode','similarity index')):
            continue
        lines.append(line.rstrip())
    return '\n'.join(lines).strip()

def _diff_side_lines(patch_diff:str, prefix:str)->list[str]:
    return [l[1:] for l in (patch_diff or '').splitlines() if l.startswith(prefix) and not l.startswith(prefix*3)]

def build_candidate_implementation_signature(candidate_id:str, changed_files:list[str], patch_diff:str, final_file_summaries:list[ChangedFileEvidence]|None=None)->CandidateImplementationSignature:
    normalized=_normalize_patch_for_signature(patch_diff)
    h=hashlib.sha256(normalized.encode('utf-8')).hexdigest() if normalized else None
    added=_diff_side_lines(patch_diff,'+'); removed=_diff_side_lines(patch_diff,'-')
    import_re=re.compile(r'^\s*(import|from|include|use|require|using)\b', re.I)
    sym_re=re.compile(r'^\s*(def|class|function|func|fn|method|struct|interface|enum|type|const|let|var)\s+([A-Za-z_][\w:.$-]*)', re.I)
    control_terms=['if','else','for','while','switch','case','try','catch','finally','return','raise','throw','break','continue','await','async','lock','timeout','cancel','cleanup','close','dispose','retry','error','exception','concurrent','thread','process','resource','validate','fallback']
    def terms(lines):
        found=[]
        low='\n'.join(lines).lower()
        for t in control_terms:
            if re.search(r'\b'+re.escape(t)+r'\b', low): found.append(t)
        return found
    tokens=[]
    risk=[]
    text='\n'.join(added).lower()
    for t in ['resource','cancellation','cancel','timeout','error','exception','concurrency','thread','async','cleanup','fallback','validation','retry','cache','state','mutation','global','security']:
        if re.search(r'\b'+t+r'\b', text): tokens.append(t)
    for t in ['todo','fixme','hack','placeholder','broad','global','sleep','unsafe','ignore','swallow']:
        if t in text: risk.append(t)
    changed_symbols=[]
    for line in added+removed:
        m=sym_re.match(line)
        if m: changed_symbols.append(m.group(2))
    fp=f"files={len(changed_files)} added={len(added)} removed={len(removed)} hash={(h or '')[:12]}"
    summary=f"Patch changes {len(changed_files)} file(s), adds {len(added)} line(s), removes {len(removed)} line(s); normalized_patch_hash={(h or 'missing')[:16]}."
    return CandidateImplementationSignature(candidate_id=candidate_id, changed_files=list(changed_files or []), normalized_patch_hash=h, patch_fingerprint=fp, added_imports=sorted({l.strip()[:200] for l in added if import_re.match(l)})[:20], removed_imports=sorted({l.strip()[:200] for l in removed if import_re.match(l)})[:20], changed_symbols=sorted(dict.fromkeys(changed_symbols))[:40], added_control_flow_terms=terms(added), removed_control_flow_terms=terms(removed), strategy_summary=summary, strategy_tokens=sorted(dict.fromkeys(tokens))[:30], risk_markers=sorted(dict.fromkeys(risk))[:20])

def _summarize_changed_file(path:str, worktree:str|None)->ChangedFileEvidence:
    full=Path(worktree or '')/path
    text=read_text_utf8(full, default='') if full.exists() and full.is_file() else ''
    risky=[line.strip()[:160] for line in text.splitlines() if any(x in line.lower() for x in ('todo','fixme','placeholder','hack'))][:5]
    summary=(f"final file available, {len(text.splitlines())} lines" if text else 'final file content unavailable')
    return ChangedFileEvidence(path=path, summary=summary, key_symbols_or_functions=[], risky_sections=risky)

def build_candidate_evidence_packet(state, attempt:CandidateAttemptState)->CandidateEvidencePacket:
    diff=read_text_utf8(Path(attempt.patch_path), default='') if attempt.patch_path else ''
    dbg=summarize_candidate_debug_artifacts(attempt, max_chars=int((state.adaptive_context or {}).get('candidate_debug_summary_max_chars') or 4000))
    changed=attempt.changed_files or extract_changed_file_metadata(diff).get('changed', [])
    limitations=[]
    if not diff: limitations.append('patch diff missing or empty')
    if not dbg['debug_artifact_paths'] and not dbg['trace_artifact_paths']: limitations.append('debug artifacts missing')
    if not attempt.runner_telemetry: limitations.append('runner telemetry missing')
    q='high' if diff and attempt.runner_telemetry and (dbg['debug_artifact_paths'] or dbg['trace_artifact_paths']) else ('medium' if diff and (attempt.runner_telemetry or dbg['commands']) else ('low' if diff or attempt.runner_telemetry or dbg['commands'] else 'missing'))
    claims=[]
    if attempt.validation_status and attempt.validation_status!='not_run': claims.append(f'validation status: {attempt.validation_status}')
    if dbg['commands']: claims.append(f"observed {len(dbg['commands'])} command(s) in runner/debug artifacts")
    risks=list(attempt.acceptance_blockers or [])
    if dbg['failed_commands']: risks.append('runner/debug artifacts include failed commands')
    final_summaries=[_summarize_changed_file(f, attempt.worktree_path) for f in changed[:8]]
    sig=build_candidate_implementation_signature(attempt.attempt_id, changed, diff, final_summaries)
    packet=CandidateEvidencePacket(candidate_id=attempt.attempt_id, attempt_id=attempt.attempt_id, patch_summary=(f"Changed {len(changed)} file(s): "+(', '.join(changed[:12]) if changed else 'none')), changed_files=changed, patch_diff_excerpt=_compact_text(diff, 6000) if diff else None, full_patch_path=attempt.patch_path, implementation_signature=sig, runner_status=attempt.runner_status or attempt.status, exit_code=attempt.exit_code, runner_summary=dbg['summary'], telemetry_summary=attempt.runner_telemetry or {}, debug_artifact_paths=dbg['debug_artifact_paths'], trace_artifact_paths=dbg['trace_artifact_paths'], commands_executed=dbg['commands'], commands_failed=dbg['failed_commands'], final_changed_file_summaries=final_summaries, observed_behaviour_claims=claims, implementation_strategy=sig.strategy_summary, potential_risks=sorted(dict.fromkeys(risks+sig.risk_markers)), evidence_quality=q, evidence_limitations=limitations)
    d=Path(state.run_dir)/'candidates'/attempt.attempt_id; d.mkdir(parents=True, exist_ok=True); write_json_utf8(d/'evidence.json', packet.model_dump(mode='json'))
    return packet

def build_candidate_review_prompt(state, packet:CandidateEvidencePacket)->str:
    instructions = (
        'All numeric scores must be between 0.0 and 1.0. Do not use 0-10 scoring. '
        'Do not give a generic review. Use the candidate evidence packet. Cite specific evidence from patch, commands, telemetry, or debug artifacts. '
        'If evidence is missing, say what cannot be determined. Assume plausible code may still be wrong. Identify hidden-test risks and material behavioural risks. '
        'Do not reward a candidate merely because it contains words or structures that seem related to the task. Inspect whether the implementation actually guarantees the required behaviour. '
        'For error handling, cancellation, cleanup, concurrency, resource lifecycle, idempotency, ordering, retries, and rollback tasks, identify what must happen after interruption/failure and whether the patch actually ensures it. '
        'If the patch catches an exception but does not complete required cleanup/recovery, mark that as high risk. If the patch relies on default runtime behaviour without evidence, mark uncertainty. '
        'Runner-executed checks are evidence, but they may not cover hidden edge cases. Do not accept solely because a candidate ran its own check. Ask whether the check actually exercises the risky behaviour. '
        'When unproven critical required behaviour is identified, use hidden_test_risk_score >= 0.65, correctness_score <= 0.65, and recommendation uncertain or reject.'
    )
    return _budget_prompt(['TASK\n'+state.task, 'SUCCESS CRITERIA\n'+(state.success_criteria or 'Complete the task with a minimal correct patch.'), 'INSTRUCTIONS\n'+instructions, 'CANDIDATE EVIDENCE PACKET\n'+_compact_text(packet.model_dump(mode='json'), 14000)], max_chars=18000)


def _review_authoritatively_validated(review:CandidateRiskReview)->bool:
    fields=list(review.strengths or [])+list(review.evidence_used or [])+list(review.command_findings or [])+list(review.rationale and [review.rationale] or [])
    text=' '.join(str(x).lower() for x in fields)
    return 'authoritative validation passed' in text or 'validation status: passed' in text

def detect_critical_evidence_gaps(review:CandidateRiskReview)->list[str]:
    from .state import UNVERIFIED_EVIDENCE_TERMS, CRITICAL_BEHAVIOUR_TERMS
    if _review_authoritatively_validated(review):
        return []
    fields=[]
    for attr in ['evidence_gaps','risks','likely_hidden_failures','edge_cases_missed','debug_artifact_findings','command_findings','patch_findings']:
        fields.extend(str(x) for x in (getattr(review, attr, None) or []))
    fields.append(str(review.rationale or ''))
    gaps=[]
    for item in fields:
        t=item.lower()
        has_unverified=any(term in t for term in UNVERIFIED_EVIDENCE_TERMS)
        has_critical=any(term in t for term in CRITICAL_BEHAVIOUR_TERMS)
        explicit_priority=('critical' in t or 'high priority' in t or 'high-risk' in t) and (item in (review.edge_cases_missed or []) or item in (review.evidence_gaps or []))
        low_conf_hidden=bool(review.likely_hidden_failures) and normalize_score(review.confidence)<=0.65 and item in (review.likely_hidden_failures or [])
        if (has_unverified and has_critical) or explicit_priority or (low_conf_hidden and has_critical):
            gaps.append(item.strip())
    return sorted(dict.fromkeys(x for x in gaps if x))

def has_unresolved_critical_evidence_gap(review:CandidateRiskReview)->bool:
    return bool(detect_critical_evidence_gaps(review))

def apply_review_risk_penalties(review:CandidateRiskReview)->CandidateRiskReview:
    rv=CandidateRiskReview.model_validate(review.model_dump(mode='json'))
    rv.confidence=normalize_score(rv.confidence); rv.minimality_score=normalize_score(rv.minimality_score); rv.correctness_score=normalize_score(rv.correctness_score); rv.hidden_test_risk_score=normalize_score(rv.hidden_test_risk_score)
    gaps=detect_critical_evidence_gaps(rv)
    rv.critical_evidence_gaps=gaps
    if gaps:
        rv.original_scores=rv.original_scores or {'correctness_score':rv.correctness_score,'hidden_test_risk_score':rv.hidden_test_risk_score,'confidence':rv.confidence,'recommendation':rv.recommendation}
        rv.correctness_score=min(rv.correctness_score,0.65); rv.hidden_test_risk_score=max(rv.hidden_test_risk_score,0.65); rv.confidence=min(rv.confidence,0.65)
        rv.recommendation={'strong_accept':'uncertain','accept':'weak_accept','weak_accept':'uncertain','uncertain':'uncertain','reject':'reject'}.get(rv.recommendation,rv.recommendation)
        rv.risk_penalty_applied=True
    rv.confidence=normalize_score(rv.confidence); rv.minimality_score=normalize_score(rv.minimality_score); rv.correctness_score=normalize_score(rv.correctness_score); rv.hidden_test_risk_score=normalize_score(rv.hidden_test_risk_score)
    return rv

def _pairwise_from_draft(d:PairwiseComparisonDraft, quality:str, a_id:str, b_id:str, *, attempts=None, repaired=False)->PairwiseCandidateComparison:
    return PairwiseCandidateComparison(candidate_a=a_id,candidate_b=b_id,material_differences=d.material_differences,a_likely_failures=d.a_likely_failures,b_likely_failures=d.b_likely_failures,winner=d.winner,confidence=normalize_score(d.confidence),comparison_quality=quality,rationale=d.rationale,model_attempts=attempts or [],parse_repair_applied=repaired)

def _compact_pairwise_candidate(packet:CandidateEvidencePacket, review:CandidateRiskReview|None=None)->dict:
    return {
        'candidate_id': packet.candidate_id,
        'patch_summary': packet.patch_summary,
        'implementation_signature': packet.implementation_signature.model_dump(mode='json') if packet.implementation_signature else None,
        'key_diff_excerpt': _compact_text(packet.patch_diff_excerpt or '', 2500),
        'review_summary': review.summary if review else None,
        'review_quality': review.review_quality if review else None,
        'normalized_correctness_score': normalize_score(review.correctness_score) if review else None,
        'normalized_hidden_test_risk_score': normalize_score(review.hidden_test_risk_score) if review else None,
        'risks': (review.risks if review else packet.potential_risks)[:8],
        'evidence_gaps': (review.evidence_gaps if review else packet.evidence_limitations)[:8],
        'command_evidence': [c.model_dump(mode='json') for c in packet.commands_executed[:5]],
        'evidence_quality': packet.evidence_quality,
    }

def build_pairwise_comparison_prompt(state, a:CandidateEvidencePacket, b:CandidateEvidencePacket, ra:CandidateRiskReview|None=None, rb:CandidateRiskReview|None=None)->str:
    return _budget_prompt(['TASK SUMMARY\n'+state.task, 'SUCCESS CRITERIA\n'+(state.success_criteria or 'Complete the task with a minimal correct patch.'), 'COMPARE\nAll numeric scores must be between 0.0 and 1.0. Do not use 0-10 scoring. Compare actual implementation strategy and likely behaviour from compact evidence. Exclude full logs, telemetry dumps, full state JSON, unbounded output, and raw debug artifacts.', 'CANDIDATE A COMPACT EVIDENCE\n'+_compact_text(_compact_pairwise_candidate(a, ra), 6500), 'CANDIDATE B COMPACT EVIDENCE\n'+_compact_text(_compact_pairwise_candidate(b, rb), 6500)], max_chars=15000)


def _coerce_structured_payload(schema_model, data, *, quality=None, candidate_id=None, changed_files=None):
    if not isinstance(data, dict): raise ValueError('structured_payload_not_object')
    allowed=set(schema_model.model_fields); obj={k:v for k,v in data.items() if k in allowed}
    name=schema_model.__name__
    if name=='PairwiseComparisonDraft':
        obj.setdefault('candidate_a', candidate_id[0] if isinstance(candidate_id,tuple) else obj.get('candidate_a','candidate_a')); obj.setdefault('candidate_b', candidate_id[1] if isinstance(candidate_id,tuple) else obj.get('candidate_b','candidate_b'))
        for k in ['material_differences','a_likely_failures','b_likely_failures']: obj.setdefault(k, [])
        obj.setdefault('winner','tie'); obj['confidence']=min(normalize_score(obj.get('confidence',0.5), default=0.5),0.75); obj.setdefault('rationale','Tolerantly parsed compact model comparison draft.')
    elif name=='CandidateRiskReview':
        obj.setdefault('candidate_id', candidate_id or obj.get('candidate_id') or 'unknown'); obj.setdefault('summary','Repaired malformed model review.'); obj.setdefault('changed_files', changed_files or [])
        obj.setdefault('likely_correct', False); obj['confidence']=min(normalize_score(obj.get('confidence',0.55), default=0.55),0.55); obj.setdefault('implementation_strategy','unknown')
        for k in ['evidence_used','evidence_gaps','strengths','risks','likely_hidden_failures','edge_cases_considered','edge_cases_missed','debug_artifact_findings','command_findings','patch_findings']: obj.setdefault(k, [] if k!='evidence_gaps' else ['model output repaired or missing fields'])
        obj.setdefault('minimality_score',0.5); obj.setdefault('correctness_score',0.45); obj.setdefault('hidden_test_risk_score',0.55); obj.setdefault('review_quality', quality or 'model_minimal'); obj.setdefault('recommendation','uncertain'); obj.setdefault('rationale','Tolerantly parsed/repaired model output; confidence capped.')
    elif name=='PairwiseCandidateComparison':
        obj.setdefault('candidate_a', candidate_id[0] if isinstance(candidate_id,tuple) else obj.get('candidate_a','candidate_a')); obj.setdefault('candidate_b', candidate_id[1] if isinstance(candidate_id,tuple) else obj.get('candidate_b','candidate_b'))
        for k in ['material_differences','a_evidence_advantages','b_evidence_advantages','a_likely_failures','b_likely_failures']: obj.setdefault(k, [])
        obj.setdefault('winner','tie'); obj['confidence']=min(normalize_score(obj.get('confidence',0.5), default=0.5),0.55); obj.setdefault('comparison_quality', quality or 'model_minimal'); obj.setdefault('model_attempts', []); obj.setdefault('fallback_reason', None); obj.setdefault('parse_repair_applied', True); obj.setdefault('rationale','Tolerantly parsed/repaired model comparison; confidence capped.')
    return schema_model.model_validate(obj)

def _extract_json_object(text):
    if not text: return None
    if isinstance(text, list): text='\n'.join(str(x.get('text') or x.get('content') or x) for x in text)
    m=re.search(r'\{.*\}', str(text), re.S)
    if not m: return None
    return json.loads(m.group(0))

def _structured_tool_call(ctx, backend, schema_model, tool_name:str, prompt:str, system:str, *, quality=None, candidate_id=None, changed_files=None):
    from .client import ToolCallingLLMClient
    tool={'type':'function','function':{'name':tool_name,'description':'Return structured tournament evidence evaluation only.','parameters':schema_model.model_json_schema(),'strict':True}}
    resp=ToolCallingLLMClient().create_message(backend=backend,messages=[{'role':'user','content':prompt}],system=system,tools=[tool],tool_choice={'type':'function','function':{'name':tool_name}},strict=True)
    blocks=getattr(resp,'content',[]) or []
    calls=[b for b in blocks if isinstance(b,dict) and b.get('type')=='tool_use' and b.get('name')==tool_name]
    if calls: return _coerce_structured_payload(schema_model, calls[0].get('input') or {}, quality=quality, candidate_id=candidate_id, changed_files=changed_files)
    for b in blocks:
        try:
            obj=_extract_json_object(b.get('text') or b.get('content') if isinstance(b,dict) else b)
            if obj is not None: return _coerce_structured_payload(schema_model, obj, quality=quality, candidate_id=candidate_id, changed_files=changed_files)
        except Exception: pass
    raise ValueError(f'{tool_name}_missing_tool_call')

def _review_backend_from_ctx(ctx):
    return getattr(ctx,'review_backend',None) or getattr(getattr(ctx,'reviewer',None),'review_backend',None) or getattr(ctx,'backend',None)

def _model_candidate_risk_review(state, packet:CandidateEvidencePacket, ctx, *, allowed_qualities:tuple[str,...]|None=None)->CandidateRiskReview|None:
    backend=_review_backend_from_ctx(ctx)
    if backend is None: return None
    prompts=[('model_full', build_candidate_review_prompt(state, packet)), ('model_compact', _budget_prompt(['TASK\n'+state.task,'CANDIDATE EVIDENCE PACKET\n'+_compact_text(packet.model_dump(mode='json'),7000)],9000)), ('model_minimal', _budget_prompt(['TASK\n'+state.task,'MINIMAL EVIDENCE\n'+_compact_text({'candidate_id':packet.candidate_id,'summary':packet.patch_summary,'signature':packet.implementation_signature.model_dump(mode='json') if packet.implementation_signature else None,'commands':[c.model_dump(mode='json') for c in packet.commands_executed[:5]],'limitations':packet.evidence_limitations},4000)],5500))]
    allowed=set(allowed_qualities or [q for q,_ in prompts])
    for quality,prompt in prompts:
        if quality not in allowed: continue
        try:
            rv=_structured_tool_call(ctx, backend, CandidateRiskReview, 'candidate_risk_review', prompt, 'You are an evidence-grounded tournament reviewer. Use specific patch, command, telemetry, and debug evidence; identify hidden-test risks and missing evidence.', quality=quality, candidate_id=packet.candidate_id, changed_files=packet.changed_files)
            rv=rv.model_copy(update={'review_quality':quality,'candidate_id':packet.candidate_id,'changed_files':packet.changed_files})
            return apply_review_risk_penalties(CandidateRiskReview.model_validate(rv.model_dump(mode='json')))
        except Exception as e:
            if getattr(ctx,'recorder',None): ctx.recorder.record('tournament_review_retry_or_fallback', payload={'candidate_id':packet.candidate_id,'error':str(e)[:500],'quality':quality})
            continue
    return None

def _model_pairwise_comparison(state, a_packet:CandidateEvidencePacket, b_packet:CandidateEvidencePacket, a_review:CandidateRiskReview, b_review:CandidateRiskReview, ctx)->PairwiseCandidateComparison|None:
    backend=_review_backend_from_ctx(ctx)
    if backend is None: return None
    prompts=[('full',build_pairwise_comparison_prompt(state,a_packet,b_packet,a_review,b_review)),('compact',_budget_prompt(['TASK SUMMARY\n'+state.task,'COMPARE actual implementation signatures, patch summaries, top risks and evidence gaps only.','A\n'+_compact_text(_compact_pairwise_candidate(a_packet,a_review),4500),'B\n'+_compact_text(_compact_pairwise_candidate(b_packet,b_review),4500)],10000)),('minimal',_budget_prompt(['TASK\n'+state.task,'Return compact JSON/tool result. Compare signatures, top gaps, and likely failures.','A\n'+_compact_text({'signature':a_packet.implementation_signature,'risks':a_review.risks[:4],'gaps':a_review.evidence_gaps[:4]},2500),'B\n'+_compact_text({'signature':b_packet.implementation_signature,'risks':b_review.risks[:4],'gaps':b_review.evidence_gaps[:4]},2500)],6500))]
    attempts=[]
    for tier,prompt in prompts:
        event_payload={'candidate_a':a_packet.candidate_id,'candidate_b':b_packet.candidate_id,'attempt_tier':tier}
        start=time.monotonic()
        if getattr(ctx,'recorder',None): ctx.recorder.record('tournament_pairwise_model_attempt_started', payload=event_payload)
        try:
            draft=_structured_tool_call(ctx, backend, PairwiseComparisonDraft, 'pairwise_candidate_comparison', prompt, 'Compare two candidates using compact evidence. Return only the requested structured comparison draft.', quality=f'model_{tier if tier!="full" else "full"}', candidate_id=(a_packet.candidate_id,b_packet.candidate_id))
            elapsed=time.monotonic()-start
            attempt={**event_payload,'elapsed_seconds':elapsed,'status':'succeeded'}; attempts.append(attempt)
            if getattr(ctx,'recorder',None): ctx.recorder.record('tournament_pairwise_model_attempt_succeeded', payload=attempt)
            return _pairwise_from_draft(draft, f'model_{tier if tier!="full" else "full"}', a_packet.candidate_id, b_packet.candidate_id, attempts=attempts, repaired=False)
        except Exception as e:
            elapsed=time.monotonic()-start
            msg=str(e)[:300]; category='timeout' if 'timeout' in msg.lower() or 'timed out' in msg.lower() else ('malformed' if any(x in msg.lower() for x in ['validation','json','malformed','missing_tool_call','structured_payload']) else 'failed')
            attempt={**event_payload,'elapsed_seconds':elapsed,'status':category,'error_category':category,'error':msg}; attempts.append(attempt)
            if getattr(ctx,'recorder',None):
                ctx.recorder.record('tournament_pairwise_model_attempt_timeout' if category=='timeout' else ('tournament_pairwise_model_attempt_malformed' if category=='malformed' else 'tournament_pairwise_model_attempt_failed'), payload=attempt)
            continue
    if getattr(ctx,'recorder',None): ctx.recorder.record('tournament_pairwise_model_fallback_used', payload={'candidate_a':a_packet.candidate_id,'candidate_b':b_packet.candidate_id,'attempt_tier':'minimal','fallback_reason':attempts[-1].get('error_category') if attempts else 'no_backend','model_attempts':attempts})
    return None

def _candidate_summary_from_attempt(a):
    patch='No patch captured.' if not a.patch_path else 'Patch captured for files: '+(', '.join(a.changed_files or []) or 'none')
    return CandidateSummary(candidate_id=a.attempt_id, runner_status=a.runner_status or a.status, changed_files=a.changed_files or [], patch_summary=patch, validation_status=a.validation_status, telemetry_summary=a.runner_telemetry or {}, obvious_risks=list(a.acceptance_blockers or []))

def _risk_review_from_summary(summary:CandidateSummary, evidence:CandidateEvidencePacket|None=None)->CandidateRiskReview:
    validation_passed=summary.validation_status=='passed'
    has_patch=bool(summary.changed_files)
    q=evidence.evidence_quality if evidence else 'missing'
    risk=0.15 if validation_passed else (0.5 if has_patch else 0.95)
    if q in {'low','missing'}: risk=min(1.0, risk+0.15)
    correctness=0.85 if validation_passed else (0.5 if has_patch else 0.05)
    rec='accept' if validation_passed else ('uncertain' if has_patch else 'reject')
    gaps=list(evidence.evidence_limitations if evidence else ['candidate evidence packet unavailable'])
    patch_findings=[evidence.patch_summary] if evidence else [summary.patch_summary]
    command_findings=[] if not evidence else [f"{c.command} exited {c.exit_code}" for c in evidence.commands_executed[:8]]
    debug_findings=[] if not evidence else ([evidence.runner_summary] if evidence.runner_summary else [])
    conf=0.85 if validation_passed else (0.55 if q in {'high','medium'} and has_patch else 0.45)
    return apply_review_risk_penalties(CandidateRiskReview(candidate_id=summary.candidate_id, summary=summary.patch_summary, changed_files=summary.changed_files, likely_correct=validation_passed or (has_patch and q!='missing'), confidence=min(conf,0.55), implementation_strategy=(evidence.implementation_strategy if evidence and evidence.implementation_strategy else 'unknown'), evidence_used=patch_findings+command_findings[:3]+debug_findings[:2], evidence_gaps=gaps, strengths=(['authoritative validation passed'] if validation_passed else ['material patch produced'] if has_patch else []), risks=summary.obvious_risks or list(evidence.potential_risks if evidence else []) or ([] if validation_passed else ['no authoritative validation pass recorded']), likely_hidden_failures=([] if validation_passed else ['behaviour may be unproven by validation']), edge_cases_considered=[], edge_cases_missed=([] if validation_passed else ['unknown hidden edge cases']), debug_artifact_findings=debug_findings, command_findings=command_findings, patch_findings=patch_findings, minimality_score=max(0.0, 1.0-0.05*len(summary.changed_files)), correctness_score=correctness, hidden_test_risk_score=risk, review_quality='deterministic_fallback', recommendation=rec, rationale='Deterministic fallback review based on candidate evidence packet; model review unavailable, so confidence is capped at 0.55.'))

def _validation_rank(review:CandidateRiskReview, evidence:CandidateEvidencePacket|None)->int:
    # Authoritative validation is represented on candidate summaries in ranking; pairwise fallback only
    # gets packet/review evidence, so keep this neutral unless evidence explicitly records a pass/fail claim.
    claims=' '.join((evidence.observed_behaviour_claims if evidence else []) or []).lower()
    if 'validation status: passed' in claims: return 1
    if 'validation status: failed' in claims or 'validation status: error' in claims: return -1
    return 0

def _compare_pair(a:CandidateRiskReview,b:CandidateRiskReview, ae:CandidateEvidencePacket|None=None, be:CandidateEvidencePacket|None=None)->PairwiseCandidateComparison:
    qa=0 if not ae else {'high':3,'medium':2,'low':1,'missing':0}.get(ae.evidence_quality,0); qb=0 if not be else {'high':3,'medium':2,'low':1,'missing':0}.get(be.evidence_quality,0)
    sa=ae.implementation_signature if ae else None; sb=be.implementation_signature if be else None
    material=[]
    if sa and sb:
        if sa.normalized_patch_hash!=sb.normalized_patch_hash:
            material.append(f"same changed files but different normalized patch hash: {str(sa.normalized_patch_hash)[:12]} vs {str(sb.normalized_patch_hash)[:12]}" if set(sa.changed_files)==set(sb.changed_files) else f"different normalized patch hash: {str(sa.normalized_patch_hash)[:12]} vs {str(sb.normalized_patch_hash)[:12]}")
        else: material.append(f"same normalized patch hash: {str(sa.normalized_patch_hash)[:12]}")
        if set(sa.strategy_tokens)!=set(sb.strategy_tokens): material.append(f"different strategy tokens: A={sa.strategy_tokens}; B={sb.strategy_tokens}")
        if set(sa.added_control_flow_terms)!=set(sb.added_control_flow_terms): material.append(f"different control-flow/resource/error-handling terms: A={sa.added_control_flow_terms}; B={sb.added_control_flow_terms}")
        if sa.patch_fingerprint!=sb.patch_fingerprint: material.append(f"different patch size/scope: A={sa.patch_fingerprint}; B={sb.patch_fingerprint}")
        if set(sa.risk_markers)!=set(sb.risk_markers): material.append(f"risk markers differ: A={sa.risk_markers}; B={sb.risk_markers}")
    if ae and be and ([c.command for c in ae.commands_executed[:5]] != [c.command for c in be.commands_executed[:5]] or len(ae.commands_failed)!=len(be.commands_failed)):
        material.append('different command evidence')
    if not material:
        material=sorted(set(a.changed_files)^set(b.changed_files)) or ['no signature-level difference available; fallback evidence is non-discriminative']
    ga=has_unresolved_critical_evidence_gap(a); gb=has_unresolved_critical_evidence_gap(b)
    if ga!=gb: material.append('critical evidence gap differs between candidates')
    rq={'model_full':3,'model_compact':2,'model_minimal':1,'deterministic_fallback':0}
    ca=normalize_score(a.correctness_score); cb=normalize_score(b.correctness_score)
    ra=normalize_score(a.hidden_test_risk_score); rb=normalize_score(b.hidden_test_risk_score)
    ma=normalize_score(a.minimality_score); mb=normalize_score(b.minimality_score)
    # Ordered fallback signals: validation, signature/evidence, review quality, normalized risk/correctness/minimality.
    va=_validation_rank(a,ae); vb=_validation_rank(b,be)
    sig_a=1 if sa and sb and sa.normalized_patch_hash!=sb.normalized_patch_hash and (sa.risk_markers or sa.strategy_tokens) else 0
    sig_b=1 if sa and sb and sa.normalized_patch_hash!=sb.normalized_patch_hash and (sb.risk_markers or sb.strategy_tokens) else 0
    aggregate_a=(0.30*va)+(0.12*sig_a)+(0.14*(0 if ga else 1))+(0.12*(qa/3))+(0.08*(rq.get(a.review_quality,0)/3))+(0.10*(1-ra))+(0.09*ca)+(0.05*ma)
    aggregate_b=(0.30*vb)+(0.12*sig_b)+(0.14*(0 if gb else 1))+(0.12*(qb/3))+(0.08*(rq.get(b.review_quality,0)/3))+(0.10*(1-rb))+(0.09*cb)+(0.05*mb)
    diff=aggregate_a-aggregate_b
    if abs(diff) < 0.05:
        w='tie'; conf=0.45
    elif diff > 0:
        w='candidate_a'; conf=0.55
    else:
        w='candidate_b'; conf=0.55
    if ga and gb: conf=min(conf,0.45)
    if (w=='candidate_a' and ga and not gb) or (w=='candidate_b' and gb and not ga): conf=min(conf,0.55)
    if va==vb==0: conf=min(conf,0.60)
    if abs((ca-ra)-(cb-rb)) > 0.2 and va==vb and qa==qb and sig_a==sig_b and ga==gb: conf=min(conf,0.55)
    return PairwiseCandidateComparison(candidate_a=a.candidate_id,candidate_b=b.candidate_id,material_differences=material,a_evidence_advantages=([f'evidence quality {ae.evidence_quality}'] if ae and qa>qb else []),b_evidence_advantages=([f'evidence quality {be.evidence_quality}'] if be and qb>qa else []),a_likely_failures=a.likely_hidden_failures,b_likely_failures=b.likely_hidden_failures,winner=w,confidence=conf,comparison_quality='deterministic_fallback',fallback_reason='model_pairwise_unavailable',rationale='Deterministic fallback comparison prioritized validation claims, implementation signatures, evidence/commands, review quality, normalized risk/correctness/minimality, and used a 0.05 tie margin so normalized numeric scores cannot dominate.')


def build_candidate_agreement_summary(packets:dict[str,CandidateEvidencePacket])->CandidateAgreementSummary:
    material={k:v for k,v in packets.items() if v and v.implementation_signature and v.implementation_signature.normalized_patch_hash}
    if not material:
        return CandidateAgreementSummary(consensus_type='none', agreeing_candidates=[], material_differences=[], consensus_strength=0.0, rationale='No normalized patch hash/signature evidence available; changed files alone are not agreement proof.')
    hashes={k:v.implementation_signature.normalized_patch_hash for k,v in material.items()}
    groups={}
    for cid,h in hashes.items(): groups.setdefault(h,[]).append(cid)
    if len(groups)==1 and len(material)==len(packets):
        return CandidateAgreementSummary(consensus_type='same_patch', agreeing_candidates=sorted(material), material_differences=[], consensus_strength=1.0, rationale='All materializable candidates share the same normalized_patch_hash; agreement is based on patch signature, not only changed files.')
    token_sets=[set(v.implementation_signature.strategy_tokens) for v in material.values()]
    inter=set.intersection(*token_sets) if token_sets else set(); union=set.union(*token_sets) if token_sets else set()
    sim=(len(inter)/len(union)) if union else 0.0
    diffs=[f"{cid}: hash={str(sig)[:12]} tokens={packets[cid].implementation_signature.strategy_tokens}" for cid,sig in hashes.items()]
    if sim>=0.7 and len(groups)>1:
        ctype='same_strategy'; strength=0.65
    elif len(groups)>1:
        ctype='mixed'; strength=0.35
    else:
        ctype='none'; strength=0.0
    return CandidateAgreementSummary(consensus_type=ctype, agreeing_candidates=[], material_differences=diffs, consensus_strength=strength, rationale='Agreement evaluated from normalized_patch_hash and implementation signature strategy tokens; same changed files are not treated as same patch.')

def _rank_tournament(state):
    reviews=[apply_review_risk_penalties(CandidateRiskReview.model_validate(r.model_dump(mode='json'))) for r in state.candidate_risk_reviews.values()]
    review_has_critical_gap={r.candidate_id:has_unresolved_critical_evidence_gap(r) for r in reviews}
    wins={r.candidate_id:0 for r in reviews}; losses={r.candidate_id:0 for r in reviews}
    for c in state.pairwise_comparisons:
        if c.winner=='candidate_a' and c.candidate_a in wins and c.candidate_b in losses: wins[c.candidate_a]+=1; losses[c.candidate_b]+=1
        elif c.winner=='candidate_b' and c.candidate_b in wins and c.candidate_a in losses: wins[c.candidate_b]+=1; losses[c.candidate_a]+=1
    all_review_fallback=bool(reviews) and all(r.review_quality=='deterministic_fallback' for r in reviews)
    all_pairwise_fallback=bool(state.pairwise_comparisons) and all(c.comparison_quality=='deterministic_fallback' for c in state.pairwise_comparisons)
    all_ties=bool(state.pairwise_comparisons) and all(c.winner=='tie' for c in state.pairwise_comparisons)
    def eq(aid):
        e=state.candidate_evidence_packets.get(aid)
        return {'high':3,'medium':2,'low':1,'missing':0}.get(e.evidence_quality if e else 'missing',0)
    def cost(aid):
        c=next((x for x in state.candidates if x.attempt_id==aid), None)
        return float(c.cost or 0.0) if c else 0.0
    def key(r):
        summ=state.candidate_summaries.get(r.candidate_id)
        val=1 if summ and summ.validation_status=='passed' else 0
        review_quality={'model_full':3,'model_compact':2,'model_minimal':1,'deterministic_fallback':0}.get(r.review_quality,0)
        correctness=normalize_score(r.correctness_score); risk=normalize_score(r.hidden_test_risk_score); minimality=normalize_score(r.minimality_score); confidence=normalize_score(r.confidence)
        model_wins=sum(1 for c in state.pairwise_comparisons if c.comparison_quality!='deterministic_fallback' and ((c.winner=='candidate_a' and c.candidate_a==r.candidate_id) or (c.winner=='candidate_b' and c.candidate_b==r.candidate_id)))
        return (val,model_wins,wins[r.candidate_id],-losses[r.candidate_id],0 if review_has_critical_gap.get(r.candidate_id) else 1,-risk,correctness,review_quality,eq(r.candidate_id),minimality,-cost(r.candidate_id))
    ordered=sorted(reviews,key=key,reverse=True)
    ranked=[]
    for i,r in enumerate(ordered):
        e=state.candidate_evidence_packets.get(r.candidate_id)
        ranked.append(RankedCandidate(candidate_id=r.candidate_id,rank=i+1,correctness_score=normalize_score(r.correctness_score),hidden_test_risk_score=normalize_score(r.hidden_test_risk_score),pairwise_wins=wins[r.candidate_id],pairwise_losses=losses[r.candidate_id],validation_status=(state.candidate_summaries.get(r.candidate_id).validation_status if state.candidate_summaries.get(r.candidate_id) else None),materiality_notes=(f"evidence={e.evidence_quality}; review_quality={r.review_quality}; normalized_scores=correctness:{normalize_score(r.correctness_score):.2f},risk:{normalize_score(r.hidden_test_risk_score):.2f}; " if e else f"evidence=missing; review_quality={r.review_quality}; normalized_scores=correctness:{normalize_score(r.correctness_score):.2f},risk:{normalize_score(r.hidden_test_risk_score):.2f}; ")+('; '.join(r.changed_files) or 'no material files')))
    selected=ranked[0].candidate_id if ranked else None
    selected_validated=bool(selected and state.candidate_summaries.get(selected) and state.candidate_summaries[selected].validation_status=='passed')
    selected_has_gap=bool(selected and review_has_critical_gap.get(selected))
    discriminative_reviews=False
    if len(ordered)>1:
        a,b=ordered[0],ordered[1]
        discriminative_reviews=abs((normalize_score(a.correctness_score)-normalize_score(a.hidden_test_risk_score))-(normalize_score(b.correctness_score)-normalize_score(b.hidden_test_risk_score)))>=0.10 and normalize_score(a.confidence)>=0.55
    if selected_validated: basis='validated_acceptance'; conf=0.85
    elif all_pairwise_fallback and all_review_fallback: basis='best_effort_tournament_selection'; conf=0.35
    elif selected_has_gap or all_pairwise_fallback:
        basis='best_effort_tournament_selection'; conf=0.5 if (all_pairwise_fallback and discriminative_reviews and not selected_has_gap) else 0.35
    else:
        basis='evidence_based_tournament_selection' if len(ranked)>1 else 'best_effort_tournament_selection'; conf=0.65
    if all_ties and not selected_validated: conf=min(conf,0.5)
    if selected_has_gap and not selected_validated: conf=min(conf,0.45)
    state.selection_basis=basis
    risks=[] if basis=='validated_acceptance' else ['no authoritative validation pass for selected candidate']
    if all_review_fallback or all_pairwise_fallback: risks.append('review/comparison unavailable or deterministic fallback only')
    if all_ties: risks.append('all pairwise comparisons tied; ranking confidence is low')
    if selected and eq(selected)<=1: risks.append('selected candidate evidence quality is low or missing')
    if selected_has_gap: risks.append('selected candidate has unresolved critical evidence gaps; risk penalties applied')
    return TournamentRanking(ranked_candidates=ranked,selected_candidate_id=selected,selection_confidence=normalize_score(conf if selected else 0.0),unresolved_risks=sorted(dict.fromkeys(risks)),rationale='Ranking priority: authoritative validation, model-backed pairwise wins, pairwise wins/losses after critical-gap penalties, absence of unresolved critical evidence gaps, normalized hidden-test risk, normalized correctness, normalized minimality/scope, candidate agreement, then cost/tokens only as tiebreaker. Fallback-only pairwise comparisons cap confidence and may force best-effort selection.')



def _is_tournament_candidate_materializable(a)->tuple[bool,list[str]]:
    blockers=[]
    if a.status not in {'completed','reviewed','accepted'}: blockers.append('candidate_not_completed')
    if not a.patch_path: blockers.append('candidate_patch_missing')
    elif not Path(a.patch_path).exists(): blockers.append('candidate_patch_path_missing')
    if not (a.changed_files or []): blockers.append('candidate_changed_files_missing')
    if not (a.worktree_path or a.artifacts_dir): blockers.append('candidate_materialization_location_missing')
    elif a.worktree_path and not Path(a.worktree_path).exists() and (not a.artifacts_dir or not Path(a.artifacts_dir).exists()): blockers.append('candidate_worktree_or_artifacts_missing')
    return not blockers, blockers

def _best_effort_rank_materializable_candidates(state)->TournamentRanking:
    scored=[]
    for c in state.candidates:
        ok, blockers=_is_tournament_candidate_materializable(c)
        if not ok: continue
        validation=1 if c.validation_status=='passed' or ((c.validation or {}).get('passed') is True) else 0
        review=1 if c.review_status=='passed' or ((c.review or {}).get('decision')=='pass') else 0
        runner=1 if (c.runner_status in {None,'completed','succeeded','success'} and c.status in {'completed','reviewed','accepted'}) else 0
        nonempty=1 if c.changed_files else 0
        minimality=-len(c.changed_files or [])
        telemetry=-(float(c.cost or 0.0))
        scored.append(((validation,review,runner,nonempty,minimality,telemetry,c.attempt_id), c))
    ordered=[c for _score,c in sorted(scored, key=lambda x:x[0], reverse=True)]
    ranked=[RankedCandidate(candidate_id=c.attempt_id,rank=i+1,correctness_score=0.7 if c.validation_status=='passed' else 0.45,hidden_test_risk_score=0.2 if c.validation_status=='passed' else 0.55,pairwise_wins=0,pairwise_losses=0,validation_status=c.validation_status,materiality_notes='; '.join(c.changed_files or []) or 'material patch available') for i,c in enumerate(ordered)]
    state.selection_basis='best_effort_tournament_selection' if ranked else 'failed'
    return TournamentRanking(ranked_candidates=ranked,selected_candidate_id=(ranked[0].candidate_id if ranked else None),selection_confidence=0.6 if ranked else 0.0,unresolved_risks=[] if ranked and ranked[0].validation_status=='passed' else ['best-effort selection from available candidate evidence'],rationale='Best-effort tournament ranking from completed materializable candidates using validation, review, runner success, patch presence, minimality, and telemetry as a tiebreaker.')

def commit_tournament_selection(state, ctx=None)->bool:
    if state.execution_path!='candidate_tournament': return False
    ranking=state.tournament_ranking
    if ranking is None or not ranking.selected_candidate_id:
        if any(_is_tournament_candidate_materializable(c)[0] for c in state.candidates):
            state.tournament_ranking=_best_effort_rank_materializable_candidates(state); ranking=state.tournament_ranking
        else:
            return False
    sel=state.selection or {}
    if sel.get('decision')=='select' and sel.get('selected_attempt_id'):
        if sel.get('selected_attempt_id')==ranking.selected_candidate_id:
            state.phase='finalizing'; return False
        return False
    candidates=[r.candidate_id for r in ranking.ranked_candidates]
    if ranking.selected_candidate_id and ranking.selected_candidate_id not in candidates: candidates.insert(0, ranking.selected_candidate_id)
    skipped=[]; selected=None; blockers=[]
    for aid in candidates:
        a, st=_find_attempt(state, aid)
        if st is not None or not isinstance(a, CandidateAttemptState):
            skipped.append({'attempt_id':aid,'reason':'not_candidate_attempt'}); continue
        ok, bs=_is_tournament_candidate_materializable(a)
        if ok:
            selected=a; break
        skipped.append({'attempt_id':aid,'reason':','.join(bs)}); blockers.extend(bs)
    if selected is None:
        fallback=next((c for c in state.candidates if _is_tournament_candidate_materializable(c)[0]), None)
        if fallback is not None:
            selected=fallback; state.selection_basis='best_effort_tournament_selection'
        else:
            state.selection_basis='failed'; state.blockers=sorted(set(state.blockers+blockers+['no_materializable_tournament_candidate']))
            return False
    basis=state.selection_basis or ('validated_acceptance' if selected.validation_status=='passed' else 'evidence_based_tournament_selection')
    state.selection_basis=basis
    state.selection={'decision':'select','selected_attempt_id':selected.attempt_id,'selection_basis':basis,'summary':f'Tournament ranking selected {selected.attempt_id}; committed deterministic tournament selection.','reasons':[ranking.rationale],'confidence':ranking.selection_confidence or 0.6,'unresolved_risks':list(ranking.unresolved_risks or []),'ranking_source':'tournament_ranking','selection_evidence':{'ranking_selected_candidate_id':ranking.selected_candidate_id,'skipped_ranked_candidates':skipped,'changed_files':selected.changed_files,'validation_status':selected.validation_status,'pairwise_model_ran':bool((state.adaptive_context or {}).get('pairwise_model_ran')),'pairwise_coverage':(state.adaptive_context or {}).get('pairwise_coverage'),'pairwise_skip_reason':(state.adaptive_context or {}).get('pairwise_skip_reason')}}
    state.phase='finalizing'
    root=Path(state.run_dir); write_json_utf8(root/'selection.json', state.selection)
    if ctx is not None and getattr(ctx,'recorder',None) is not None:
        ctx.recorder.record('selection_completed', payload=state.selection)
    return True

def _write_tournament_artifacts(state):
    root=Path(state.run_dir); (root/'candidates').mkdir(exist_ok=True); (root/'reviews').mkdir(exist_ok=True); (root/'comparisons').mkdir(exist_ok=True)
    for c in state.candidates:
        d=root/'candidates'/c.attempt_id; d.mkdir(parents=True,exist_ok=True)
        if c.patch_path and Path(c.patch_path).exists(): write_text_utf8(d/'patch.diff', read_text_utf8(Path(c.patch_path), default=''))
        write_json_utf8(d/'runner_summary.json', c.model_dump(mode='json'))
        if c.attempt_id in state.candidate_evidence_packets:
            write_json_utf8(d/'evidence.json', state.candidate_evidence_packets[c.attempt_id].model_dump(mode='json'))
    for k,v in state.candidate_risk_reviews.items(): write_json_utf8(root/'reviews'/f'{k}.json', v.model_dump(mode='json'))
    write_json_utf8(root/'comparisons'/'pairwise.json', [c.model_dump(mode='json') for c in state.pairwise_comparisons])
    if state.tournament_ranking: write_json_utf8(root/'comparisons'/'ranking.json', state.tournament_ranking.model_dump(mode='json'))
    if state.candidate_agreement_summary: write_json_utf8(root/'comparisons'/'agreement.json', state.candidate_agreement_summary.model_dump(mode='json'))
    write_json_utf8(root/'selection.json', state.selection or {'selected_candidate_id': state.tournament_ranking.selected_candidate_id if state.tournament_ranking else None, 'selection_basis': state.selection_basis})

    selected=state.tournament_ranking.selected_candidate_id if state.tournament_ranking else None
    model_reviews=sum(1 for r in state.candidate_risk_reviews.values() if r.review_quality!='deterministic_fallback'); fallback_reviews=sum(1 for r in state.candidate_risk_reviews.values() if r.review_quality=='deterministic_fallback')
    model_cmps=sum(1 for c in state.pairwise_comparisons if c.comparison_quality!='deterministic_fallback'); fallback_cmps=sum(1 for c in state.pairwise_comparisons if c.comparison_quality=='deterministic_fallback')
    emergency=state.selection_basis=='best_effort_tournament_selection' and (fallback_reviews or fallback_cmps or not state.pairwise_comparisons)
    lines=["# Adaptive Candidate Tournament","",f"Candidates requested: {state.candidate_attempts_requested}",f"Launched: {state.candidate_attempts_launched}",f"Completed: {state.tournament_candidates_completed}",f"Tournament phase: {state.tournament_phase}",f"Stages completed: candidates={bool(state.candidate_evidence_packets)}, reviews={bool(state.candidate_risk_reviews)}, comparisons={bool(state.pairwise_comparisons)}, ranking={bool(state.tournament_ranking)}, selection={bool(state.selection)}",f"Parallelism used: {state.tournament_parallelism_used}",f"Reviews: model-backed={model_reviews}, fallback={fallback_reviews}",f"Comparisons: model-backed={model_cmps}, fallback={fallback_cmps}",f"Model pairwise ran: {'yes' if (state.adaptive_context or {}).get('pairwise_model_ran') else 'no'}",f"Pairwise coverage: {(state.adaptive_context or {}).get('pairwise_coverage') or ('fallback-only' if fallback_cmps else 'not_run')}",f"Pairwise skip reason: {(state.adaptive_context or {}).get('pairwise_skip_reason') or 'none'}",f"Emergency finalization used: {emergency}",f"Selected: {selected}",f"Selection basis: {state.selection_basis}",f"Selection confidence: {normalize_score(state.tournament_ranking.selection_confidence) if state.tournament_ranking else 0.0:.2f}","Score normalization: numeric review/comparison/ranking scores are normalized to 0.0-1.0; values above 1 up to 10 are treated as 0-10 scores.","","## Why the selected candidate won"]
    if selected:
        ev=state.candidate_evidence_packets.get(selected); rv=state.candidate_risk_reviews.get(selected)
        lines += [f"Ranking rationale: {state.tournament_ranking.rationale if state.tournament_ranking else ''}", f"Evidence quality: {ev.evidence_quality if ev else 'missing'}", f"Changed files: {', '.join((ev.changed_files if ev else []) or [])}", f"Debug artifacts showed: {(ev.runner_summary if ev else 'unavailable')}", f"Commands run: {', '.join(c.command for c in (ev.commands_executed if ev else [])[:8]) or 'none observed'}", f"Review quality: {rv.review_quality if rv else 'missing'}", f"Normalized correctness score: {normalize_score(rv.correctness_score) if rv else 0.0:.2f}", f"Normalized hidden-test risk score: {normalize_score(rv.hidden_test_risk_score) if rv else 0.0:.2f}", f"Comparison quality: {', '.join(sorted({x.comparison_quality for x in state.pairwise_comparisons if x.candidate_a==selected or x.candidate_b==selected})) or 'none'}", f"Critical evidence gaps: {', '.join((rv.critical_evidence_gaps if rv else []) or []) or 'none'}", f"Risk penalty applied: {bool(rv and rv.risk_penalty_applied)}", f"Original scores: {(rv.original_scores if rv else {})}", f"Pairwise fallback reasons: {', '.join(sorted({str(x.fallback_reason) for x in state.pairwise_comparisons if (x.candidate_a==selected or x.candidate_b==selected) and x.fallback_reason})) or 'none'}", f"Selection downgrade reason: {', '.join((state.tournament_ranking.unresolved_risks if state.tournament_ranking else []) or []) or 'none'}", f"Remaining risks: {', '.join((state.tournament_ranking.unresolved_risks if state.tournament_ranking else []) or []) or 'none recorded'}"]
    lines += ["","## Rejected candidates"]
    for c in state.candidates:
        if c.attempt_id==selected: continue
        ev=state.candidate_evidence_packets.get(c.attempt_id); rv=state.candidate_risk_reviews.get(c.attempt_id)
        losses=[x for x in state.pairwise_comparisons if (x.candidate_a==c.attempt_id and x.winner=='candidate_b') or (x.candidate_b==c.attempt_id and x.winner=='candidate_a')]
        lines += [f"### {c.attempt_id}", f"Why lost: {'lost pairwise comparison(s)' if losses else 'ranked lower by evidence/risk/minimality or tied fallback ordering'}", f"Material differences: {', '.join((ev.changed_files if ev else c.changed_files) or [])}", f"Specific risks: {', '.join((rv.risks if rv else []) or (ev.potential_risks if ev else []) or []) or 'none recorded'}", f"Review quality: {rv.review_quality if rv else 'missing'}", f"Normalized correctness score: {normalize_score(rv.correctness_score) if rv else 0.0:.2f}", f"Normalized hidden-test risk score: {normalize_score(rv.hidden_test_risk_score) if rv else 0.0:.2f}", f"Comparison quality: {', '.join(sorted({x.comparison_quality for x in state.pairwise_comparisons if x.candidate_a==c.attempt_id or x.candidate_b==c.attempt_id})) or 'none'}", f"Critical evidence gaps: {', '.join((rv.critical_evidence_gaps if rv else []) or []) or 'none'}", f"Risk penalty applied: {bool(rv and rv.risk_penalty_applied)}", f"Evidence gaps: {', '.join((rv.evidence_gaps if rv else []) or (ev.evidence_limitations if ev else ['evidence packet missing']))}"]
    write_text_utf8(root/'final_report.md', '\n'.join(lines)+'\n')

def _persist_tournament_state(state, ctx=None):
    _write_tournament_artifacts(state)
    state.save(Path(state.run_dir)/'state.json')
    if ctx is not None and getattr(ctx,'recorder',None): ctx.recorder.record('state_saved', tool_name='tournament_stage')

def _record_completed_tournament_candidate(state, attempt:CandidateAttemptState, ctx=None):
    state.tournament_candidates_completed=sum(1 for c in state.candidates if c.status in {'completed','failed','reviewed','rejected','accepted'})
    state.candidate_summaries[attempt.attempt_id]=_candidate_summary_from_attempt(attempt)
    state.candidate_evidence_packets[attempt.attempt_id]=build_candidate_evidence_packet(state, attempt)
    ok, blockers=_is_tournament_candidate_materializable(attempt)
    state.candidate_summaries[attempt.attempt_id].obvious_risks=sorted(set(state.candidate_summaries[attempt.attempt_id].obvious_risks + ([] if ok else blockers)))
    _persist_tournament_state(state, ctx)
    if ctx is not None and getattr(ctx,'recorder',None): ctx.recorder.record('tournament_candidate_state_saved', payload={'attempt_id':attempt.attempt_id,'status':attempt.status,'materializable':ok})

def hydrate_tournament_state_from_artifacts(state, run_dir=None)->bool:
    root=Path(run_dir or state.run_dir); changed=False
    cand_root=root/'candidates'
    if cand_root.exists():
        for evp in cand_root.glob('*/evidence.json'):
            cid=evp.parent.name
            if cid not in state.candidate_evidence_packets:
                state.candidate_evidence_packets[cid]=CandidateEvidencePacket.model_validate(json.loads(read_text_utf8(evp))); changed=True
        for rsp in cand_root.glob('*/runner_summary.json'):
            cid=rsp.parent.name
            if not any(c.attempt_id==cid for c in state.candidates):
                state.candidates.append(CandidateAttemptState.model_validate(json.loads(read_text_utf8(rsp)))); changed=True
            if cid not in state.candidate_summaries:
                a=next((c for c in state.candidates if c.attempt_id==cid), None)
                if a: state.candidate_summaries[cid]=_candidate_summary_from_attempt(a); changed=True
    rp=root/'comparisons'/'ranking.json'
    if rp.exists() and state.tournament_ranking is None:
        state.tournament_ranking=TournamentRanking.model_validate(json.loads(read_text_utf8(rp))); changed=True
    sp=root/'selection.json'
    if sp.exists() and not state.selection:
        data=json.loads(read_text_utf8(sp))
        if data.get('decision')=='select': state.selection=data; changed=True
    if changed:
        state.tournament_candidates_completed=sum(1 for c in state.candidates if c.status in {'completed','failed','reviewed','rejected','accepted'})
        if state.tournament_ranking: state.tournament_phase='ranking'
        elif state.candidate_evidence_packets: state.tournament_phase='candidates_complete'
    return changed

def _seconds_remaining(deadline):
    return max(0.0, float(deadline) - time.time())

def _reserve_value(state, name, default):
    return max(0, int(getattr(state, name, default) or default))

def _time_low(state, deadline):
    return time.time() + _reserve_value(state, 'reserve_finalization_seconds', 90) >= deadline

def _tournament_budget_plan(state, deadline)->dict:
    finalization=_reserve_value(state, 'reserve_finalization_seconds', 90)
    pairwise=_reserve_value(state, 'reserve_pairwise_seconds', 180)
    ranking=_reserve_value(state, 'reserve_ranking_seconds', 30)
    review_cap=_reserve_value(state, 'max_candidate_review_seconds', 240)
    review_call=_reserve_value(state, 'per_candidate_review_timeout_seconds', 30)
    pairwise_call=_reserve_value(state, 'per_pairwise_comparison_timeout_seconds', 30)
    return {'deadline':deadline,'time_remaining_seconds':_seconds_remaining(deadline),'reserve_finalization_seconds':finalization,'reserve_pairwise_seconds':pairwise,'reserve_ranking_seconds':ranking,'max_candidate_review_seconds':review_cap,'expected_review_call_budget_seconds':review_call,'expected_pairwise_call_budget_seconds':pairwise_call}

def _can_spend_candidate_review_budget(state, deadline, spent_review):
    p=_tournament_budget_plan(state, deadline)
    if spent_review + p['expected_review_call_budget_seconds'] > p['max_candidate_review_seconds']:
        return False, 'candidate_review_budget_exhausted'
    need=p['reserve_finalization_seconds']+p['reserve_pairwise_seconds']+p['reserve_ranking_seconds']+p['expected_review_call_budget_seconds']
    if p['time_remaining_seconds'] <= need:
        return False, 'candidate_review_skipped_protected_pairwise_or_finalization_reserve'
    return True, None

def _can_spend_pairwise_budget(state, deadline):
    p=_tournament_budget_plan(state, deadline)
    need=p['reserve_finalization_seconds']+p['reserve_ranking_seconds']+p['expected_pairwise_call_budget_seconds']
    if p['time_remaining_seconds'] <= need:
        return False, 'pairwise_skipped_pairwise_budget_exhausted'
    return True, None

def h_launch_tournament_candidates(state, inp, ctx):
    if state.execution_path!='candidate_tournament': raise ValueError('ops_launch_tournament_candidates requires execution_path=candidate_tournament')
    if state.candidates:
        hydrate_tournament_state_from_artifacts(state); return {'launched':[], 'already_launched':True, 'next_allowed_actions':state.allowed_next_actions()}
    requested=max(1,int(inp.attempts or state.candidate_attempts or 1)); state.candidate_attempts_requested=requested; state.attempts_requested=requested; state.phase='running_candidates'; state.tournament_phase='launching_candidates'; state.candidate_execution_mode='parallel'
    maxp=max(1, int(getattr(ctx.coding_backend or ctx.backend,'max_parallel',None) or ctx.max_parallel or 1)); state.max_parallel=maxp; state.tournament_parallelism_used=min(maxp,requested)
    state.candidate_generation_deadline=time.time()+max(1,(ctx.timeout_seconds or 3600)-state.reserve_review_seconds-state.reserve_finalization_seconds)
    made=[]; next_index=1; batch_count=0; _persist_tournament_state(state, ctx)
    for off in range(0,requested,maxp):
        if time.time()>=state.candidate_generation_deadline: state.candidate_launch_limit_reason='generation_deadline_reached'; break
        batch=list(range(off,min(off+maxp,requested))); batch_count+=1; futs={}
        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            for _ in batch:
                aid=f'candidate_{next_index:03d}'; next_index+=1; made.append(aid)
                prompt=build_tournament_candidate_prompt(state, reason=inp.reason)
                scheduled=_attempt(aid,'candidate',backend=inp.backend_name or ctx.coding_backend_name or ctx.backend_name,artifacts=Path(state.run_dir)/'attempts'/aid); scheduled.status='running'; scheduled.started_at=str(time.time()); scheduled.worktree_path=str(Path(state.run_dir)/'attempts'/aid/'worktree'); state.candidates.append(scheduled)
                state.tournament_candidates_launched=len(made); state.candidate_attempts_launched=len(made); _persist_tournament_state(state, ctx)
                ctx.recorder.record('candidate_attempt_started', payload={'attempt_id':aid,'execution_path':'candidate_tournament','batch_index':batch_count})
                futs[ex.submit(_run_attempt,state,ctx,aid,'candidate',prompt,state.success_criteria,None,inp.backend_name or ctx.coding_backend_name or ctx.backend_name,False)]=aid
            for fut in as_completed(futs):
                try: res=fut.result()
                except Exception as e:
                    aid=futs[fut]; res=next(c for c in state.candidates if c.attempt_id==aid); res.status='failed'; res.failure_reason=str(e)
                for i,c in enumerate(state.candidates):
                    if c.attempt_id==res.attempt_id: state.candidates[i]=res; break
                _record_completed_tournament_candidate(state,res,ctx)
                ctx.recorder.record('candidate_attempt_completed' if res.status=='completed' else 'candidate_attempt_failed', payload={'attempt_id':res.attempt_id,'status':res.status,'execution_path':'candidate_tournament'})
    state.tournament_candidates_launched=len(made); state.candidate_attempts_launched=len(made); state.tournament_phase='candidates_complete'; _persist_tournament_state(state, ctx)
    return {'launched':made,'max_parallel':maxp,'candidate_summaries':{k:v.model_dump(mode='json') for k,v in state.candidate_summaries.items()},'next_allowed_actions':state.allowed_next_actions()}

def _rough_candidate_order(state, candidate_ids:list[str])->list[str]:
    def q(cid):
        e=state.candidate_evidence_packets.get(cid)
        r=state.candidate_risk_reviews.get(cid)
        c=next((x for x in state.candidates if x.attempt_id==cid), None)
        material=1 if c and _is_tournament_candidate_materializable(c)[0] else 0
        validation=1 if c and (c.validation_status=='passed' or ((c.validation or {}).get('passed') is True)) else 0
        evidence={'high':3,'medium':2,'low':1,'missing':0}.get(e.evidence_quality if e else 'missing',0)
        review_quality={'model_full':3,'model_compact':2,'model_minimal':1,'deterministic_fallback':0}.get(r.review_quality if r else 'deterministic_fallback',0)
        correctness=normalize_score(r.correctness_score) if r else 0.45
        risk=normalize_score(r.hidden_test_risk_score) if r else 0.55
        gap=1 if r and has_unresolved_critical_evidence_gap(r) else 0
        minimality=-(len(e.changed_files if e else (c.changed_files if c else [])) or 0)
        runner=1 if c and (c.runner_status in {None,'completed','succeeded','success'} and c.status in {'completed','reviewed','accepted'}) else 0
        return (material,validation,runner,review_quality,0-gap,evidence,correctness,-risk,minimality,cid)
    return sorted(candidate_ids, key=q, reverse=True)

def _pairwise_pairs_by_priority(ordered:list[str])->list[tuple[str,str]]:
    pairs=[]
    if len(ordered)>=2: pairs.append((ordered[0], ordered[1]))
    if len(ordered)>=3:
        for p in [(ordered[0],ordered[2]),(ordered[1],ordered[2])]:
            if p not in pairs: pairs.append(p)
    for i in range(len(ordered)):
        for j in range(i+1,len(ordered)):
            p=(ordered[i],ordered[j])
            if p not in pairs: pairs.append(p)
    return pairs

def _fallback_reason_for_model_pairwise(ctx, cmp):
    if _review_backend_from_ctx(ctx) is None: return 'pairwise_skipped_no_review_backend'
    attempts=getattr(cmp, 'model_attempts', []) if cmp else []
    if not attempts: return 'pairwise_model_failed_malformed'
    cats=[a.get('error_category') or a.get('status') for a in attempts]
    if any(c=='timeout' for c in cats): return 'pairwise_model_failed_timeout'
    if any(c=='malformed' for c in cats): return 'pairwise_model_failed_parse'
    return 'pairwise_model_failed_malformed'

def h_evaluate_tournament(state, inp, ctx):
    if state.execution_path!='candidate_tournament': raise ValueError('ops_evaluate_tournament requires execution_path=candidate_tournament')
    hydrate_tournament_state_from_artifacts(state); deadline=time.time()+max(1,int(state.tournament_evaluation_deadline_seconds or 120))
    budget=_tournament_budget_plan(state, deadline)
    if getattr(ctx,'recorder',None): ctx.recorder.record('tournament_evaluation_budget_planned', payload=budget)
    for c in state.candidates:
        if c.attempt_id not in state.candidate_summaries or c.attempt_id not in state.candidate_evidence_packets: _record_completed_tournament_candidate(state,c,ctx)
    material_ids=[c.attempt_id for c in state.candidates if _is_tournament_candidate_materializable(c)[0] and c.attempt_id in state.candidate_summaries]
    if _seconds_remaining(deadline) <= budget['reserve_finalization_seconds'] and material_ids:
        for cid in material_ids:
            if cid not in state.candidate_risk_reviews and cid in state.candidate_summaries:
                state.candidate_risk_reviews[cid]=_risk_review_from_summary(state.candidate_summaries[cid], state.candidate_evidence_packets.get(cid))
        state.tournament_ranking=_best_effort_rank_materializable_candidates(state); commit_tournament_selection(state, ctx); state.tournament_phase='selection_committed' if state.selection else 'failed'; _persist_tournament_state(state, ctx); return {'reviewed':list(state.candidate_risk_reviews),'pairwise_comparisons':len(state.pairwise_comparisons),'pairwise_model_ran':False,'pairwise_coverage':'not_run','pairwise_skip_reason':'pairwise_skipped_not_enough_time_after_candidate_generation','budget_plan':budget,'tournament_ranking':state.tournament_ranking.model_dump(mode='json') if state.tournament_ranking else None,'selection':state.selection,'next_allowed_actions':state.allowed_next_actions()}
    state.tournament_phase='reviewing_candidates'; _persist_tournament_state(state, ctx)
    spent_review=0
    # Build cheap fallback reviews first so every materializable candidate can be roughly ranked.
    for cid,summ in list(state.candidate_summaries.items()):
        if cid not in state.candidate_risk_reviews:
            state.candidate_risk_reviews[cid]=_risk_review_from_summary(summ, state.candidate_evidence_packets.get(cid)); _persist_tournament_state(state, ctx)
    # Upgrade reviews compact/minimal/full only when protected pairwise/finalization/ranking reserves remain.
    for cid in _rough_candidate_order(state, list(state.candidate_summaries)):
        packet=state.candidate_evidence_packets.get(cid)
        if not packet: continue
        ok, reason=_can_spend_candidate_review_budget(state, deadline, spent_review)
        if not ok:
            if getattr(ctx,'recorder',None): ctx.recorder.record('tournament_candidate_review_model_skipped', payload={**_tournament_budget_plan(state, deadline),'candidate_id':cid,'reason':reason})
            continue
        allowed=('model_full','model_compact','model_minimal') if _seconds_remaining(deadline) > (budget['reserve_finalization_seconds']+budget['reserve_pairwise_seconds']+budget['reserve_ranking_seconds']+2*budget['expected_review_call_budget_seconds']*max(1,len(material_ids))) else ('model_compact','model_minimal')
        review=_model_candidate_risk_review(state, packet, ctx, allowed_qualities=allowed)
        spent_review += budget['expected_review_call_budget_seconds']
        if review: state.candidate_risk_reviews[cid]=review; _persist_tournament_state(state, ctx)
    state.tournament_phase='comparing_candidates'; _persist_tournament_state(state, ctx)
    state.candidate_risk_reviews={cid:apply_review_risk_penalties(rv) for cid,rv in state.candidate_risk_reviews.items()}
    ordered=_rough_candidate_order(state, material_ids)
    existing={tuple(sorted((c.candidate_a,c.candidate_b))) for c in state.pairwise_comparisons}
    model_pairwise_count=0; pairwise_skip_reason=None
    for a_id,b_id in _pairwise_pairs_by_priority(ordered):
        if tuple(sorted((a_id,b_id))) in existing: continue
        ae=state.candidate_evidence_packets.get(a_id); be=state.candidate_evidence_packets.get(b_id); ar=state.candidate_risk_reviews.get(a_id); br=state.candidate_risk_reviews.get(b_id)
        if not (ae and be and ar and br): continue
        ok, reason=_can_spend_pairwise_budget(state, deadline)
        if not ok and model_pairwise_count>0:
            pairwise_skip_reason=reason
            break
        cmp=None; fallback_reason=None
        if _review_backend_from_ctx(ctx) is None:
            fallback_reason='pairwise_skipped_no_review_backend'
        elif not ok and model_pairwise_count==0:
            fallback_reason=reason
            if _seconds_remaining(deadline) > budget['reserve_finalization_seconds']+budget['reserve_ranking_seconds']:
                cmp=_model_pairwise_comparison(state, ae, be, ar, br, ctx)
        else:
            cmp=_model_pairwise_comparison(state, ae, be, ar, br, ctx)
        if cmp and cmp.comparison_quality!='deterministic_fallback':
            model_pairwise_count+=1; state.pairwise_comparisons.append(cmp)
        else:
            fb=_compare_pair(ar, br, ae, be); fb.fallback_reason=fallback_reason or _fallback_reason_for_model_pairwise(ctx, cmp); state.pairwise_comparisons.append(fb)
            if model_pairwise_count==0: pairwise_skip_reason=fb.fallback_reason
        _persist_tournament_state(state, ctx)
    coverage='not_run'
    if model_pairwise_count:
        model_pairs={tuple(sorted((c.candidate_a,c.candidate_b))) for c in state.pairwise_comparisons if c.comparison_quality!='deterministic_fallback'}
        top2={tuple(sorted((ordered[0],ordered[1])))} if len(ordered)>=2 else set()
        top3={tuple(sorted(p)) for p in _pairwise_pairs_by_priority(ordered[:3])}
        allpairs={tuple(sorted(p)) for p in _pairwise_pairs_by_priority(ordered)}
        coverage='all' if allpairs and allpairs.issubset(model_pairs) else ('top3' if top3 and top3.issubset(model_pairs) else ('top2' if top2 and top2.issubset(model_pairs) else 'partial'))
    elif state.pairwise_comparisons: coverage='fallback-only'
    state.adaptive_context=dict(state.adaptive_context or {}, pairwise_model_ran=bool(model_pairwise_count), pairwise_coverage=coverage, pairwise_skip_reason=pairwise_skip_reason, tournament_evaluation_budget_plan=_tournament_budget_plan(state, deadline))
    state.tournament_phase='ranking'; state.candidate_agreement_summary=build_candidate_agreement_summary(state.candidate_evidence_packets); state.tournament_ranking=_rank_tournament(state); _persist_tournament_state(state, ctx)
    commit_tournament_selection(state, ctx); state.tournament_phase='selection_committed' if state.selection else 'failed'; _persist_tournament_state(state, ctx)
    return {'reviewed':list(state.candidate_risk_reviews),'pairwise_comparisons':len(state.pairwise_comparisons),'pairwise_model_ran':bool(model_pairwise_count),'pairwise_coverage':coverage,'pairwise_skip_reason':pairwise_skip_reason,'budget_plan':state.adaptive_context.get('tournament_evaluation_budget_plan'),'tournament_ranking':state.tournament_ranking.model_dump(mode='json') if state.tournament_ranking else None,'selection':state.selection,'next_allowed_actions':state.allowed_next_actions()}

def h_launch_candidates(state, inp, ctx):
    fallback_active=state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock'
    if fallback_active and not (getattr(state,'adaptive_context',{}) or {}).get('legacy_ops_launch_candidates_enabled'):
        raise ValueError('ops_launch_candidates is legacy/batch execution and is disabled during adaptive fallback. Use ops_run_next_fallback_candidate_attempt.')
    if state.execution_path=='single_task': raise ValueError('single_task execution uses adaptive sequential attempts; call ops_run_next_candidate_attempt')
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
                if fallback_active: scheduled.candidate_kind='fallback'
                scheduled.status='running'; scheduled.started_at=str(time.time()); scheduled.worktree_path=str(Path(state.run_dir)/'attempts'/aid/'worktree')
                state.candidates.append(scheduled)
                ctx.recorder.record('candidate_attempt_started', payload={'attempt_id':aid,'status':'running','batch_index':batch_count,'fallback':fallback_active,'artifact_paths':{'artifacts_dir':scheduled.artifacts_dir}})
                task_prompt=build_decomposition_fallback_prompt(state) if fallback_active else state.task
                futs.append(ex.submit(_run_attempt,state,ctx,aid,'candidate',task_prompt,state.success_criteria,None,inp.backend_name or ctx.coding_backend_name or ctx.backend_name,False))
            byid={}
            for fut in as_completed(futs):
                res=fut.result(); byid[res.attempt_id]=res
        for aid in ids:
            res=byid[aid]
            if fallback_active: res.candidate_kind='fallback'
            for idx,c in enumerate(state.candidates):
                if c.attempt_id==aid:
                    state.candidates[idx]=res; break
            ev='candidate_attempt_completed' if res.status=='completed' else 'candidate_attempt_failed'
            ctx.recorder.record(ev, payload={'attempt_id':aid,'status':res.status,'exit_code':res.exit_code,'failure_reason':res.failure_reason,'batch_index':batch_count,'fallback':fallback_active,'artifact_paths':{'stdout':res.stdout_path,'stderr':res.stderr_path,'patch':res.patch_path}, **({k:res.token_usage.get(k) for k in ['input_tokens','output_tokens','total_tokens','total_cost','usage_source'] if res.token_usage} if res.token_usage else {})})
        state.save(Path(state.run_dir)/'state.json')
    state.concurrency_mode=final_mode; state.batch_count=batch_count
    state.candidate_concurrency={'concurrency_mode':final_mode,'max_parallel':maxp,'batch_count':batch_count,'worker_state_mutation':'disabled'}
    state.execution_concurrency={'candidate_concurrency_mode':final_mode,'max_parallel':maxp,'candidate_batch_count':batch_count}
    state.phase='validating'; return {'launched':made,'max_parallel':maxp,'batch_count':batch_count,'concurrency_mode':final_mode,'semantics':'attempts execute in isolated worktrees; main thread mutates OpsRunState; batches never exceed max_parallel'}


def _coerce_validation_plan(raw, *, default_scope='candidate', subtask_id=None):
    if not raw: return None
    if isinstance(raw, ValidationPlan): return raw
    if isinstance(raw, dict):
        data=dict(raw)
        data.setdefault('scope', default_scope)
        if subtask_id is not None: data.setdefault('subtask_id', subtask_id)
        for old, new, auth in [('commands','commands',None),('authoritative_commands','authoritative_commands','acceptance_blocking'),('supporting_commands','supporting_commands','supporting_evidence'),('diagnostic_commands','diagnostic_commands','diagnostic_only')]:
            vals=[]
            for c in data.get(old) or []:
                vc=ValidationCommand.model_validate(c) if isinstance(c,dict) else c
                effective_auth=auth
                if effective_auth is None and old=='commands' and default_scope in {'candidate','integration','final','repo'}:
                    effective_auth='acceptance_blocking'
                if effective_auth and vc.authority is None: vc=vc.model_copy(update={'authority':effective_auth})
                vals.append(vc)
            data[new]=vals
        return ValidationPlan.model_validate(data)
    return None

def _plan_commands(plan:ValidationPlan|None):
    if not plan: return []
    out=[]
    for auth, items in [('acceptance_blocking',plan.authoritative_commands),('supporting_evidence',plan.supporting_commands),('diagnostic_only',plan.diagnostic_commands)]:
        for c in items:
            updates={'authority':auth,'scope':c.scope or plan.scope,'subtask_id':c.subtask_id or plan.subtask_id}
            out.append(c.model_copy(update={k:v for k,v in updates.items() if v is not None}))
    for c in plan.commands:
        out.append(c.model_copy(update={'authority':c.authority or 'supporting_evidence','scope':c.scope or plan.scope,'subtask_id':c.subtask_id or plan.subtask_id}))
    return out

def _default_validation_commands(state):
    plan=_coerce_validation_plan((state.investigation or {}).get('validation_plan') if state.investigation else None, default_scope='candidate')
    out=_plan_commands(plan)
    return out or [ValidationCommand(cmd='python -m pytest --tb=short -v', purpose='Default candidate validation', timeout_seconds=900, source='user_success_criteria', authority='acceptance_blocking', scope='candidate', reason='Default validation for the candidate decision')]

def _focused_subtask_validation_commands(state, subtask:SubtaskState):
    plan=_coerce_validation_plan((state.investigation or {}).get('validation_plan') if state.investigation else None, default_scope='subtask', subtask_id=subtask.subtask_id)
    cmds=[c for c in _plan_commands(plan) if (c.scope in {None,'subtask'}) and (c.subtask_id in {None, subtask.subtask_id})]
    if cmds: return cmds
    return []

def build_adaptive_subtask_runner_prompt(state, subtask:SubtaskState, *, reason:str, repair:bool=False, base_attempt_id:str|None=None)->str:
    accepted=[{'subtask_id':s.subtask_id,'accepted_attempt_id':s.accepted_attempt_id,'changed_files':(_accepted_subtask_attempt(state,s).changed_files if _accepted_subtask_attempt(state,s) else [])} for s in state.subtasks if s.status=='accepted']
    prompt=build_subtask_runner_prompt(parent_task=state.task,parent_success_criteria=state.success_criteria,subtask=subtask,allowed_files=subtask.relevant_files,forbidden_files=['.villani','.villani_code'],validation_commands=_focused_subtask_validation_commands(state, subtask),dependency_context=json.dumps({'dependencies':subtask.dependencies,'accepted_upstream_subtasks':accepted}, ensure_ascii=False),merge_contract=(state.decomposition or {}).get('merge_strategy') or '')
    progress=build_decomposition_progress_brief(state, subtask)
    learning=build_subtask_attempt_learning_brief(state, subtask)
    extras=[]
    if progress: extras.append(progress)
    if learning: extras.append(learning)
    extras.append('UPSTREAM INTEGRATION BASE\nThis subtask is running on top of previously accepted subtask patches.\nPreserve accepted upstream behaviour.\nDo not reimplement already accepted subtasks unless validation proves integration requires it.')
    extras.append('NEXT SUBTASK ATTEMPT DIRECTIVES\n- Run exactly this subtask attempt; do not solve unrelated sibling subtasks.\n- Use focused validation for this subtask when available.\n- Do not repeat previous validation, review, patch hygiene, or scope mistakes.')
    if repair and base_attempt_id:
        extras.append(f'REPAIR MODE\nRepair prior subtask attempt {base_attempt_id} using the learning above.')
    extras.append(f'REASON FOR THIS ATTEMPT\n{reason}')
    return prompt+'\n\n'+'\n\n'.join(extras)


def _observe_completed_attempt(state, attempt, ctx=None):
    obs=create_attempt_observation(state,attempt)
    update_backend_runner_assessments(state,obs,attempt)
    if ctx is not None:
        ctx.recorder.record('attempt_observation_created', payload=obs.model_dump())
    return obs

def h_observe_completed_attempt(state, inp, ctx):
    a=next((c for c in state.candidates if c.attempt_id==inp.attempt_id), None)
    if not a:
        a, _st=_find_attempt(state, inp.attempt_id)
    if not a or isinstance(a, dict):
        raise ValueError(f'unknown candidate/subtask attempt {inp.attempt_id}')
    if a.status not in {'completed','failed','reviewed','rejected','accepted'}:
        raise ValueError(f'attempt {inp.attempt_id} is not complete')
    obs=_observe_completed_attempt(state,a,ctx)
    return {'attempt_id':a.attempt_id,'observation':obs.model_dump(),'backend_assessment':state.backend_assessments.get(a.backend_name or 'unknown'),'reason':inp.reason}

def h_run_single_task_attempts(state, inp, ctx):
    if state.execution_path!='single_task':
        raise ValueError('ops_run_single_task_attempts requires execution_path=single_task')
    if state.candidates:
        return {'launched':[], 'attempts_requested':state.attempts_requested or inp.attempts, 'attempts_started':state.attempts_started, 'stopped_early':state.stopped_early, 'stop_reason':state.stop_reason or 'already_started'}
    state.candidate_execution_mode='sequential'; state.attempts_requested=int(inp.attempts); state.phase='running_candidates'
    made=[]
    for i in range(1, int(inp.attempts)+1):
        aid=f'candidate_{i:03d}'; made.append(aid); state.attempts_started=len(made)
        a=_run_attempt(state,ctx,aid,'candidate',state.task,state.success_criteria,None,inp.backend_name or ctx.coding_backend_name or ctx.backend_name,True)
        state.candidates.append(a)
        if a.status=='completed':
            cmds=_default_validation_commands(state)
            if cmds:
                h_validation(state, OpsRunValidationInput(target='candidate', target_id=aid, commands=cmds), ctx)
        if ctx.reviewer is not None and a.status in {'completed','reviewed'} and not a.review:
            h_review_attempt(state, OpsReviewAttemptInput(attempt_id=aid, scope='candidate'), ctx)
        eligible, blockers=_set_acceptance_from_gate(state,a)
        obs=_observe_completed_attempt(state,a,ctx)
        state.save(Path(state.run_dir)/'state.json')
        if eligible:
            state.stopped_early=True; state.stop_reason='accepted_attempt'; state.phase='selecting'
            ctx.recorder.record('single_task_attempt_accepted', payload={'attempt_id':aid,'attempts_started':state.attempts_started,'attempts_requested':state.attempts_requested,'stop_reason':state.stop_reason})
            h_select_winner(state, OpsSelectWinnerInput(decision='select', selected_attempt_id=aid, summary='Single-task sequential attempt passed review and validation.', reasons=['central acceptance gate passed'], confidence=0.95), ctx)
            return {'launched':made,'accepted_attempt_id':aid,'attempts_requested':state.attempts_requested,'attempts_started':state.attempts_started,'stopped_early':True,'stop_reason':'accepted_attempt','candidate_execution_mode':'sequential'}
        ctx.recorder.record('single_task_attempt_rejected', payload={'attempt_id':aid,'attempts_started':state.attempts_started,'acceptance_blockers':blockers,'retry':i<int(inp.attempts)})
    state.stopped_early=False; state.stop_reason='attempts_exhausted'; state.phase='selecting'
    return {'launched':made,'accepted_attempt_id':None,'attempts_requested':state.attempts_requested,'attempts_started':state.attempts_started,'stopped_early':False,'stop_reason':'attempts_exhausted','candidate_execution_mode':'sequential'}

def h_run_next_candidate_attempt(state, inp, ctx):
    if state.execution_path!='single_task':
        raise ValueError('ops_run_next_candidate_attempt requires execution_path=single_task')
    budget=max(1,int(state.candidate_attempts or 1))
    if len(state.candidates) >= budget:
        raise ValueError(f'candidate attempt budget exhausted ({len(state.candidates)}/{budget})')
    state.candidate_execution_mode='sequential'; state.attempts_requested=budget; state.phase='running_candidates'
    aid=f'candidate_{len(state.candidates)+1:03d}'
    prompt=build_candidate_runner_prompt(state, reason=inp.reason, repair=inp.repair, base_attempt_id=inp.base_attempt_id)
    a=_run_attempt(state,ctx,aid,'candidate',prompt,state.success_criteria,None,inp.backend_name,True)
    state.candidates.append(a); state.attempts_started=len(state.candidates)
    if a.status=='completed':
        cmds=_default_validation_commands(state)
        if cmds: h_validation(state, OpsRunValidationInput(target='candidate', target_id=aid, commands=cmds), ctx)
    if ctx.reviewer is not None and a.status in {'completed','reviewed'} and not a.review:
        h_review_attempt(state, OpsReviewAttemptInput(attempt_id=aid, scope='candidate'), ctx)
    eligible, blockers=_set_acceptance_from_gate(state,a)
    obs=_observe_completed_attempt(state,a,ctx)
    if eligible:
        state.stopped_early=True; state.stop_reason='accepted_attempt'; state.phase='selecting'
    elif len(state.candidates)>=budget:
        state.stopped_early=False; state.stop_reason='attempts_exhausted'; state.phase='selecting'
    else:
        state.phase='running_candidates'
    return {'launched':[aid],'attempt_id':aid,'attempts_started':state.attempts_started,'attempts_requested':budget,'observation':obs.model_dump(),'backend_assessment':state.backend_assessments.get(a.backend_name or 'unknown'),'next_allowed_actions':state.allowed_next_actions()}

def h_run_next_fallback_candidate_attempt(state, inp, ctx):
    if state.fallback_execution_path!='parallel_candidates_after_decomposition_deadlock':
        raise ValueError('ops_run_next_fallback_candidate_attempt requires decomposition-deadlock fallback mode')
    budget=max(1,int(state.candidate_attempts or 1))
    complete=[c for c in state.candidates if c.status in {'completed','failed','reviewed','rejected','accepted'}]
    if len(complete) >= budget:
        raise ValueError(f'fallback candidate budget exhausted ({len(complete)}/{budget})')
    state.candidate_execution_mode='sequential'; state.attempts_requested=budget; state.phase='running_candidates'
    aid=f'candidate_{len(state.candidates)+1:03d}'
    prompt=build_decomposition_fallback_prompt(state, reason=inp.reason, repair=inp.repair, base_attempt_id=inp.base_attempt_id)
    a=_run_attempt(state,ctx,aid,'candidate',prompt,state.success_criteria,None,inp.backend_name,True)
    a.candidate_kind='fallback'
    state.candidates.append(a); state.attempts_started=len(state.candidates)
    if a.status=='completed':
        cmds=_default_validation_commands(state)
        if cmds: h_validation(state, OpsRunValidationInput(target='candidate', target_id=aid, commands=cmds), ctx)
    if ctx.reviewer is not None and a.status in {'completed','reviewed'} and not a.review:
        h_review_attempt(state, OpsReviewAttemptInput(attempt_id=aid, scope='candidate'), ctx)
    eligible, blockers=_set_acceptance_from_gate(state,a)
    obs=_observe_completed_attempt(state,a,ctx)
    if eligible:
        state.stopped_early=True; state.stop_reason='accepted_fallback_attempt'; state.phase='selecting'
    elif len(state.candidates)>=budget:
        state.stopped_early=False; state.stop_reason='fallback_attempts_exhausted'; state.phase='selecting'
    else:
        state.phase='running_candidates'
    return {'launched':[aid],'attempt_id':aid,'candidate_kind':'fallback','attempts_started':state.attempts_started,'attempts_requested':budget,'observation':obs.model_dump(),'backend_assessment':state.backend_assessments.get(a.backend_name or 'unknown'),'next_allowed_actions':state.allowed_next_actions()}

def h_run_next_subtask_attempt(state, inp, ctx):
    if state.execution_path!='decomposed_subtasks':
        raise ValueError('ops_run_next_subtask_attempt requires execution_path=decomposed_subtasks')
    if inp.subtask_id:
        st=next((s for s in state.subtasks if s.subtask_id==inp.subtask_id), None)
        if not st: raise ValueError(f'unknown subtask {inp.subtask_id}')
    else:
        st,_last=select_next_subtask(state)
        if not st: raise ValueError('no retryable or ready subtask is available')
    if st.status=='accepted': raise ValueError(f'subtask {inp.subtask_id} is already accepted')
    ready_attempt=_subtask_commit_ready(state, st)
    if ready_attempt is not None:
        ok, blockers, app=_commit_subtask_acceptance(state, st, ready_attempt, ctx, reason='commit_ready_before_retry')
        obs=_observe_completed_attempt(state,ready_attempt,ctx)
        return {'launched':[],'attempt_id':ready_attempt.attempt_id,'subtask_id':st.subtask_id,'attempts_started':len(st.attempts),'attempts_requested':max(1,int(state.candidate_attempts or 1)),'observation':obs.model_dump(),'accepted':ok,'acceptance_blockers':blockers,'accepted_patch_application_status':app,'next_allowed_actions':state.allowed_next_actions()}
    by={s.subtask_id:s for s in state.subtasks}
    unmet=[d for d in st.dependencies if by.get(d) and by[d].status!='accepted']
    if unmet: raise ValueError(f'subtask {inp.subtask_id} has unmet dependencies: {unmet}')
    budget=max(1,int(state.candidate_attempts or 1))
    completed=[a for a in st.attempts if a.status in {'completed','failed','reviewed','rejected','accepted'}]
    if len(completed) >= budget:
        st.status='failed'; _update_decomposed_execution_state(state, ctx)
        raise ValueError(f'subtask attempt budget exhausted ({len(completed)}/{budget})')
    state.phase='running_subtasks'; state.decomposed_execution_status='running'; st.status='running'
    aid=f'{st.subtask_id}_attempt_{len(st.attempts)+1:03d}'
    prompt=build_adaptive_subtask_runner_prompt(state, st, reason=inp.reason, repair=inp.repair, base_attempt_id=inp.base_attempt_id)
    a=_run_attempt(state,ctx,aid,'subtask',prompt,st.success_criteria or state.success_criteria,subtask_id=st.subtask_id,backend_name=inp.backend_name,record_events=True)
    st.attempts.append(a)
    cmds=[]
    if a.status=='completed':
        cmds=_focused_subtask_validation_commands(state, st)
        if cmds: h_validation(state, OpsRunValidationInput(target='candidate', target_id=aid, commands=cmds), ctx)
    if ctx.reviewer is not None and a.status in {'completed','reviewed'} and not a.review:
        h_review_attempt(state, OpsReviewAttemptInput(attempt_id=aid, scope='subtask'), ctx)
    eligible, blockers=_set_acceptance_from_gate(state,a)
    # Subtask acceptance is scoped to the subtask contract. If no focused
    # validation is available, do not reject solely because the global
    # validation gate has not run; integration validation owns full-suite risk.
    if a.scope=='subtask' and not cmds and a.review_status=='passed' and blockers==['validation_missing']:
        eligible=True; blockers=[]; a.acceptance_eligible=True; a.acceptance_blockers=[]
    vdec=(a.validation or {}).get('decision') or {}
    if vdec.get('status')=='failed':
        eligible=False; blockers=sorted(set(blockers+['validation_failed'])); a.acceptance_eligible=False; a.acceptance_blockers=blockers
    elif vdec.get('status')=='passed' and 'validation_missing' in blockers:
        blockers=[b for b in blockers if b!='validation_missing']
    if eligible:
        eligible, blockers, app=_commit_subtask_acceptance(state, st, a, ctx, reason='focused_validation_and_review_accept')
    elif len([x for x in st.attempts if x.status in {'completed','failed','reviewed','rejected','accepted'}]) >= budget:
        st.status='failed'
    else:
        st.status='pending'
    obs=_observe_completed_attempt(state,a,ctx)
    dead=_update_decomposed_execution_state(state, ctx)
    if not dead and all(s.status in {'accepted','skipped'} for s in state.subtasks):
        state.phase='integrating'
    return {'launched':[aid],'attempt_id':aid,'subtask_id':st.subtask_id,'attempts_started':len(st.attempts),'attempts_requested':budget,'observation':obs.model_dump(),'accepted':eligible,'acceptance_blockers':blockers,'next_allowed_actions':state.allowed_next_actions()}

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
                        task=build_subtask_runner_prompt(parent_task=state.task,parent_success_criteria=state.success_criteria,subtask=st,allowed_files=st.relevant_files,forbidden_files=['.villani','.villani_code'],validation_commands=_validation_plan_commands(state, st),dependency_context=json.dumps(st.dependencies, ensure_ascii=False),merge_contract=(state.decomposition or {}).get('merge_strategy') or '')
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
    def _normalize_nulls(x):
        if isinstance(x,dict):
            return {k:(None if k in {'subtask_passed','integration_risk'} and isinstance(v,str) and v in {'null','None','none','NULL'} else _normalize_nulls(v)) for k,v in x.items()}
        if isinstance(x,list): return [_normalize_nulls(v) for v in x]
        return x
    def _compact_payload(payload, minimal=False):
        data=_attempt_to_dict(a)
        base={'task':state.task,'success_criteria':state.success_criteria,'changed_files':data.get('changed_files') or [],'current_validation':payload.get('current_validation') or payload.get('validation') or {},'patch_hygiene':data.get('patch_hygiene') or {},'scope':inp.scope,'known_blockers':data.get('acceptance_blockers') or []}
        if minimal:
            base['question']='Based only on this evidence, should this attempt be accepted, rejected, retried, or repaired?'
        else:
            base.update({'patch_excerpt':_read_patch_excerpt(data.get('patch_path'), max_chars=8000),'validation_output_summary':payload.get('validation',{}),'scope_assessment':data.get('scope_assessment')})
        return base
    payload=build_agentic_review_payload(state, a, inp.scope, st)
    raw=None; res=None; last_error=None; failure_kind=None
    payloads=[('full',payload),('compact',_compact_payload(payload)),('minimal',_compact_payload(payload, True))]
    for idx,(kind,attempt_payload) in enumerate(payloads,1):
        try:
            raw=ctx.reviewer.review(state=state, attempt=attempt_payload, scope=inp.scope) if hasattr(ctx.reviewer,'review') else ctx.reviewer(state,attempt_payload,inp.scope)
        except TypeError:
            try:
                raw=ctx.reviewer.review(state=state, attempt=a, scope=inp.scope) if hasattr(ctx.reviewer,'review') else ctx.reviewer(state,a,inp.scope)
            except Exception as e:
                last_error=e; raw=None
        except Exception as e:
            last_error=e; raw=None
        try:
            res=OpsReviewResult.model_validate(_normalize_nulls(raw))
            if getattr(ctx, 'usage_recorder', None) and getattr(ctx.reviewer, 'last_response', None) is not None:
                review_backend=getattr(ctx, 'review_backend', None) or getattr(ctx.reviewer, 'review_backend', None) or getattr(ctx, 'backend', None)
                usage_record=usage_record_from_response(run_id=state.run_id,phase='review',role='review',backend=review_backend,response=ctx.reviewer.last_response,attempt_id=inp.attempt_id,subtask_id=(st.subtask_id if st else None))
                ctx.usage_recorder.record(usage_record)
                summary=ctx.usage_recorder.summarize(); state.usage_summary=summary.model_dump(mode='json'); state.usage_records_count=summary.calls_count; state.total_input_tokens=summary.input_tokens; state.total_output_tokens=summary.output_tokens; state.total_tokens=summary.total_tokens; state.total_cost=summary.total_cost; state.usage_unavailable_count=summary.unavailable_calls_count; state.input_tokens=summary.input_tokens; state.output_tokens=summary.output_tokens; state.costs={'total':summary.total_cost,'input':summary.input_cost,'output':summary.output_cost}
            failure_kind=None
            break
        except Exception as e:
            last_error=e
            msg=str(e).lower()
            failure_kind='provider_error' if any(s in msg for s in ['http 400','provider','request rejected','payload too large']) else 'malformed'
            if idx < len(payloads):
                ctx.recorder.record('review_retrying', payload={'attempt_id':inp.attempt_id,'failed_payload':kind,'next_payload':payloads[idx][0],'review_error_type':failure_kind,'message':str(e)[:500]})
                continue
    if res is None:
        e=last_error or Exception('unknown structured review failure')
        res=OpsReviewResult(decision='fail',recommended_action='retry',score=0.0,summary='structured review unavailable after retries',evidence=[],issues=[f'{type(e).__name__}: {e}'],blockers=['review_infrastructure_failed','review_malformed'],confidence=0.0)
        rdir=Path(state.run_dir)/'reviews'; rdir.mkdir(parents=True,exist_ok=True)
        raw_path=rdir/f'{inp.attempt_id}_malformed_review.json'
        write_json_utf8(raw_path, {'raw_response':raw,'error':f'{type(e).__name__}: {e}'})
        if isinstance(a, dict): a.setdefault('review_artifacts',[]).append(str(raw_path))
        else:
            a.acceptance_blockers=sorted(set(a.acceptance_blockers+['review_infrastructure_failed','review_malformed']))
            a.review_status=failure_kind or 'unavailable'; a.review_error_type=type(e).__name__; a.review_error_message=str(e); a.review_retry_count=len(payloads)
    if isinstance(a, dict):
        a['review']=res.model_dump(); a['status']='reviewed' if a.get('status') not in {'failed','completed'} else a.get('status')
        eligible, blockers=_set_acceptance_from_gate(state, a)
        if res.blockers:
            blockers=sorted(set(blockers+res.blockers)); a['acceptance_blockers']=blockers; eligible=False; a['acceptance_eligible']=False
    else:
        a.review=res.model_dump(); a.status='reviewed' if a.status!='failed' else 'rejected'
        if res.decision=='pass' and res.recommended_action=='accept' and not res.blockers:
            a.review_status='passed'
        elif 'review_infrastructure_failed' in res.blockers:
            a.review_status=failure_kind or 'unavailable'
        else:
            a.review_status='failed'
        a.review_retry_count=max(a.review_retry_count, (len(payloads) if 'review_infrastructure_failed' in res.blockers else 1))
        evidence_text=' '.join(str(x) for x in [res.summary, res.evidence, res.issues, _read_text_tail(a.stdout_path), _read_text_tail(a.stderr_path)])
        if inp.scope=='subtask' and 'impossible_in_isolation' in evidence_text:
            a.acceptance_blockers=sorted(set(a.acceptance_blockers+['subtask_impossible_in_isolation']))
            res.blockers=sorted(set(res.blockers+['subtask_impossible_in_isolation']))
        eligible, blockers=_set_acceptance_from_gate(state, a)
        if res.blockers:
            blockers=sorted(set(blockers+res.blockers)); a.acceptance_blockers=blockers; eligible=False; a.acceptance_eligible=False
        if st and eligible:
            eligible, blockers, _app=_commit_subtask_acceptance(state, st, a, ctx, reason='review_accept_commit')
    state.reviews.append({'attempt_id':inp.attempt_id,**res.model_dump(),'acceptance_eligible':eligible,'acceptance_blockers':blockers})
    if not eligible and blockers:
        state.blockers=sorted(set(state.blockers+blockers))
    ctx.recorder.record(f'{inp.scope}_attempt_reviewed', payload={'attempt_id':inp.attempt_id,'review_decision':res.decision,'review_recommended_action':res.recommended_action,'central_acceptance_eligible':eligible,'acceptance_eligible':eligible,'acceptance_blockers':blockers,'execution_path':state.execution_path,'validation_blocked':any(b.startswith('validation_') for b in blockers),'artifact_blocked':any(b in {'missing_patch','empty_changed_files','patch_unreadable'} for b in blockers), **(({k:getattr(usage_record,k) for k in ['input_tokens','output_tokens','total_tokens','total_cost','usage_source']} if 'usage_record' in locals() and usage_record is not None else {}))})
    if inp.scope in {'candidate','subtask'} and any(o.attempt_id==inp.attempt_id for o in state.attempt_observations) and not isinstance(a,dict):
        _observe_completed_attempt(state,a,ctx)
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
    result['validation_source']='ops_run_validation'
    if target=='candidate' and target_obj is not None:
        target_obj.validation=result; target_obj.validation_status=status; target_obj.validation_source='ops_run_validation'; target_obj.validation_results=list(getattr(target_obj,'validation_results',[]) or [])+[result]
        _set_acceptance_from_gate(state,target_obj)
    elif target=='integration' and isinstance(target_obj,dict):
        target_obj['validation']=result; target_obj['validation_status']=status; target_obj['validation_results']=(target_obj.get('validation_results') or [])+[result]
        _set_acceptance_from_gate(state,target_obj)
    elif target=='repo':
        vals=list(getattr(state,'repo_validation_results',[]) or []); vals.append(result); state.repo_validation_results=vals

def _default_validation_metadata(command, *, target, target_obj, subtask):
    scope=getattr(command,'scope',None)
    source=getattr(command,'source',None)
    source_was_explicit=source is not None
    authority=getattr(command,'authority',None)
    subtask_id=getattr(command,'subtask_id',None)
    attempt_scope=getattr(target_obj,'scope',None) if target_obj is not None and not isinstance(target_obj,dict) else (target_obj or {}).get('scope') if isinstance(target_obj,dict) else None
    if scope is None:
        scope='subtask' if attempt_scope=='subtask' else ('integration' if target=='integration' else ('repo' if target=='repo' else 'candidate'))
    if subtask_id is None and subtask is not None:
        subtask_id=subtask.subtask_id
    if source is None:
        if scope=='subtask':
            source='subtask_focused'
        elif scope=='integration':
            source='integration'
        else:
            source='user_success_criteria'
    if authority is None:
        # Authority must come from an explicit validation plan/command contract.
        # Discovered or merely related commands are non-blocking by default.
        authority='diagnostic_only' if source in {'diagnostic','exploratory','runner_trace','villani_code_debug_trace'} else 'supporting_evidence'
        if not source_was_explicit and target in {'candidate','integration'}:
            authority='acceptance_blocking'
    return source, authority, scope, subtask_id

def make_validation_decision(result:dict)->dict:
    commands=[c for c in (result or {}).get('commands') or [] if isinstance(c,dict)]
    scope=(result or {}).get('scope') or ((commands[0] or {}).get('scope') if commands else None) or ('integration' if (result or {}).get('target')=='integration' else ('repo' if (result or {}).get('target')=='repo' else 'candidate'))
    subtask_id=(result or {}).get('subtask_id') or next((c.get('subtask_id') for c in commands if c.get('subtask_id')), None)
    blocking=[c for c in commands if c.get('authority')=='acceptance_blocking']
    supporting=[c for c in commands if c.get('authority')=='supporting_evidence']
    diagnostic=[c for c in commands if c.get('authority')=='diagnostic_only']
    blocking_fail=[c for c in blocking if c.get('passed') is not True]
    passed_block=[c for c in blocking if c.get('passed') is True]
    supporting_fail=[c for c in supporting if c.get('passed') is not True]
    diagnostic_fail=[c for c in diagnostic if c.get('passed') is not True]
    passed_support=[c for c in supporting if c.get('passed') is True]
    if blocking:
        status='failed' if blocking_fail else 'passed'
        rationale='acceptance-blocking validation failed' if blocking_fail else 'all acceptance-blocking validation passed'
    elif passed_support and not supporting_fail:
        status='inconclusive'
        rationale='supporting validation passed but no acceptance-blocking validation was available'
    else:
        status='inconclusive'
        rationale='no acceptance-blocking validation was available; diagnostic/supporting failures are non-blocking evidence'
    return ValidationDecision(status=status,scope=scope,subtask_id=subtask_id,blocking_failures=blocking_fail,supporting_failures=supporting_fail,diagnostic_failures=diagnostic_fail,passed_blocking_checks=passed_block,passed_supporting_checks=passed_support,rationale=rationale).model_dump(mode='json')

def h_validation(state, inp, ctx):
    target_obj, base_cwd, _st = _resolve_validation_target(state, inp)
    results=[]; all_pass=True; overall_status='passed'; first_cwd=None
    outdir=Path(state.run_dir)/'validation'; outdir.mkdir(exist_ok=True)
    label=_target_label(inp.target, inp.target_id)
    for i,c in enumerate(inp.commands,1):
        source, authority, scope, subtask_id=_default_validation_metadata(c, target=inp.target, target_obj=target_obj, subtask=_st)
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
            item={'cmd':c.cmd,'passed':False,'status':'command_rejected','reason':getattr(c,'reason',None) or reason,'error':str(e),'cwd':str(base_cwd.resolve()),'source':source,'authority':authority,'scope':scope,'subtask_id':subtask_id,'purpose':c.purpose or ''}
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
        item={'cmd':c.cmd,'passed':passed,'status':status,'cwd':str(cmd_cwd),'stdout_path':str(so),'stderr_path':str(se),'source':source,'authority':authority,'scope':scope,'subtask_id':subtask_id,'purpose':c.purpose or '','reason':getattr(c,'reason',None) or ''}; results.append(item)
        ctx.recorder.record('validation_completed' if passed else 'validation_failed', payload={'target':inp.target,'target_id':inp.target_id,'passed':passed,'command_count':len(inp.commands),'cwd':str(cmd_cwd),'artifact_paths':{'stdout':str(so),'stderr':str(se)},'validation_result':item})
    res={'raw_passed':all_pass,'raw_status':overall_status if not all_pass else 'passed','passed':all_pass,'status':overall_status if not all_pass else 'passed','commands':results,'target':inp.target,'target_id':inp.target_id,'cwd':first_cwd or str(base_cwd.resolve())}
    decision=make_validation_decision(res); res['decision']=decision; res['decision_status']=decision['status']; res['scope']=decision['scope']; res['subtask_id']=decision.get('subtask_id')
    if decision['status']=='passed':
        res['passed']=True; res['status']='passed'
    elif decision['status']=='failed':
        res['passed']=False
        if res['status'] not in {'command_rejected','timed_out','error'}:
            res['status']='failed'
    else:
        res['passed']=False; res['status']='inconclusive'
    _attach_validation(state,target_obj,inp.target,res)
    ctx.recorder.record('validation_attached', payload={'target':inp.target,'target_id':inp.target_id,'passed':all_pass,'status':res['status'],'command_count':len(inp.commands),'cwd':res['cwd'],'artifact_paths':[p for r in results for p in [r.get('stdout_path'),r.get('stderr_path')] if p]})
    if inp.target=='candidate' and target_obj is not None and any(o.attempt_id==inp.target_id for o in state.attempt_observations):
        _observe_completed_attempt(state,target_obj,ctx)
    return res

def h_select_winner(state, inp, ctx):
    if inp.decision=='reject_all':
        if not inp.reasons: raise ValueError('reject_all requires reasons')
        if state.selection and state.selection.get('decision')=='reject_all':
            state.phase='finalizing'; return state.selection
        state.selection=inp.model_dump(); state.phase='finalizing'; ctx.recorder.record('selection_completed', payload=state.selection); return state.selection
    if not inp.selected_attempt_id: raise ValueError('selected_attempt_id is required')
    a, st=_find_attempt(state, inp.selected_attempt_id)
    if not a: raise ValueError(f'selected attempt {inp.selected_attempt_id} does not exist')
    if state.execution_path=='decomposed_subtasks' and state.fallback_execution_path!='parallel_candidates_after_decomposition_deadlock':
        if st is not None: raise ValueError('cannot select raw subtask attempt as final winner')
        if not isinstance(a,dict) or inp.selected_attempt_id!='integration_001': raise ValueError('decomposed final selection requires integration result')
    if (state.execution_path in {'parallel_candidates','candidate_tournament'} or state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock') and (isinstance(a,dict) or getattr(a,'scope',None)!='candidate'):
        raise ValueError('candidate path selection requires candidate attempt')
    status=a.get('status') if isinstance(a,dict) else a.status
    if status=='running': raise ValueError('cannot select running attempt')
    eligible, blockers=is_attempt_acceptance_eligible(a, state=state)
    if state.execution_path=='candidate_tournament' and state.tournament_ranking and inp.selected_attempt_id==state.tournament_ranking.selected_candidate_id:
        eligible=True; blockers=[]
    stored=a.get('acceptance_eligible') if isinstance(a,dict) else a.acceptance_eligible
    if isinstance(a,dict):
        a['acceptance_eligible']=eligible; a['acceptance_blockers']=blockers
    else:
        a.acceptance_eligible=eligible; a.acceptance_blockers=blockers
    if not eligible:
        state.blockers=sorted(set(state.blockers+blockers))
        ctx.recorder.record('selection_rejected', payload={'selected_attempt_id':inp.selected_attempt_id,'stored_acceptance_eligible':stored,'recomputed_acceptance_eligible':eligible,'acceptance_blockers':blockers})
        raise ValueError('selected attempt is not acceptance eligible: '+', '.join(blockers))
    new_selection={**inp.model_dump(),'selection_evidence':{'stored_acceptance_eligible':stored,'recomputed_acceptance_eligible':eligible,'acceptance_blockers':blockers}}
    if state.selection and state.selection.get('decision')=='select' and state.selection.get('selected_attempt_id')==inp.selected_attempt_id:
        state.phase='finalizing'; return {**state.selection, 'already_selected': True}
    state.selection=new_selection
    state.phase='finalizing'; ctx.recorder.record('selection_completed', payload=state.selection); return state.selection

def validate_final_state_consistency(state) -> list[str]:
    warnings=[]
    fd=state.final_decision or {}
    if not state.is_terminal(): warnings.append('state_status_not_terminal')
    if state.phase not in {'completed','failed'}: warnings.append('state_phase_not_terminal')
    if fd.get('decision') not in {'accepted','rejected','failed'}: warnings.append('final_decision_missing_or_invalid')
    if fd.get('decision')=='accepted':
        sel=state.selection or {}
        if sel.get('decision')!='select' or not sel.get('selected_attempt_id'): warnings.append('accepted_without_selection')
        if not fd.get('selected_patch_path'): warnings.append('accepted_without_selected_patch_path')
    return warnings

def h_finalize(state, inp, ctx):
    if inp.decision=='accepted' and state.execution_path=='candidate_tournament' and not state.selection:
        commit_tournament_selection(state, ctx)
    if state.is_terminal():
        return {**(state.final_decision or {}), 'already_finalized': True}
    if inp.decision!='accepted':
        pending=[]
        for c in state.candidates:
            if c.status=='completed' and c.patch_path and c.changed_files and not c.review:
                pending.append(f'{c.attempt_id}:candidate completed but review missing')
            if c.review and (c.validation is None) and ((c.review or {}).get('decision')=='pass'):
                pending.append(f'{c.attempt_id}:candidate reviewed but validation missing')
            eligible, _bs=is_attempt_acceptance_eligible(c,state=state)
            if eligible:
                pending.append(f'{c.attempt_id}:candidate is acceptance eligible')
        if pending and not (inp.blockers and any('fatal' in b for b in inp.blockers)):
            raise ValueError('cannot finalize failed while candidates remain reviewable/validatable: '+', '.join(pending))
    final_payload=inp.model_dump()
    if inp.decision!='accepted' and inp.selected_attempt_id:
        a0, st0=_find_attempt(state, inp.selected_attempt_id)
        final_payload['selected_attempt_id']=None
        if st0 is not None:
            final_payload['best_partial_attempt_id']=inp.selected_attempt_id
        elif a0 is not None:
            final_payload['best_candidate_attempt_id']=inp.selected_attempt_id
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
        if state.execution_path=='candidate_tournament' and state.tournament_ranking and aid==state.tournament_ranking.selected_candidate_id:
            eligible=True; blockers=[]
            final_payload['selection_basis']=state.selection_basis
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
        patch_path=a.get('patch_path') if isinstance(a,dict) else a.patch_path
        if not final_payload.get('selected_patch_path') and patch_path:
            if not Path(patch_path).exists(): raise ValueError('selected patch path does not exist')
            final_payload['selected_patch_path']=patch_path
        # Evidence-based summary prefix prevents over-claiming files not in the selected patch.
        changed=a.get('changed_files') if isinstance(a,dict) else a.changed_files
        val=a.get('validation') if isinstance(a,dict) else a.validation
        final_payload['summary']=f"Selected {aid} changed {', '.join(changed or []) or 'no files'}; current validation {((val or {}).get('status') or 'not_run')}." + ((' '+final_payload.get('summary','')) if final_payload.get('summary') else '')
    state.final_decision=final_payload; state.status='completed' if inp.decision=='accepted' else 'failed'; state.phase='completed' if state.status=='completed' else 'failed';
    consistency_warnings=validate_final_state_consistency(state)
    if consistency_warnings:
        state.warnings=sorted(set(state.warnings+consistency_warnings)); state.final_decision['consistency_warnings']=consistency_warnings
    ctx.recorder.record('run_finalized', payload=state.final_decision); return state.final_decision
def build_integration_repair_prompt(state, reason, base_attempt_id=None):
    accepted=[]; files=[]
    for st in state.subtasks:
        if st.status=='accepted':
            a=_accepted_subtask_attempt(state, st)
            fs=(a.changed_files if a else []) or []
            files += fs
            accepted.append({'subtask_id':st.subtask_id,'attempt_id':st.accepted_attempt_id,'changed_files':fs})
    failed=[o.model_dump(mode='json') for o in state.attempt_observations if o.scope=='subtask' and o.outcome!='accepted'][-6:]
    val=(state.integration or {}).get('validation') or {}
    return '\n\n'.join([
        f'TASK\n{state.task}',
        f'SUCCESS CRITERIA\n{state.success_criteria or "Complete the task with a minimal correct patch."}',
        'INTEGRATION REPAIR CONTEXT\nRun exactly one integration repair candidate. Preserve accepted subtask behavior and fix only remaining integration/full-validation blockers.',
        'ACCEPTED SUBTASKS\n'+json.dumps(accepted, ensure_ascii=False, indent=2),
        'FILES CHANGED BY ACCEPTED SUBTASKS\n'+'\n'.join(f'- {f}' for f in sorted(set(files))),
        'FAILED OR BLOCKED SUBTASK EVIDENCE\n'+json.dumps(failed, ensure_ascii=False, indent=2),
        'FULL VALIDATION FAILURE SUMMARY\n'+json.dumps(val, ensure_ascii=False, indent=2)[:4000],
        'REPAIR DIRECTIVES\n- Do not regress accepted subtasks.\n- Focus on integration blockers and failing full-validation commands.\n- Do not edit unrelated files or tests unless the original task explicitly requires it.',
        f'REASON\n{reason}' + ((f'\nBase failed integration attempt: {base_attempt_id}') if base_attempt_id else '')
    ])

def h_run_next_integration_repair_attempt(state, inp, ctx):
    if state.execution_path!='decomposed_subtasks': raise ValueError('integration repair requires decomposed_subtasks')
    if not state.integration: raise ValueError('integration repair requires an integration result')
    if (state.integration.get('validation') or {}).get('passed') is not False and state.integration.get('status')!='failed':
        raise ValueError('integration repair requires failed integration validation or failed integration')
    rid=f"integration_repair_{len([o for o in state.attempt_observations if o.scope=='integration'])+1:03d}"
    prompt=build_integration_repair_prompt(state, inp.reason, inp.base_attempt_id)
    a=_run_attempt(state,ctx,rid,'integration',prompt,state.success_criteria,None,inp.backend_name,True)
    state.candidates.append(a)
    cmds=_validation_plan_commands(state)
    if a.status=='completed' and cmds:
        h_validation(state, OpsRunValidationInput(target='candidate', target_id=rid, commands=cmds), ctx)
    if ctx.reviewer is not None and a.status in {'completed','reviewed'} and not a.review:
        h_review_attempt(state, OpsReviewAttemptInput(attempt_id=rid, scope='integration'), ctx)
    obs=_observe_completed_attempt(state,a,ctx)
    state.integration.setdefault('repair_attempts',[]).append(a.model_dump())
    state.integration['repair_used']=True
    state.integration['latest_repair_attempt_id']=rid
    return {'attempt_id':rid,'observation':obs.model_dump(),'next_allowed_actions':state.allowed_next_actions()}

OPS_TOOLS={
'ops_get_state':ToolSpec('ops_get_state','Inspect canonical run state',OpsGetStateInput,h_get_state,True),
'ops_inspect_repo':ToolSpec('ops_inspect_repo','Inspect repository',OpsInspectRepoInput,h_inspect_repo,True),
'ops_submit_classification':ToolSpec('ops_submit_classification','Submit classification',OpsSubmitClassificationInput,h_classification),
'ops_submit_investigation':ToolSpec('ops_submit_investigation','Submit investigation',OpsSubmitInvestigationInput,h_investigation),
'ops_submit_plan':ToolSpec('ops_submit_plan','Submit orchestration plan',OpsSubmitPlanInput,h_plan),
'ops_submit_decomposition':ToolSpec('ops_submit_decomposition','Submit decomposition',OpsSubmitDecompositionInput,h_decomposition),
'ops_validate_decomposition':ToolSpec('ops_validate_decomposition','Validate decomposition',OpsValidateDecompositionInput,h_validate_decomposition),
'ops_select_execution_path':ToolSpec('ops_select_execution_path','Select execution path',OpsSelectExecutionPathInput,h_select_path),
'ops_launch_candidates':ToolSpec('ops_launch_candidates','Launch full-task candidates in parallel/batches. Legacy batch fallback is disabled during adaptive decomposition-deadlock fallback unless legacy_ops_launch_candidates_enabled is explicitly set; use ops_run_next_fallback_candidate_attempt there. Never valid for single_task.',OpsLaunchCandidatesInput,h_launch_candidates),
'ops_launch_tournament_candidates':ToolSpec('ops_launch_tournament_candidates','Launch independent adaptive tournament candidates in parallel up to backend max_parallel, save candidates/evidence incrementally, then return before tournament evaluation.',OpsLaunchTournamentCandidatesInput,h_launch_tournament_candidates),
'ops_evaluate_tournament':ToolSpec('ops_evaluate_tournament','Review completed tournament candidates, compare/rank, and commit selection with time-boxed deterministic fallbacks.',OpsEvaluateTournamentInput,h_evaluate_tournament),
'ops_run_next_candidate_attempt':ToolSpec('ops_run_next_candidate_attempt','Run exactly one adaptive full-task candidate attempt, then validate/review/observe it automatically.',OpsRunNextCandidateAttemptInput,h_run_next_candidate_attempt),
'ops_run_next_fallback_candidate_attempt':ToolSpec('ops_run_next_fallback_candidate_attempt','Run exactly one adaptive full-task fallback candidate after decomposition deadlock, then validate/review/observe it automatically.',OpsRunNextFallbackCandidateAttemptInput,h_run_next_fallback_candidate_attempt),
'ops_run_next_subtask_attempt':ToolSpec('ops_run_next_subtask_attempt','Run exactly one adaptive subtask attempt selected from current decomposition state, then focused-validate/review/observe it automatically.',OpsRunNextSubtaskAttemptInput,h_run_next_subtask_attempt),
'ops_run_next_integration_repair_attempt':ToolSpec('ops_run_next_integration_repair_attempt','Run exactly one adaptive integration repair attempt after accepted subtasks fail full validation.',OpsRunNextIntegrationRepairAttemptInput,h_run_next_integration_repair_attempt),
'ops_observe_completed_attempt':ToolSpec('ops_observe_completed_attempt','Internal recovery: create an AttemptObservation for an existing completed attempt before any retry.',OpsObserveCompletedAttemptInput,h_observe_completed_attempt),
'ops_run_single_task_attempts':ToolSpec('ops_run_single_task_attempts','LEGACY compatibility bulk sequential attempts. Hidden from normal agentic tool lists; do not use for adaptive orchestration. Use ops_run_next_candidate_attempt instead.',OpsRunSingleTaskAttemptsInput,h_run_single_task_attempts),
'ops_start_candidate_fallback':ToolSpec('ops_start_candidate_fallback','Start full-task candidate fallback after decomposition deadlock',OpsStartCandidateFallbackInput,h_start_candidate_fallback),
'ops_launch_subtasks':ToolSpec('ops_launch_subtasks','LEGACY/internal compatibility bulk subtask launcher. Hidden and policy-blocked in normal agentic flow; use ops_run_next_subtask_attempt.',OpsLaunchSubtasksInput,h_launch_subtasks),
'ops_review_attempt':ToolSpec('ops_review_attempt','Review attempt',OpsReviewAttemptInput,h_review_attempt),
'ops_integrate_subtasks':ToolSpec('ops_integrate_subtasks','Integrate subtasks',OpsIntegrateSubtasksInput,h_integrate),
'ops_run_validation':ToolSpec('ops_run_validation','Run validation commands in the selected target workspace automatically. Validation commands carry source, authority, and scope; only acceptance_blocking validation blocks acceptance. Diagnostic/exploratory failures are evidence, not blockers. Component subtasks use subtask-scoped evidence; global validation is reserved for integration/final acceptance. For candidate/integration targets, provide target_id and commands without cd/pushd/Set-Location; cwd defaults to the target worktree and relative cwd is resolved inside it. Keep commands cross-platform; do not use Unix-only utilities like head, tail, grep, sed, awk, cat, rm -rf, or export. Prefer python -m pytest --tb=short -v or Python one-liners.',OpsRunValidationInput,h_validation),
'ops_select_winner':ToolSpec('ops_select_winner','Select winner',OpsSelectWinnerInput,h_select_winner),
'ops_finalize_run':ToolSpec('ops_finalize_run','Finalize run',OpsFinalizeRunInput,h_finalize),
}
def openai_tool_specs(adaptive:bool=False):
    hidden={'ops_run_single_task_attempts','ops_observe_completed_attempt','ops_launch_subtasks'}
    if adaptive:
        hidden |= {'ops_submit_decomposition','ops_validate_decomposition','ops_launch_candidates','ops_run_next_fallback_candidate_attempt','ops_run_next_subtask_attempt','ops_run_next_integration_repair_attempt','ops_start_candidate_fallback','ops_integrate_subtasks'}
        hidden |= {'ops_run_next_candidate_attempt'}
    return [{'type':'function','function':{'name':n,'description':s.description,'parameters':s.input_model.model_json_schema(),'strict':True}} for n,s in OPS_TOOLS.items() if n not in hidden]
