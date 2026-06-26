from __future__ import annotations
from pathlib import Path
import json

def write_transcript(run_dir:Path, transcript:list[dict]): (Path(run_dir)/'transcript.json').write_text(json.dumps(transcript,indent=2))
def derive_graph(state, events:list[dict])->dict:
    nodes=[{'id':'run','type':'run','status':state.status},{'id':'investigation','type':'investigation','present':state.investigation is not None},{'id':'plan','type':'plan','present':state.plan is not None}]
    if state.decomposition: nodes.append({'id':'decomposition','type':'decomposition','accepted':state.decomposition_accepted,'validated':state.decomposition_validated,'executed':state.decomposition_executed,'fallback_used':state.decomposition_fallback_used})
    for c in state.candidates: nodes.append({'id':c.attempt_id,'type':c.scope,'status':('accepted' if c.status=='accepted' and c.acceptance_eligible else c.status),'acceptance_eligible':c.acceptance_eligible,'acceptance_blockers':c.acceptance_blockers})
    for s in state.subtasks: nodes.append({'id':s.subtask_id,'type':'subtask','status':s.status})
    if state.integration: nodes.append({'id':'integration','type':'integration','status':state.integration.get('status'),'failure_reason':state.integration.get('failure_reason'),'acceptance_eligible':state.integration.get('acceptance_eligible'),'acceptance_blockers':state.integration.get('acceptance_blockers') or []})
    nodes += [{'id':'selection','type':'selection','present':state.selection is not None},{'id':'finalization','type':'finalization','present':state.final_decision is not None}]
    return {'canonical':'state.json','derived_from_events':len(events),'nodes':nodes,'edges':[]}
def write_artifacts(run_dir:Path,state,events:list[dict],transcript:list[dict]):
    run_dir=Path(run_dir); state.save(run_dir/'state.json'); write_transcript(run_dir,transcript); (run_dir/'orchestration_graph.json').write_text(json.dumps(derive_graph(state,events),indent=2))
