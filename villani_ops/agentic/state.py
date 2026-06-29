from __future__ import annotations
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from .artifacts import read_text_utf8, write_text_utf8

class CandidateAttemptState(BaseModel):
    model_config=ConfigDict(extra='forbid')
    attempt_id:str; backend_name:str|None=None; model:str|None=None
    status:Literal['scheduled','running','completed','failed','reviewed','accepted','rejected']
    scope:Literal['candidate','subtask','integration']; subtask_id:str|None=None
    candidate_kind:Literal['normal','fallback']|None=None
    worktree_path:str|None=None; artifacts_dir:str|None=None; patch_path:str|None=None
    changed_files:list[str]=Field(default_factory=list); stdout_path:str|None=None; stderr_path:str|None=None; transcript_path:str|None=None
    review:dict|None=None; acceptance_eligible:bool=False; acceptance_blockers:list[str]=Field(default_factory=list)
    started_at:str|None=None; completed_at:str|None=None
    exit_code:int|None=None; exit_reason:str|None=None; failure_reason:str|None=None; runner_status:str|None=None; runner_error_type:str|None=None; duration_seconds:float|None=None
    added_files:list[str]=Field(default_factory=list); deleted_files:list[str]=Field(default_factory=list); modified_files:list[str]=Field(default_factory=list); renamed_files:list[str]=Field(default_factory=list)
    validation:dict|None=None; validation_results:list[dict]=Field(default_factory=list); validation_status:Literal['not_run','passed','failed','command_rejected','error','timed_out','inconclusive']='not_run'; validation_source:str|None=None; token_usage:dict|None=None; cost:float|None=None
    patch_hygiene:dict|None=None; scope_assessment:dict|None=None; runner_telemetry:dict=Field(default_factory=dict)
    review_status:Literal['not_run','passed','failed','unavailable','malformed','provider_error']='not_run'; review_error_type:str|None=None; review_error_message:str|None=None; review_retry_count:int=0

class AttemptObservation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    attempt_id:str
    scope:Literal["candidate","subtask","integration"]
    subtask_id:str|None=None
    backend_name:str|None=None
    model:str|None=None
    outcome:Literal["accepted","validation_failed","review_failed","runner_failed","infra_failed","no_patch","scope_failed","patch_failed","partial_progress","unknown"]
    progress_score:float=0.0
    failure_class:str|None=None
    evidence:list[str]=Field(default_factory=list)
    blockers:list[str]=Field(default_factory=list)
    changed_files:list[str]=Field(default_factory=list)
    validation_status:str|None=None
    validation_decision_status:str|None=None
    validation_decision_rationale:str|None=None
    blocking_validation_failures:list[str]=Field(default_factory=list)
    diagnostic_validation_failures:list[str]=Field(default_factory=list)
    supporting_validation_failures:list[str]=Field(default_factory=list)
    passed_blocking_validations:list[str]=Field(default_factory=list)
    review_status:str|None=None
    runner_signals:dict=Field(default_factory=dict)
    backend_signals:dict=Field(default_factory=dict)
    next_attempt_directives:list[str]=Field(default_factory=list)
    should_retry_same_plan:bool=False
    should_repair:bool=False
    should_decompose:bool=False
    should_escalate_backend:bool=False
    observed_at_stage:str='completed'
    validation_snapshot_id:str|None=None
    review_snapshot_id:str|None=None
    updated_at:str|None=None

class CandidateSummary(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate_id:str
    runner_status:str
    changed_files:list[str]=Field(default_factory=list)
    patch_summary:str
    validation_status:str|None=None
    telemetry_summary:dict=Field(default_factory=dict)
    material_behaviour_claims:list[str]=Field(default_factory=list)
    obvious_risks:list[str]=Field(default_factory=list)

class CandidateRiskReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate_id:str; summary:str; changed_files:list[str]=Field(default_factory=list)
    likely_correct:bool; confidence:float
    strengths:list[str]=Field(default_factory=list); risks:list[str]=Field(default_factory=list); likely_hidden_failures:list[str]=Field(default_factory=list); edge_cases_considered:list[str]=Field(default_factory=list); edge_cases_missed:list[str]=Field(default_factory=list)
    minimality_score:float; correctness_score:float; hidden_test_risk_score:float
    recommendation:Literal['strong_accept','accept','weak_accept','reject','uncertain']
    rationale:str

class PairwiseCandidateComparison(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate_a:str; candidate_b:str
    material_differences:list[str]=Field(default_factory=list); a_likely_failures:list[str]=Field(default_factory=list); b_likely_failures:list[str]=Field(default_factory=list)
    winner:Literal['candidate_a','candidate_b','tie','neither']; confidence:float; rationale:str

class RankedCandidate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate_id:str; rank:int; correctness_score:float; hidden_test_risk_score:float; pairwise_wins:int; pairwise_losses:int; validation_status:str|None=None; materiality_notes:str

class TournamentRanking(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ranked_candidates:list[RankedCandidate]=Field(default_factory=list)
    selected_candidate_id:str|None=None; selection_confidence:float; unresolved_risks:list[str]=Field(default_factory=list); rationale:str

class CandidateAgreementSummary(BaseModel):
    model_config=ConfigDict(extra='forbid')
    consensus_type:Literal['same_patch','same_answer','same_strategy','mixed','none']
    agreeing_candidates:list[str]=Field(default_factory=list); material_differences:list[str]=Field(default_factory=list); consensus_strength:float; rationale:str

class SubtaskState(BaseModel):
    model_config=ConfigDict(extra='forbid')
    subtask_id:str; title:str; objective:str; success_criteria:str|None=None
    relevant_files:list[str]=Field(default_factory=list); dependencies:list[str]=Field(default_factory=list)
    status:Literal['pending','running','accepted','failed','skipped']='pending'
    attempts:list[CandidateAttemptState]=Field(default_factory=list); accepted_attempt_id:str|None=None
    expected_difficulty:Literal['easy','medium','hard','unknown']='unknown'; risk:Literal['low','medium','high','unknown']='unknown'

class DecompositionDeadlock(BaseModel):
    model_config=ConfigDict(extra='forbid')
    deadlocked:bool
    reason:str
    failed_subtasks:list[str]=Field(default_factory=list)
    blocked_subtasks:list[str]=Field(default_factory=list)
    accepted_subtasks:list[str]=Field(default_factory=list)
    pending_subtasks:list[str]=Field(default_factory=list)
    can_continue_subtasks:bool=False

def _depends_on_failed(sid:str, by:dict, failed:set[str], seen:set[str]|None=None)->bool:
    seen=seen or set()
    if sid in seen: return False
    seen.add(sid)
    st=by.get(sid)
    if not st: return False
    return any(d in failed or _depends_on_failed(d, by, failed, seen) for d in st.dependencies)

def detect_decomposition_deadlock(state:'OpsRunState')->DecompositionDeadlock|None:
    if state.execution_path!='decomposed_subtasks': return None
    by={s.subtask_id:s for s in state.subtasks}
    failed={s.subtask_id for s in state.subtasks if s.status=='failed'}
    if not failed: return None
    accepted=sorted(s.subtask_id for s in state.subtasks if s.status=='accepted')
    pending=sorted(s.subtask_id for s in state.subtasks if s.status in {'pending','running'})
    blocked=sorted(s.subtask_id for s in state.subtasks if s.status=='skipped' or (s.status in {'pending','running'} and _depends_on_failed(s.subtask_id, by, failed)))
    ready=[s.subtask_id for s in state.subtasks if s.status=='pending' and all(by[d].status=='accepted' for d in s.dependencies)]
    exhausted=any(s.status=='failed' and len(s.attempts)>=max(1,int(state.candidate_attempts or 1)) for s in state.subtasks)
    incomplete=any(s.status not in {'accepted','skipped'} for s in state.subtasks) or bool(blocked)
    if (blocked or incomplete) and not ready and exhausted:
        reasons=['required_subtask_failed']
        if blocked: reasons += ['dependency_failed','blocked_dependents_exist']
        if exhausted: reasons.append('subtask_attempts_exhausted')
        if not ready: reasons.append('no_ready_subtasks_remaining')
        return DecompositionDeadlock(deadlocked=True,reason=','.join(dict.fromkeys(reasons)),failed_subtasks=sorted(failed),blocked_subtasks=blocked,accepted_subtasks=accepted,pending_subtasks=pending,can_continue_subtasks=False)
    return None

class OpsRunState(BaseModel):
    model_config=ConfigDict(extra='forbid')
    run_id:str; run_dir:str; repo_path:str; task:str; success_criteria:str|None=None; mode:str; runner:str; candidate_attempts:int
    orchestrator:Literal['adaptive','agentic']='agentic'
    status:Literal['active','completed','failed','interrupted']='active'
    phase:Literal['started','investigating','planning','decomposing','choosing_execution_path','running_candidates','running_subtasks','integrating','validating','selecting','finalizing','completed','failed']='started'
    classification:dict|None=None; investigation:dict|None=None; plan:dict|None=None; decomposition:dict|None=None
    execution_path:Literal['unknown','single_task','parallel_candidates','decomposed_subtasks','candidate_tournament']='unknown'
    candidate_execution_mode:Literal['unknown','sequential','parallel']='unknown'; attempts_requested:int|None=None; attempts_started:int=0; stopped_early:bool=False; stop_reason:str|None=None
    tournament_candidates_launched:int=0; tournament_candidates_completed:int=0; tournament_parallelism_used:int=0
    candidate_summaries:dict[str,CandidateSummary]=Field(default_factory=dict); candidate_risk_reviews:dict[str,CandidateRiskReview]=Field(default_factory=dict)
    pairwise_comparisons:list[PairwiseCandidateComparison]=Field(default_factory=list); tournament_ranking:TournamentRanking|None=None; candidate_agreement_summary:CandidateAgreementSummary|None=None
    selection_basis:Literal['validated_acceptance','evidence_based_tournament_selection','best_effort_tournament_selection','failed','inconclusive']|None=None
    candidate_attempts_requested:int|None=None; candidate_attempts_launched:int=0; candidate_launch_limit_reason:str|None=None
    reserve_finalization_seconds:int=30; reserve_review_seconds:int=60; max_review_retries:int=2; max_malformed_review_retries:int=2; candidate_generation_deadline:float|None=None
    decomposition_requested:bool=False; decomposition_validated:bool=False; decomposition_accepted:bool|None=None; decomposition_executed:bool=False
    decomposition_fallback_used:bool=False; decomposition_fallback_reason:str|None=None
    decomposed_execution_status:Literal['not_started','running','completed','blocked','failed']='not_started'
    decomposed_execution_blockers:list[str]=Field(default_factory=list)
    decomposed_execution_failed_subtasks:list[str]=Field(default_factory=list)
    decomposed_execution_blocked_subtasks:list[str]=Field(default_factory=list)
    decomposed_execution_completed_subtasks:list[str]=Field(default_factory=list)
    fallback_execution_path:Literal['none','parallel_candidates_after_decomposition_deadlock']='none'
    fallback_reason:str|None=None; fallback_used:bool=False; fallback_from_execution_path:str|None=None; fallback_started_at:str|None=None
    best_partial_attempt_id:str|None=None; partial_progress:dict|None=None
    candidates:list[CandidateAttemptState]=Field(default_factory=list); subtasks:list[SubtaskState]=Field(default_factory=list)
    attempt_observations:list[AttemptObservation]=Field(default_factory=list); backend_assessments:dict[str,dict]=Field(default_factory=dict); runner_assessment:dict=Field(default_factory=dict); adaptive_context:dict=Field(default_factory=dict)
    integration:dict|None=None; decomposition_integration_worktree:str|None=None; integration_base_revision:str|None=None
    accepted_patch_application_status:dict[str,dict]=Field(default_factory=dict)
    reviews:list[dict]=Field(default_factory=list); repo_validation_results:list[dict]=Field(default_factory=list); selection:dict|None=None; final_decision:dict|None=None
    active_nodes:list[str]=Field(default_factory=list); completed_nodes:list[str]=Field(default_factory=list); failed_nodes:list[str]=Field(default_factory=list)
    costs:dict[str,float]=Field(default_factory=dict); input_tokens:int=0; output_tokens:int=0
    usage_summary:dict=Field(default_factory=dict); usage_records_count:int=0; total_input_tokens:int=0; total_output_tokens:int=0; total_tokens:int=0; total_cost:float=0.0; usage_unavailable_count:int=0
    warnings:list[str]=Field(default_factory=list); blockers:list[str]=Field(default_factory=list); concurrency_mode:str|None=None; max_parallel:int|None=None; execution_concurrency:dict=Field(default_factory=dict); candidate_concurrency:dict=Field(default_factory=dict); subtask_concurrency:dict=Field(default_factory=dict); batch_count:int|None=None; wave_count:int|None=None; recovery_count:int=0; last_error:str|None=None; last_tool_name:str|None=None; last_tool_input:dict|None=None
    last_invalid_tool_name:str|None=None; last_invalid_tool_input_hash:str|None=None; repeat_invalid_count:int=0; last_progress_event_id:str|None=None; turns_since_progress:int=0
    def is_terminal(self)->bool: return self.status in {'completed','failed','interrupted'}
    def allowed_next_actions(self)->list[str]:
        if self.is_terminal(): return []
        a=['ops_get_state']
        if not self.investigation:
            a += ['ops_inspect_repo','ops_submit_classification','ops_submit_investigation']; return a
        if not self.plan: a.append('ops_submit_plan'); return a
        if self.orchestrator=='adaptive' and self.execution_path=='unknown': a.append('ops_select_execution_path'); return a
        if self.decomposition_requested and not self.decomposition: a.append('ops_submit_decomposition'); return a
        if self.decomposition and not self.decomposition_validated: a.append('ops_validate_decomposition'); return a
        if self.execution_path=='unknown': a.append('ops_select_execution_path'); return a
        if self.execution_path=='candidate_tournament':
            if not self.candidates: a.append('ops_launch_tournament_candidates'); return list(dict.fromkeys(a))
            if self.tournament_ranking is None: a.append('ops_select_winner'); return list(dict.fromkeys(a))
            a += ['ops_finalize_run']; return list(dict.fromkeys(a))
        if self.execution_path=='single_task':
            budget=max(1,int(self.candidate_attempts or 1))
            eligible=[]; needs_validation=[]; needs_review=[]; needs_observation=[]
            obs_by_id={o.attempt_id:o for o in self.attempt_observations}
            for c in self.candidates:
                try:
                    from villani_ops.core.acceptance import is_attempt_acceptance_eligible
                    if is_attempt_acceptance_eligible(c,state=self)[0]: eligible.append(c)
                except Exception:
                    pass
                complete=c.status in {'completed','failed','reviewed','rejected','accepted'}
                if complete and c.patch_path and c.changed_files and not c.validation:
                    needs_validation.append(c)
                if complete and c.patch_path and c.changed_files and not c.review:
                    needs_review.append(c)
                o=obs_by_id.get(c.attempt_id)
                val_snap=f"{c.validation_status}:{len(c.validation_results or [])}"
                rev_snap=f"{c.review_status}:{c.review_retry_count}:{bool(c.review)}"
                if complete and (o is None or o.validation_snapshot_id!=val_snap or o.review_snapshot_id!=rev_snap):
                    needs_observation.append(c)
            if eligible: a += ['ops_select_winner','ops_finalize_run']; return list(dict.fromkeys(a))
            if needs_validation: a.append('ops_run_validation'); return list(dict.fromkeys(a))
            if needs_review: a.append('ops_review_attempt'); return list(dict.fromkeys(a))
            if needs_observation: a.append('ops_observe_completed_attempt'); return list(dict.fromkeys(a))
            if len(self.candidates) < budget: a += ['ops_run_next_candidate_attempt']; return list(dict.fromkeys(a))
            a += ['ops_select_winner','ops_finalize_run']; return list(dict.fromkeys(a))
        if self.execution_path=='parallel_candidates' and not self.candidates: a.append('ops_launch_candidates'); return a
        if self.fallback_execution_path=='parallel_candidates_after_decomposition_deadlock':
            budget=max(1,int(self.candidate_attempts or 1))
            obs_by_id={o.attempt_id:o for o in self.attempt_observations}
            complete=[c for c in self.candidates if c.status in {'completed','failed','reviewed','rejected','accepted'}]
            eligible=[]; needs_validation=[]; needs_review=[]; needs_observation=[]
            for c in complete:
                try:
                    from villani_ops.core.acceptance import is_attempt_acceptance_eligible
                    if is_attempt_acceptance_eligible(c,state=self)[0]: eligible.append(c)
                except Exception:
                    pass
                if c.patch_path and c.changed_files and not c.validation: needs_validation.append(c)
                if c.patch_path and c.changed_files and not c.review and not c.failure_reason: needs_review.append(c)
                o=obs_by_id.get(c.attempt_id); val_snap=f"{c.validation_status}:{len(c.validation_results or [])}"; rev_snap=f"{c.review_status}:{c.review_retry_count}:{bool(c.review)}"
                if o is None or o.validation_snapshot_id!=val_snap or o.review_snapshot_id!=rev_snap: needs_observation.append(c)
            if eligible: a += ['ops_select_winner','ops_finalize_run']; return list(dict.fromkeys(a))
            if needs_validation: a.append('ops_run_validation'); return list(dict.fromkeys(a))
            if needs_review: a.append('ops_review_attempt'); return list(dict.fromkeys(a))
            if needs_observation: a.append('ops_observe_completed_attempt'); return list(dict.fromkeys(a))
            if len(complete) < budget: a.append('ops_run_next_fallback_candidate_attempt'); return list(dict.fromkeys(a))
            a.append('ops_finalize_run'); return list(dict.fromkeys(a))
        if self.execution_path=='decomposed_subtasks' and self.decomposed_execution_status in {'blocked','failed'}:
            if not self.candidates: a.append('ops_start_candidate_fallback')
            a += ['ops_select_winner','ops_finalize_run']; return list(dict.fromkeys(a))
        if self.execution_path=='decomposed_subtasks':
            if any(s.status=='pending' for s in self.subtasks): a += ['ops_run_next_subtask_attempt']; return list(dict.fromkeys(a))
            if all(s.status in {'accepted','skipped'} for s in self.subtasks) and not self.integration: a.append('ops_integrate_subtasks'); return a
            if self.integration and (self.integration.get('validation') or {}).get('passed') is False: a.append('ops_run_next_integration_repair_attempt'); return list(dict.fromkeys(a))
        a += ['ops_review_attempt','ops_run_validation','ops_select_winner','ops_finalize_run']
        return a
    def save(self,path:Path)->None:
        write_text_utf8(path, self.model_dump_json(indent=2), atomic=True)
    @classmethod
    def load(cls,path:Path)->'OpsRunState':
        text = read_text_utf8(path)
        if not text.strip():
            tmp = Path(path).with_name(Path(path).name + '.tmp')
            if tmp.exists():
                tmp_text = read_text_utf8(tmp)
                if tmp_text.strip():
                    return cls.model_validate_json(tmp_text)
            raise ValueError('state.json is empty or corrupted, likely due a previous interrupted/failed write. Check runtime_events.jsonl and transcript.json.')
        return cls.model_validate_json(text)
