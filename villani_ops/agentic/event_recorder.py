from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
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
        ev=self.events(); (self.run_dir/'event_digest.json').write_text(json.dumps({'run_id':self.run_id,'status':state.status,'phase':state.phase,'event_count':len(ev),'event_types':[e['type'] for e in ev]},indent=2))
