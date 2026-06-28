from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
from .state import CandidateAttemptState, SubtaskState, AttemptObservation, detect_decomposition_deadlock
from .git_artifacts import capture_git_patch, ensure_git_baseline, clean_runner_artifacts_from_worktree, DEFAULT_PATCH_EXCLUDES, is_git_compatible_patch, patch_contains_internal_artifacts, clean_untracked_scratch_artifacts, is_scratch_artifact_path
from villani_ops.core.acceptance import is_attempt_acceptance_eligible, attempt_requires_patch, validation_evidence_strength, validation_is_reliable, normalized_review_metrics, candidate_ranking_evidence, explain_candidate_selection, candidate_ranking_key, is_usable_unverified_candidate, usable_unverified_candidates, best_unverified_candidate
import subprocess, json, time, shutil, os, re
from villani_ops.agentic.validation import classify_validation_command, run_classified_validation, skipped_validation_result
from datetime import datetime, timezone
from .artifacts import read_text_utf8, write_text_utf8, write_json_utf8
from concurrent.futures import ThreadPoolExecutor, as_completed
from villani_ops.telemetry.usage import usage_record_from_runner, usage_record_from_response

_UNVERIFIED_FATAL_BLOCKERS={'runner_failed','runner_exception','missing_patch','empty_changed_files','internal_artifacts_only','scratch_artifact_in_patch','patch_hygiene_failed','patch_contains_internal_artifacts','invalid_patch_format','patch_apply_check_failed','scope_failed','review_infrastructure_failed'}

def _is_unverified_candidate_usable(state, attempt):
    return is_usable_unverified_candidate(state, attempt)

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



def _validation_snapshot(attempt) -> str:
    data=_attempt_to_dict(attempt)
    validation=data.get('validation') or {}
    status=data.get('validation_status') or validation.get('status') or 'not_run'
    source=data.get('validation_source') or validation.get('validation_source') or validation.get('source')
    commands=validation.get('commands') or []
    command_sig=[]
    for c in commands:
        if isinstance(c, dict):
            command_sig.append({
                'cmd': c.get('cmd') or c.get('command'),
                'status': c.get('status'),
                'exit_code': c.get('exit_code'),
                'blocking': c.get('blocking'),
                'authority': c.get('authority'),
                'source': c.get('source'),
                'evidence_strength': c.get('evidence_strength'),
            })
    return json.dumps({'status':status,'source':source,'decision_status':validation.get('decision_status') or (validation.get('decision') or {}).get('status'),'commands':command_sig}, sort_keys=True, default=str)

def _invalidate_stale_review_after_validation(state, attempt, ctx=None, *, reason='validation_changed_after_review'):
    if isinstance(attempt, dict):
        if not attempt.get('review'):
            return False
        current=_validation_snapshot(attempt)
        prior=attempt.get('review_validation_snapshot') or (attempt.get('review') or {}).get('validation_snapshot')
        blockers=(attempt.get('review') or {}).get('blockers') or []
        stale = (prior is not None and prior != current) or ('validation_missing' in blockers)
        if stale:
            attempt['stale_review']=attempt.get('review')
            attempt['review']=None; attempt['review_status']='not_run'; attempt['review_validation_snapshot']=None
            attempt['acceptance_eligible']=False
            attempt['acceptance_blockers']=sorted(set((attempt.get('acceptance_blockers') or [])+['review_invalidated_after_validation']))
        return stale
    if not getattr(attempt,'review',None):
        return False
    current=_validation_snapshot(attempt)
    prior=getattr(attempt,'review_validation_snapshot',None) or (attempt.review or {}).get('validation_snapshot')
    blockers=(attempt.review or {}).get('blockers') or []
    stale = (prior is not None and prior != current) or ('validation_missing' in blockers)
    if stale:
        attempt.review=None; attempt.review_status='not_run'; attempt.review_validation_snapshot=None; attempt.acceptance_eligible=False
        attempt.acceptance_blockers=sorted(set((attempt.acceptance_blockers or [])+['review_invalidated_after_validation']))
        if ctx is not None:
            ctx.recorder.record('review_invalidated_after_validation', payload={'attempt_id':attempt.attempt_id,'reason':reason,'validation_snapshot':current})
    return stale

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
        stdout_tail=_read_text_tail(item.get('stdout_path'), max_chars=4000)
        stderr_tail=_read_text_tail(item.get('stderr_path'), max_chars=4000)
        validation_tails.append({
            **item,
            'command_source': item.get('source'),
            'command_or_argv': item.get('argv') or item.get('cmd') or item.get('command'),
            'stdout_summary': stdout_tail,
            'stderr_summary': stderr_tail,
            'stdout_tail': stdout_tail,
            'stderr_tail': stderr_tail,
            'blocking_or_diagnostic': 'blocking' if item.get('blocking') or item.get('authority')=='acceptance_blocking' else 'diagnostic',
        })
    validation_metadata={
        'validation_status':data.get('validation_status') or validation.get('status') or 'not_run',
        'validation_source':data.get('validation_source') or validation.get('validation_source'),
        'validation_confidence':validation.get('confidence') or next((c.get('confidence') for c in validation_tails if c.get('confidence')), None),
        'validation_blocking':any(c.get('blocking') or c.get('authority')=='acceptance_blocking' for c in validation_tails),
        'validation_evidence_strength':validation_evidence_strength(validation),
        'validation_authoritative_or_diagnostic':'authoritative' if validation_is_reliable(validation) else 'diagnostic',
        'validation_blocking_or_diagnostic':'blocking' if any(c.get('blocking') or c.get('authority')=='acceptance_blocking' for c in validation_tails) else 'diagnostic',
        'commands':validation_tails,
        'decision':validation.get('decision') or {},
        'decision_status':validation.get('decision_status') or (validation.get('decision') or {}).get('status'),
        'infrastructure_error_reason':validation.get('infrastructure_error') or next((c.get('infrastructure_error') for c in validation_tails if c.get('infrastructure_error')), None),
    }
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
        'validation':{**current_validation, **validation_metadata, 'authoritative': validation_is_reliable(validation)},
        'validation_decision':(current_validation or {}).get('decision') or {},
        'current_validation':{**current_validation, **validation_metadata, 'source':'ops_run_validation'} if current_validation else validation_metadata,
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
    return cmds

def build_subtask_runner_prompt(*, parent_task:str, parent_success_criteria:str|None, subtask:SubtaskState, allowed_files:list[str], forbidden_files:list[str]|None, validation_commands:list[ValidationCommand]|list[str], dependency_context:str|None, merge_contract:str|None)->str:
    cmds=[c.cmd if hasattr(c,'cmd') else str(c) for c in (validation_commands or [])]
    allowed='\n'.join(f'- {f}' for f in allowed_files) if allowed_files else 'Allowed files were not confidently identified. Make the smallest possible change and explain changed files.'
    forbidden='\n'.join(f'- {f}' for f in (forbidden_files or ['.villani','.villani_code']))
    return ("You are executing ONE Villani Ops subtask, not the whole parent task.\n\n"
        "Your job is to complete only the subtask below.\nDo not solve unrelated parts of the parent task.\nDo not broaden scope unless the subtask is impossible without a minimal cross-file change.\n\n"
        f"PARENT TASK CONTEXT\nThis is background only. Do not solve the whole parent task unless required by the subtask.\n{parent_task}\n\n"
        "Parent success criteria are provided only so you understand the larger system. Your acceptance is based on the subtask objective and subtask validation, not solving the entire parent task.\n"
        f"{parent_success_criteria or ''}\n\nSUBTASK OBJECTIVE\n{subtask.title}\n{subtask.objective}\n\nSUBTASK SUCCESS CRITERIA\n{subtask.success_criteria or subtask.objective}\n\n"
        f"ALLOWED FILES\n{allowed}\n\nFORBIDDEN FILES / ARTIFACTS\n{forbidden}\nDo not create helper scripts, scratch files, logs, checkpoints, or temporary fix files in the repo.\nDo not modify or create Villani internal directories such as .villani or .villani_code.\nOnly product code and necessary tests should change.\n\n"
        f"DEPENDENCY CONTEXT\n{dependency_context or 'No accepted dependency context was provided.'}\n\nMERGE CONTRACT\n{merge_contract or 'Keep changes minimal and merge-friendly.'}\n\n"
        "EXPECTED VALIDATION\nRun the narrowest relevant tests for this subtask first. Then, if cheap enough, run broader parent validation.\nValidation commands have authority levels: only acceptance-blocking validation should block acceptance; diagnostic and exploratory failures are evidence, not blockers. Component subtasks are accepted on subtask-scoped evidence. Global validation is reserved for integration/final acceptance.\nSuggested commands (empty means no reliable command was detected; do not invent a generic language-specific fallback):\n" + ('\n'.join(f'- {c}' for c in cmds) if cmds else '- none') +
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
    source:Literal['user_provided','project_detected','generated','user_success_criteria','investigation_discovered','subtask_focused','runner_suggested','diagnostic','exploratory','integration','final']|None=None
    confidence:Literal['high','medium','low']|None=None
    blocking:bool|None=None
    shell:bool=False
    argv:list[str]|None=None
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
class OpsSelectExecutionPathInput(StrictModel): path:Literal['single_task','parallel_candidates','decomposed_subtasks']; reason:str
class OpsLaunchCandidatesInput(StrictModel): attempts:int; backend_name:str|None=None; reason:str
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
        invalid=bool(inp.should_decompose or inp.strategy!='single_task')
        if invalid:
            _adaptive_warning(state, ctx, 'adaptive_orchestrator_forced_single_task_plan', {'original_plan': plan})
        plan.update({'strategy':'single_task','should_decompose':False,'decomposition_reason':None,'candidate_attempts':state.candidate_attempts})
        plan['execution_path']='single_task'
        plan['plan_kind']='single_task'
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
    if _is_adaptive(state) and inp.path!='single_task':
        _adaptive_warning(state, ctx, 'adaptive_orchestrator_forced_single_task_execution_path', {'requested_path': inp.path, 'reason': inp.reason})
        state.execution_path='single_task'; state.phase='running_candidates'; state.candidate_execution_mode='sequential'; state.decomposition_executed=False; state.subtasks=[]; state.decomposition=None
        return {'execution_path':state.execution_path,'reason':'adaptive forced single_task','decomposition_fallback_used':False,'warning':'adaptive_orchestrator_forced_single_task_execution_path'}
    strategy=(state.plan or {}).get('strategy')
    if strategy=='single_task' and inp.path=='parallel_candidates':
        raise ValueError('plan strategy is single_task; use execution_path=single_task for sequential attempts, not parallel_candidates')
    if strategy=='parallel_candidates' and inp.path=='single_task':
        raise ValueError('plan strategy is parallel_candidates; use execution_path=parallel_candidates')
    state.execution_path=inp.path
    state.phase='running_subtasks' if inp.path=='decomposed_subtasks' else 'running_candidates'
    state.candidate_execution_mode='sequential' if inp.path=='single_task' else ('parallel' if inp.path=='parallel_candidates' else state.candidate_execution_mode)
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
    return out

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

def _commands_for_attempt_scope(state, attempt, st=None):
    if getattr(attempt,'scope',None)=='subtask' and st is not None:
        return _focused_subtask_validation_commands(state, st)
    if getattr(attempt,'scope',None)=='integration':
        return _validation_plan_commands(state)
    return _default_validation_commands(state)

def _validate_review_observe_attempt(state, attempt, ctx, *, target_id=None, review_scope='candidate', st=None, commands=None):
    aid=target_id or getattr(attempt,'attempt_id',None)
    if getattr(attempt,'status',None)=='completed' and not getattr(attempt,'validation',None):
        cmds=_commands_for_attempt_scope(state, attempt, st) if commands is None else commands
        h_validation(state, OpsRunValidationInput(target='candidate', target_id=aid, commands=cmds or []), ctx)
    if ctx.reviewer is not None and getattr(attempt,'status',None) in {'completed','reviewed'} and not getattr(attempt,'review',None):
        h_review_attempt(state, OpsReviewAttemptInput(attempt_id=aid, scope=review_scope), ctx)
    eligible, blockers=_set_acceptance_from_gate(state,attempt)
    obs=_observe_completed_attempt(state,attempt,ctx)
    return eligible, blockers, obs

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
        eligible, blockers, obs=_validate_review_observe_attempt(state,a,ctx,target_id=aid,review_scope='candidate')
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
    eligible, blockers, obs=_validate_review_observe_attempt(state,a,ctx,target_id=aid,review_scope='candidate')
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
    eligible, blockers, obs=_validate_review_observe_attempt(state,a,ctx,target_id=aid,review_scope='candidate')
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
    cmds=_focused_subtask_validation_commands(state, st) if a.status=='completed' else []
    eligible, blockers, obs=_validate_review_observe_attempt(state,a,ctx,target_id=aid,review_scope='subtask',st=st,commands=cmds)
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
                    if a and not isinstance(a,dict) and a.status=='completed':
                        _validate_review_observe_attempt(state,a,ctx,target_id=aid,review_scope='subtask',st=st,commands=_focused_subtask_validation_commands(state, st))
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
    review_validation_snapshot=_validation_snapshot(a)
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
    if res is not None and (getattr(a,'validation_status',None) or (_attempt_to_dict(a).get('validation') or {}).get('status')) not in {None,'not_run'}:
        if 'validation_missing' in (res.blockers or []):
            res.blockers=[b for b in res.blockers if b!='validation_missing']
            if res.decision=='fail' and not res.blockers and res.recommended_action in {'accept','retry'}:
                # The reviewer saw current validation in this invocation; never persist stale validation_missing.
                res.summary=(res.summary or '') + ' (Removed stale validation_missing blocker because validation was attached before review.)'
    vmeta=(payload.get('current_validation') or payload.get('validation') or {})
    if res is not None and res.decision=='pass' and res.recommended_action=='accept' and not validation_is_reliable(vmeta):
        strength=validation_evidence_strength(vmeta)
        res.confidence=min(float(res.confidence or 0.0), 0.69)
        if strength in {'generated_smoke','diagnostic_only','generated_behavioral','skipped'}:
            res.summary=(res.summary or '') + f' Validation evidence is {strength}; candidate may be plausible but is not verified.'
            res.evidence=list(res.evidence or [])+[f'validation evidence strength: {strength}']
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
        a['review']={**res.model_dump(), **normalized_review_metrics(res.model_dump()), 'validation_snapshot': review_validation_snapshot}; a['review_validation_snapshot']=review_validation_snapshot; a['status']='reviewed' if a.get('status') not in {'failed','completed'} else a.get('status')
        eligible, blockers=_set_acceptance_from_gate(state, a)
        if res.blockers:
            blockers=sorted(set(blockers+res.blockers)); a['acceptance_blockers']=blockers; eligible=False; a['acceptance_eligible']=False
    else:
        a.review={**res.model_dump(), **normalized_review_metrics(res.model_dump()), 'validation_snapshot': review_validation_snapshot}; a.review_validation_snapshot=review_validation_snapshot; a.status='reviewed' if a.status!='failed' else 'rejected'
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
    state.reviews.append({'attempt_id':inp.attempt_id,**res.model_dump(),'acceptance_eligible':eligible,'acceptance_blockers':blockers,'validation_evidence_strength':validation_evidence_strength(_attempt_to_dict(a).get('validation') or {})})
    if not eligible and blockers:
        state.blockers=sorted(set(state.blockers+blockers))
    ctx.recorder.record(f'{inp.scope}_attempt_reviewed', payload={'attempt_id':inp.attempt_id,'review_decision':res.decision,'review_recommended_action':res.recommended_action,'central_acceptance_eligible':eligible,'acceptance_eligible':eligible,'acceptance_blockers':blockers,'execution_path':state.execution_path,'validation_blocked':any(b.startswith('validation_') for b in blockers),'artifact_blocked':any(b in {'missing_patch','empty_changed_files','patch_unreadable'} for b in blockers), **(({k:getattr(usage_record,k) for k in ['input_tokens','output_tokens','total_tokens','total_cost','usage_source']} if 'usage_record' in locals() and usage_record is not None else {}))})
    if inp.scope in {'candidate','subtask'} and any(o.attempt_id==inp.attempt_id for o in state.attempt_observations) and not isinstance(a,dict):
        _observe_completed_attempt(state,a,ctx)
    return {**res.model_dump(),'acceptance_eligible':eligible,'acceptance_blockers':blockers,'validation_evidence_strength':validation_evidence_strength(_attempt_to_dict(a).get('validation') or {}),'review_payload_included':['patch_excerpt','stdout_tail','stderr_tail','validation','validation_evidence_strength']}
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

def _attach_validation(state, target_obj, target, result, ctx=None):
    status=result.get('status') or ('passed' if result.get('passed') else 'failed')
    result['validation_source']='ops_run_validation'
    if target=='candidate' and target_obj is not None:
        target_obj.validation=result; target_obj.validation_status=status; target_obj.validation_source='ops_run_validation'; target_obj.validation_results=list(getattr(target_obj,'validation_results',[]) or [])+[result]
        invalidated=_invalidate_stale_review_after_validation(state,target_obj,ctx,reason='candidate_validation_attached_after_review')
        _set_acceptance_from_gate(state,target_obj)
    elif target=='integration' and isinstance(target_obj,dict):
        target_obj['validation']=result; target_obj['validation_status']=status; target_obj['validation_results']=(target_obj.get('validation_results') or [])+[result]
        invalidated=_invalidate_stale_review_after_validation(state,target_obj,ctx,reason='validation_attached_after_review')
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
            source='generated'
        elif scope=='integration':
            source='generated'
        else:
            source='generated'
    if authority is None:
        # Authority must come from an explicit validation plan/command contract.
        # Discovered or merely related commands are non-blocking by default.
        authority='diagnostic_only' if source in {'diagnostic','exploratory','runner_trace','villani_code_debug_trace'} else 'supporting_evidence'
        if not source_was_explicit and target in {'candidate','integration'}:
            authority='supporting_evidence'
    return source, authority, scope, subtask_id

def _command_evidence_strength(*, source, authority, confidence, blocking, status=None):
    if status in {'infrastructure_error','command_rejected','timeout','timed_out','error'}:
        return 'infrastructure_error'
    if source in {'user_provided','user_success_criteria','integration','final'}:
        return 'explicit_user_command'
    if source in {'project_detected','investigation_discovered','subtask_focused'} and confidence=='high' and (blocking or authority=='acceptance_blocking'):
        return 'high_confidence_project_detected'
    if source in {'project_detected','investigation_discovered','subtask_focused'} and (blocking or authority=='acceptance_blocking'):
        return 'project_test'
    if source=='generated' and confidence=='high' and (blocking or authority=='acceptance_blocking'):
        return 'generated_behavioral'
    if source=='generated':
        return 'generated_smoke'
    if source in {'diagnostic','exploratory','runner_trace','villani_code_debug_trace'} or authority=='diagnostic_only':
        return 'diagnostic_only'
    if blocking or authority=='acceptance_blocking':
        return 'project_test'
    return 'diagnostic_only'

def make_validation_decision(result:dict)->dict:
    commands=[c for c in (result or {}).get('commands') or [] if isinstance(c,dict)]
    scope=(result or {}).get('scope') or ((commands[0] or {}).get('scope') if commands else None) or ('integration' if (result or {}).get('target')=='integration' else ('repo' if (result or {}).get('target')=='repo' else 'candidate'))
    subtask_id=(result or {}).get('subtask_id') or next((c.get('subtask_id') for c in commands if c.get('subtask_id')), None)
    blocking=[c for c in commands if c.get('authority')=='acceptance_blocking']
    supporting=[c for c in commands if c.get('authority')=='supporting_evidence']
    diagnostic=[c for c in commands if c.get('authority')=='diagnostic_only']
    blocking_fail=[c for c in blocking if c.get('passed') is not True and c.get('status') not in {'infrastructure_error','timeout'}]
    passed_block=[c for c in blocking if c.get('passed') is True]
    supporting_fail=[c for c in supporting if c.get('passed') is not True]
    diagnostic_fail=[c for c in diagnostic if c.get('passed') is not True]
    passed_support=[c for c in supporting if c.get('passed') is True]
    if blocking:
        status='failed' if blocking_fail else ('passed' if passed_block else 'inconclusive')
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
    outdir=Path(state.run_dir)/'validation'; outdir.mkdir(exist_ok=True)
    label=_target_label(inp.target, inp.target_id)
    if not inp.commands:
        res=skipped_validation_result(target=inp.target, target_id=inp.target_id, cwd=base_cwd.resolve())
        res['evidence_strength']='skipped'; res['authoritative']=False
        decision=make_validation_decision(res); res['decision']=decision; res['decision_status']=decision['status']; res['scope']=decision['scope']; res['subtask_id']=decision.get('subtask_id')
        _attach_validation(state,target_obj,inp.target,res,ctx)
        ctx.recorder.record('validation_attached', payload={'target':inp.target,'target_id':inp.target_id,'passed':False,'status':res['status'],'command_count':0,'cwd':res['cwd'],'artifact_paths':[]})
        return res
    results=[]; first_cwd=None
    for i,c in enumerate(inp.commands,1):
        source, authority, scope, subtask_id=_default_validation_metadata(c, target=inp.target, target_obj=target_obj, subtask=_st)
        requested_blocking = bool(getattr(c,'blocking',None)) if getattr(c,'blocking',None) is not None else authority=='acceptance_blocking'
        confidence=getattr(c,'confidence',None) or ('high' if source in {'user_provided','user_success_criteria','integration','final'} or (source in {'project_detected','investigation_discovered','subtask_focused'} and authority=='acceptance_blocking') else 'low')
        reliable_blocking = (source in {'user_provided','user_success_criteria','integration','final'}) or (source in {'project_detected','investigation_discovered','subtask_focused'} and confidence=='high')
        blocking = bool(requested_blocking and reliable_blocking)
        if blocking:
            authority='acceptance_blocking'
        elif requested_blocking and source == 'generated':
            authority='diagnostic_only'
        elif requested_blocking:
            authority='supporting_evidence'
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
            item={'cmd':c.cmd,'command':c.cmd,'passed':False,'status':'infrastructure_error','reason':getattr(c,'reason',None) or reason,'error':str(e),'cwd':str(base_cwd.resolve()),'source':source,'confidence':confidence,'blocking':False if source in {'generated','diagnostic'} else blocking,'authority':'diagnostic_only' if source in {'generated','diagnostic'} else authority,'scope':scope,'subtask_id':subtask_id,'purpose':c.purpose or '','execution_mode':'argv','shell':False,'argv':[],'exit_code':None,'infrastructure_error':str(e)}
            item['evidence_strength']=_command_evidence_strength(source=item['source'],authority=item['authority'],confidence=item['confidence'],blocking=item['blocking'],status=item['status'])
            results.append(item)
            ctx.recorder.record('validation_command_rejected', payload={'target':inp.target,'target_id':inp.target_id,'cmd':c.cmd,'reason':reason,'message':str(e)})
            continue
        ctx.recorder.record('validation_started', payload={'target':inp.target,'target_id':inp.target_id,'target_label':label,'cmd':c.cmd,'cwd':str(cmd_cwd),'worktree_path':str(base_cwd) if inp.target in {'candidate','integration'} else None})
        so=outdir/f'{inp.target}_{inp.target_id or "repo"}_{i}.stdout.log'; se=outdir/f'{inp.target}_{inp.target_id or "repo"}_{i}.stderr.log'
        classified=classify_validation_command(cmd=c.cmd, source=source, confidence=confidence, blocking=blocking, reason=getattr(c,'reason',None) or c.purpose or '', timeout_seconds=c.timeout_seconds or 300)
        classified=classified.__class__(command=classified.command, source=classified.source, confidence=classified.confidence, blocking=classified.blocking, reason=classified.reason, argv=getattr(c,'argv',None), shell=bool(getattr(c,'shell',False)), timeout_seconds=classified.timeout_seconds)
        item=run_classified_validation(classified, cwd=cmd_cwd, stdout_path=so, stderr_path=se)
        item.update({'cwd':str(cmd_cwd),'scope':scope,'subtask_id':subtask_id,'purpose':c.purpose or '','authority':item.get('authority') if item.get('status')=='infrastructure_error' else authority,'source':source,'blocking':False if item.get('status')=='infrastructure_error' else blocking,'confidence':confidence})
        item['evidence_strength']=_command_evidence_strength(source=source,authority=authority,confidence=confidence,blocking=blocking,status=item.get('status'))
        results.append(item)
        ctx.recorder.record('validation_completed' if item.get('passed') else 'validation_failed', payload={'target':inp.target,'target_id':inp.target_id,'passed':item.get('passed'),'command_count':len(inp.commands),'cwd':str(cmd_cwd),'artifact_paths':{'stdout':str(so),'stderr':str(se)},'validation_result':item})
    statuses={r.get('status') for r in results}
    if any(r.get('status')=='failed_candidate' and r.get('blocking') for r in results): overall='failed_candidate'; passed=False
    elif 'timeout' in statuses: overall='timeout'; passed=False
    elif 'infrastructure_error' in statuses and not any(r.get('status') in {'failed_candidate','diagnostic_failed'} for r in results): overall='infrastructure_error'; passed=False
    elif 'diagnostic_failed' in statuses: overall='diagnostic_failed'; passed=False
    elif results and all(r.get('passed') for r in results): overall='passed'; passed=True
    else: overall='skipped_no_reliable_command'; passed=False
    temp={'commands':results,'status':overall}
    strength=validation_evidence_strength(temp)
    res={'raw_passed':passed,'raw_status':overall,'passed':passed,'status':overall,'commands':results,'target':inp.target,'target_id':inp.target_id,'cwd':first_cwd or str(base_cwd.resolve()),'evidence_strength':strength,'authoritative':strength in {'authoritative','project_test','explicit_user_command','high_confidence_project_detected'}}
    decision=make_validation_decision(res); res['decision']=decision; res['decision_status']=decision['status']; res['scope']=decision['scope']; res['subtask_id']=decision.get('subtask_id')
    if decision['status']=='passed': res['passed']=True; res['status']='passed'
    _attach_validation(state,target_obj,inp.target,res,ctx)
    if inp.target=='integration' and isinstance(target_obj,dict) and target_obj.get('review') is None and ctx.reviewer is not None:
        h_review_attempt(state, OpsReviewAttemptInput(attempt_id=target_obj.get('attempt_id') or 'integration_001', scope='integration'), ctx)
    ctx.recorder.record('validation_attached', payload={'target':inp.target,'target_id':inp.target_id,'passed':res['passed'],'status':res['status'],'command_count':len(inp.commands),'cwd':res['cwd'],'artifact_paths':[p for r in results for p in [r.get('stdout_path'),r.get('stderr_path')] if p]})
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
    if (state.execution_path=='parallel_candidates' or state.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock') and (isinstance(a,dict) or getattr(a,'scope',None)!='candidate'):
        raise ValueError('candidate path selection requires candidate attempt')
    status=a.get('status') if isinstance(a,dict) else a.status
    if status=='running': raise ValueError('cannot select running attempt')
    eligible, blockers=is_attempt_acceptance_eligible(a, state=state)
    val=_attempt_to_dict(a).get('validation') or {}
    strength=validation_evidence_strength(val)
    review=(_attempt_to_dict(a).get('review') or {})
    reliable_failed=((val.get('decision') or {}).get('status')=='failed') or ('validation_failed' in blockers)
    review_accepts=review.get('decision')=='pass' and review.get('recommended_action')=='accept'
    deadline_or_exhausted=any(str(x).lower() in {'candidate_attempt_budget_exhausted','attempts_exhausted','orchestration_deadline','backend_timeout','max_orchestration_turns_reached'} for x in (inp.reasons or [])) or state.phase=='selecting'
    unverified_selectable=(not eligible and not reliable_failed and is_usable_unverified_candidate(state,a) and (review_accepts or deadline_or_exhausted))
    if not unverified_selectable and not eligible and not reliable_failed and state.execution_path=='decomposed_subtasks' and inp.selected_attempt_id=='integration_001' and review_accepts and set(blockers).issubset({'validation_missing','validation_unverified'}):
        unverified_selectable=True
    override_warning=None
    model_selected_attempt_id=inp.selected_attempt_id
    if unverified_selectable:
        verified_alternatives=[]
        for other in getattr(state,'candidates',[]) or []:
            if getattr(other,'attempt_id',None)==inp.selected_attempt_id:
                continue
            try:
                other_ok, _other_blockers=is_attempt_acceptance_eligible(other, state=state)
            except Exception:
                other_ok=False
            if other_ok and validation_is_reliable(getattr(other,'validation',None) or {}):
                verified_alternatives.append(getattr(other,'attempt_id',None))
        if verified_alternatives:
            raise ValueError('selected attempt is unverified while verified alternatives exist: '+', '.join(x for x in verified_alternatives if x))
        usable=usable_unverified_candidates(state)
        if usable:
            best=best_unverified_candidate(state)
            if getattr(best,'attempt_id',None) != inp.selected_attempt_id and candidate_ranking_key(best, state=state) > candidate_ranking_key(a, state=state):
                override_warning={'warning':'model_selected_unverified_candidate_overridden_by_ranking','model_selected_attempt_id':inp.selected_attempt_id,'deterministic_selected_attempt_id':getattr(best,'attempt_id',None),'model_selected_evidence':candidate_ranking_evidence(a,state=state),'deterministic_selected_evidence':candidate_ranking_evidence(best,state=state)}
                state.warnings.append('model_selected_unverified_candidate_overridden_by_ranking')
                ctx.recorder.record('selection_override', payload=override_warning)
                a=best; inp.selected_attempt_id=getattr(best,'attempt_id',None)
                eligible, blockers=is_attempt_acceptance_eligible(a, state=state)
                val=_attempt_to_dict(a).get('validation') or {}
                strength=validation_evidence_strength(val)
    stored=a.get('acceptance_eligible') if isinstance(a,dict) else a.acceptance_eligible
    if isinstance(a,dict):
        a['acceptance_eligible']=eligible; a['acceptance_blockers']=blockers
    else:
        a.acceptance_eligible=eligible; a.acceptance_blockers=blockers
    if not eligible and not unverified_selectable:
        state.blockers=sorted(set(state.blockers+blockers))
        ctx.recorder.record('selection_rejected', payload={'selected_attempt_id':inp.selected_attempt_id,'stored_acceptance_eligible':stored,'recomputed_acceptance_eligible':eligible,'acceptance_blockers':blockers})
        raise ValueError('selected attempt is not acceptance eligible: '+', '.join(blockers))
    decision_bucket='accepted_verified' if eligible and validation_is_reliable(val) else ('accepted_unverified' if unverified_selectable or (eligible and not validation_is_reliable(val)) else 'rejected')
    ranking_evidence=candidate_ranking_evidence(a, state=state)
    selection_explanation=explain_candidate_selection(a, getattr(state,'candidates',[]) or [], state=state) if decision_bucket=='accepted_unverified' else {'winner':ranking_evidence,'nearest_alternatives':[],'reasons':['verified candidate selected through central acceptance gate'],'summary':'verified candidate selected through central acceptance gate'}
    new_selection={**inp.model_dump(),'model_selected_attempt_id':model_selected_attempt_id,'decision_bucket':decision_bucket,'materialization_signal':('verified_accepted' if decision_bucket=='accepted_verified' else 'unverified_best_candidate'),'validation_strength':strength,'validation_authoritative':validation_is_reliable(val),'selection_evidence':{'stored_acceptance_eligible':stored,'recomputed_acceptance_eligible':eligible,'acceptance_blockers':blockers,'validation_strength':strength, **ranking_evidence, 'selection_explanation':selection_explanation, **({'selection_warning':override_warning} if override_warning else {})}}
    if decision_bucket=='accepted_unverified':
        if override_warning:
            new_selection['warnings']=(new_selection.get('warnings') or []) + [override_warning['warning']]
        new_selection['summary']=(new_selection.get('summary') or '') + ' ' + selection_explanation['summary']
        new_selection['reasons']=list(dict.fromkeys((new_selection.get('reasons') or []) + selection_explanation['reasons']))
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
        val=a.get('validation') if isinstance(a,dict) else a.validation
        sel_bucket=(state.selection or {}).get('decision_bucket')
        if isinstance(a,dict):
            a['acceptance_eligible']=eligible; a['acceptance_blockers']=blockers
        else:
            a.acceptance_eligible=eligible; a.acceptance_blockers=blockers
        if not eligible and sel_bucket!='accepted_unverified':
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
        strength=validation_evidence_strength(val or {})
        final_payload['decision_bucket']=sel_bucket or ('accepted_verified' if validation_is_reliable(val or {}) else 'accepted_unverified')
        final_payload['materialization_signal']='verified_accepted' if final_payload['decision_bucket']=='accepted_verified' else 'unverified_best_candidate'
        final_payload['validation_strength']=strength
        final_payload['validation_authoritative']=validation_is_reliable(val or {})
        ranking_evidence=candidate_ranking_evidence(a, state=state)
        final_payload['selection_evidence']={**((state.selection or {}).get('selection_evidence') or {}), **ranking_evidence}
        if final_payload['decision_bucket']=='accepted_unverified':
            final_payload['selection_explanation']=explain_candidate_selection(a, getattr(state,'candidates',[]) or [], state=state)
        prefix='verified' if final_payload['decision_bucket']=='accepted_verified' else 'selected_unverified'
        final_payload['summary']=f"{prefix}: Selected {aid} changed {', '.join(changed or []) or 'no files'}; normalized score {ranking_evidence['normalized_review_score']:.3f}, normalized confidence {ranking_evidence['normalized_confidence']:.3f}; current validation {((val or {}).get('status') or 'not_run')} with strength {strength}." + ((' '+final_payload.get('summary','')) if final_payload.get('summary') else '')
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
    _eligible, _blockers, obs=_validate_review_observe_attempt(state,a,ctx,target_id=rid,review_scope='integration',commands=cmds)
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
'ops_run_validation':ToolSpec('ops_run_validation','Run validation commands in the selected target workspace automatically. Validation commands carry source, authority, and scope; only acceptance_blocking validation blocks acceptance. Diagnostic/exploratory failures are evidence, not blockers. Component subtasks use subtask-scoped evidence; global validation is reserved for integration/final acceptance. For candidate/integration targets, provide target_id and commands without cd/pushd/Set-Location; cwd defaults to the target worktree and relative cwd is resolved inside it. Keep commands cross-platform; do not use Unix-only utilities like head, tail, grep, sed, awk, cat, rm -rf, or export. Do not invent language-specific fallback commands unless project evidence supports them.',OpsRunValidationInput,h_validation),
'ops_select_winner':ToolSpec('ops_select_winner','Select winner',OpsSelectWinnerInput,h_select_winner),
'ops_finalize_run':ToolSpec('ops_finalize_run','Finalize run',OpsFinalizeRunInput,h_finalize),
}
def openai_tool_specs(adaptive:bool=False):
    hidden={'ops_run_single_task_attempts','ops_observe_completed_attempt','ops_launch_subtasks'}
    if adaptive:
        hidden |= {'ops_submit_decomposition','ops_validate_decomposition','ops_launch_candidates','ops_run_next_fallback_candidate_attempt','ops_run_next_subtask_attempt','ops_run_next_integration_repair_attempt','ops_start_candidate_fallback','ops_integrate_subtasks'}
    return [{'type':'function','function':{'name':n,'description':s.description,'parameters':s.input_model.model_json_schema(),'strict':True}} for n,s in OPS_TOOLS.items() if n not in hidden]
