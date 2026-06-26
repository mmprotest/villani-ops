from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import json, uuid
from .events import OpsEvent
class OpsEventRecorder:
    def __init__(self, run_dir:Path, run_id:str): self.run_dir=Path(run_dir); self.run_id=run_id; self.path=self.run_dir/'runtime_events.jsonl'; self.run_dir.mkdir(parents=True,exist_ok=True)
    def record(self,type:str,payload:dict|None=None,**fields)->None:
        ev=OpsEvent(event_id=str(uuid.uuid4()),run_id=self.run_id,timestamp=datetime.now(timezone.utc).isoformat(),type=type,payload=payload or {},**fields)
        with self.path.open('a') as f: f.write(ev.model_dump_json()+"\n")
    def events(self):
        return [json.loads(l) for l in self.path.read_text().splitlines()] if self.path.exists() else []
    def write_digest(self,state):
        ev=self.events(); types=[e['type'] for e in ev]
        attempts=[*state.candidates, *[a for st in state.subtasks for a in st.attempts]]
        blockers=[b for a in attempts for b in (a.acceptance_blockers or [])]
        reviewed=[a for a in attempts if a.review]
        digest={'run_id':self.run_id,'status':state.status,'phase':state.phase,'execution_path':state.execution_path,'decomposition_requested':state.decomposition_requested,'decomposition_validated':state.decomposition_validated,'decomposition_accepted':state.decomposition_accepted,'decomposition_executed':state.decomposition_executed,'decomposition_fallback_used':state.decomposition_fallback_used,'attempts_started':sum(1 for a in attempts if a.started_at),'attempts_completed':sum(1 for a in attempts if a.status in {'completed','reviewed','accepted'}),'attempts_failed':sum(1 for a in attempts if a.status in {'failed','rejected'}),'attempts_reviewed':len(reviewed),'attempts_acceptance_eligible':sum(1 for a in attempts if a.acceptance_eligible),'attempts_blocked':sum(1 for a in reviewed if not a.acceptance_eligible),'common_blockers':dict(Counter(blockers).most_common()),'validations_passed':types.count('validation_completed'),'validations_failed':types.count('validation_failed'),'integration_status':(state.integration or {}).get('status'),'selected_attempt':(state.selection or {}).get('selected_attempt_id'),'final_decision':state.final_decision,'blockers':state.blockers,'warnings':state.warnings,'recovery_count':state.recovery_count,'event_count':len(ev),'event_types':types}
        (self.run_dir/'event_digest.json').write_text(json.dumps(digest,indent=2))
