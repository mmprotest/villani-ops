from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from typing import Callable
import json, uuid, threading
from .events import OpsEvent
from .artifacts import read_text_utf8, write_json_utf8

class OpsEventRecorder:
    def __init__(self, run_dir:Path, run_id:str, on_event:Callable[[OpsEvent],None]|None=None):
        self.run_dir=Path(run_dir); self.run_id=run_id; self.path=self.run_dir/'runtime_events.jsonl'; self.run_dir.mkdir(parents=True,exist_ok=True); self._lock=threading.Lock(); self.on_event=on_event; self._dedupe_keys=set()
    def record(self,type:str,payload:dict|None=None,**fields)->None:
        payload = payload or {}
        if type in {'selection_completed','run_finalized'}:
            key=(type, payload.get('selected_attempt_id'), payload.get('decision'))
            with self._lock:
                if key in self._dedupe_keys:
                    return
                self._dedupe_keys.add(key)
        ev=OpsEvent(event_id=str(uuid.uuid4()),run_id=self.run_id,timestamp=datetime.now(timezone.utc).isoformat(),type=type,payload=payload,**fields)
        line=json.dumps(ev.model_dump(mode='json'), ensure_ascii=False, default=str)+"\n"
        with self._lock:
            with self.path.open('a', encoding='utf-8', newline='\n') as f:
                f.write(line); f.flush()
        if self.on_event:
            try: self.on_event(ev)
            except Exception: pass
    def events(self):
        return [json.loads(l) for l in read_text_utf8(self.path, default='').splitlines()] if self.path.exists() else []
    def write_digest(self,state):
        with self._lock:
            ev=self.events()
        types=[e['type'] for e in ev]
        attempts=[*state.candidates, *[a for st in state.subtasks for a in st.attempts]]
        blockers=[b for a in attempts for b in (a.acceptance_blockers or [])]
        reviewed=[a for a in attempts if a.review]
        imported=[r for a in attempts for r in (a.validation_results or []) if r.get('validation_source')=='villani_code_debug_trace']
        digest={'run_id':self.run_id,'status':state.status,'phase':state.phase,'execution_path':state.execution_path,'candidate_execution_mode':getattr(state,'candidate_execution_mode','unknown'),'attempts_requested':getattr(state,'attempts_requested',None),'single_task_attempts_started':getattr(state,'attempts_started',0),'stopped_early':getattr(state,'stopped_early',False),'stop_reason':getattr(state,'stop_reason',None),'decomposition_requested':state.decomposition_requested,'decomposition_validated':state.decomposition_validated,'decomposition_accepted':state.decomposition_accepted,'decomposition_executed':state.decomposition_executed,'decomposition_fallback_used':state.decomposition_fallback_used,'decomposed_execution_status':state.decomposed_execution_status,'decomposed_execution_blockers':state.decomposed_execution_blockers,'decomposed_execution_failed_subtasks':state.decomposed_execution_failed_subtasks,'decomposed_execution_blocked_subtasks':state.decomposed_execution_blocked_subtasks,'fallback_used':state.fallback_used,'fallback_execution_path':state.fallback_execution_path,'fallback_reason':state.fallback_reason,'fallback_from_execution_path':state.fallback_from_execution_path,'partial_progress':state.partial_progress,'best_partial_attempt_id':state.best_partial_attempt_id,'attempts_started':sum(1 for a in attempts if a.started_at),'attempts_completed':sum(1 for a in attempts if a.status in {'completed','reviewed','accepted'}),'attempts_failed':sum(1 for a in attempts if a.status in {'failed','rejected'}),'attempts_reviewed':len(reviewed),'attempts_acceptance_eligible':sum(1 for a in attempts if a.acceptance_eligible),'attempts_blocked':sum(1 for a in reviewed if not a.acceptance_eligible),'runner_failures':sum(1 for a in attempts if a.runner_status in {'exception'} or a.failure_reason or (a.exit_code is not None and a.exit_code!=0)),'validation_failures':types.count('validation_failed'),'changed_files_count':sum(len(a.changed_files or []) for a in attempts)+len((state.integration or {}).get('changed_files') or []),'deleted_files_count':sum(len(a.deleted_files or []) for a in attempts)+len((state.integration or {}).get('deleted_files') or []),'integration_failure_reason':(state.integration or {}).get('failure_reason'),'concurrency_mode':state.concurrency_mode,'max_parallel':state.max_parallel,'execution_concurrency':getattr(state,'execution_concurrency',{}),'candidate_concurrency_mode':(getattr(state,'candidate_concurrency',{}) or {}).get('concurrency_mode'),'subtask_concurrency_mode':(getattr(state,'subtask_concurrency',{}) or {}).get('concurrency_mode'),'candidate_batch_count':(getattr(state,'candidate_concurrency',{}) or {}).get('batch_count') or getattr(state,'batch_count',None),'subtask_wave_count':(getattr(state,'subtask_concurrency',{}) or {}).get('wave_count') or getattr(state,'wave_count',None),'common_blockers':dict(Counter(blockers).most_common()),'validations_passed':types.count('validation_completed'),'validations_failed':types.count('validation_failed'),'imported_validation_count':len(imported),'imported_validation_passed':sum(1 for r in imported if r.get('passed') is True),'imported_validation_failed':sum(1 for r in imported if r.get('passed') is False),'validation_source':'villani_code_debug_trace' if imported else None,'scope_assessment':[a.scope_assessment for a in attempts if a.scope_assessment],'scope_exception_used':any((a.scope_assessment or {}).get('scope_exception_used') for a in attempts),'scope_exception_adequate':any((a.scope_assessment or {}).get('scope_exception_adequate') for a in attempts),'integration_status':(state.integration or {}).get('status'),'selected_attempt':(state.selection or {}).get('selected_attempt_id'),'final_decision':state.final_decision,'blockers':state.blockers,'warnings':state.warnings,'recovery_count':state.recovery_count,'event_count':len(ev),'decomposition_deadlock_detected':types.count('decomposition_deadlock_detected'),'failed_subtasks':state.decomposed_execution_failed_subtasks,'blocked_subtasks':state.decomposed_execution_blocked_subtasks,'fallback_candidates_started':sum(1 for e in ev if e['type']=='candidate_attempt_started' and (e.get('payload') or {}).get('fallback')),'fallback_candidates_completed':sum(1 for e in ev if e['type'] in {'candidate_attempt_completed','candidate_attempt_failed'} and (e.get('payload') or {}).get('fallback')),'event_types':types,'behavioural_oracle_summary':getattr(state,'behavioural_oracles',[]), 'behavioural_probe_results':getattr(state,'behavioural_probe_results',[]), 'unresolved_behavioural_gaps':[{'attempt_id':getattr(a,'attempt_id',None),'failed':getattr(a,'critical_requirements_failed',[]),'uncertain':getattr(a,'critical_requirements_uncertain',[]),'oracle_coverage_score':getattr(a,'oracle_coverage_score',0)} for a in attempts], 'usage':getattr(state,'usage_summary',{}) or {'input_tokens':getattr(state,'total_input_tokens',0),'output_tokens':getattr(state,'total_output_tokens',0),'total_tokens':getattr(state,'total_tokens',0),'total_cost':getattr(state,'total_cost',0.0),'unavailable_calls_count':getattr(state,'usage_unavailable_count',0)}}
        with self._lock:
            write_json_utf8(self.run_dir/'event_digest.json', digest, atomic=True)
