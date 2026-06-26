from __future__ import annotations
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

class CandidateAttemptState(BaseModel):
    model_config=ConfigDict(extra='forbid')
    attempt_id:str; backend_name:str|None=None; model:str|None=None
    status:Literal['scheduled','running','completed','failed','reviewed','accepted','rejected']
    scope:Literal['candidate','subtask','integration']; subtask_id:str|None=None
    worktree_path:str|None=None; artifacts_dir:str|None=None; patch_path:str|None=None
    changed_files:list[str]=Field(default_factory=list); stdout_path:str|None=None; stderr_path:str|None=None; transcript_path:str|None=None
    review:dict|None=None; acceptance_eligible:bool=False; acceptance_blockers:list[str]=Field(default_factory=list)
    started_at:str|None=None; completed_at:str|None=None
    exit_code:int|None=None; exit_reason:str|None=None; failure_reason:str|None=None; runner_status:str|None=None; runner_error_type:str|None=None; duration_seconds:float|None=None
    added_files:list[str]=Field(default_factory=list); deleted_files:list[str]=Field(default_factory=list); modified_files:list[str]=Field(default_factory=list); renamed_files:list[str]=Field(default_factory=list)
    validation:dict|None=None; token_usage:dict|None=None; cost:float|None=None

class SubtaskState(BaseModel):
    model_config=ConfigDict(extra='forbid')
    subtask_id:str; title:str; objective:str; success_criteria:str|None=None
    relevant_files:list[str]=Field(default_factory=list); dependencies:list[str]=Field(default_factory=list)
    status:Literal['pending','running','accepted','failed','skipped']='pending'
    attempts:list[CandidateAttemptState]=Field(default_factory=list); accepted_attempt_id:str|None=None
    expected_difficulty:Literal['easy','medium','hard','unknown']='unknown'; risk:Literal['low','medium','high','unknown']='unknown'

class OpsRunState(BaseModel):
    model_config=ConfigDict(extra='forbid')
    run_id:str; run_dir:str; repo_path:str; task:str; success_criteria:str|None=None; mode:str; runner:str; candidate_attempts:int
    status:Literal['active','completed','failed','interrupted']='active'
    phase:Literal['started','investigating','planning','decomposing','choosing_execution_path','running_candidates','running_subtasks','integrating','validating','selecting','finalizing','completed','failed']='started'
    classification:dict|None=None; investigation:dict|None=None; plan:dict|None=None; decomposition:dict|None=None
    execution_path:Literal['unknown','parallel_candidates','decomposed_subtasks']='unknown'
    decomposition_requested:bool=False; decomposition_validated:bool=False; decomposition_accepted:bool|None=None; decomposition_executed:bool=False
    decomposition_fallback_used:bool=False; decomposition_fallback_reason:str|None=None
    candidates:list[CandidateAttemptState]=Field(default_factory=list); subtasks:list[SubtaskState]=Field(default_factory=list)
    integration:dict|None=None; reviews:list[dict]=Field(default_factory=list); selection:dict|None=None; final_decision:dict|None=None
    active_nodes:list[str]=Field(default_factory=list); completed_nodes:list[str]=Field(default_factory=list); failed_nodes:list[str]=Field(default_factory=list)
    costs:dict[str,float]=Field(default_factory=dict); input_tokens:int=0; output_tokens:int=0
    warnings:list[str]=Field(default_factory=list); blockers:list[str]=Field(default_factory=list); concurrency_mode:str|None=None; max_parallel:int|None=None; recovery_count:int=0; last_error:str|None=None; last_tool_name:str|None=None; last_tool_input:dict|None=None
    last_invalid_tool_name:str|None=None; last_invalid_tool_input_hash:str|None=None; repeat_invalid_count:int=0; last_progress_event_id:str|None=None; turns_since_progress:int=0
    def is_terminal(self)->bool: return self.status in {'completed','failed','interrupted'}
    def allowed_next_actions(self)->list[str]:
        if self.is_terminal(): return []
        a=['ops_get_state']
        if not self.investigation:
            a += ['ops_inspect_repo','ops_submit_classification','ops_submit_investigation']; return a
        if not self.plan: a.append('ops_submit_plan'); return a
        if self.decomposition_requested and not self.decomposition: a.append('ops_submit_decomposition'); return a
        if self.decomposition and not self.decomposition_validated: a.append('ops_validate_decomposition'); return a
        if self.execution_path=='unknown': a.append('ops_select_execution_path'); return a
        if self.execution_path=='parallel_candidates' and not self.candidates: a.append('ops_launch_candidates'); return a
        if self.execution_path=='decomposed_subtasks':
            if any(s.status=='pending' for s in self.subtasks): a.append('ops_launch_subtasks'); return a
            if all(s.status in {'accepted','skipped'} for s in self.subtasks) and not self.integration: a.append('ops_integrate_subtasks'); return a
        a += ['ops_review_attempt','ops_run_validation','ops_select_winner','ops_finalize_run']
        return a
    def save(self,path:Path)->None: path.write_text(self.model_dump_json(indent=2))
    @classmethod
    def load(cls,path:Path)->'OpsRunState': return cls.model_validate_json(path.read_text())
