from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import secrets
from pydantic import BaseModel, ConfigDict
from villani_ops.core.decision import Decision
from .state import OpsRunState
from .event_recorder import OpsEventRecorder
from .tools import openai_tool_specs
from .state_tooling import execute_tool_with_policy, OpsToolContext
from .prompts import SYSTEM_PROMPT, ADAPTIVE_SYSTEM_APPENDIX, initial_user_message
from .recovery import handle_no_tool_call, recommend_next_agentic_action
from .artifacts import write_artifacts
from .client import ToolCallingLLMClient
from villani_ops.runners import runner_for_name
from villani_ops.telemetry.usage import UsageRecorder, usage_record_from_response
from villani_ops.execution_policies import policy_for_mode
from villani_ops.core.policy import DEFAULT_TIMEOUT_SECONDS
from villani_ops.orchestration.nodes import OrchestrationNode
from villani_ops.orchestration.context import TaskContext

class AgenticLLMReviewer:
    def __init__(self, review_backend):
        self.review_backend=review_backend
        self.client=ToolCallingLLMClient(); self.last_response=None
    def review(self, *, state, attempt, scope):
        from .tools import OpsReviewResult
        payload={'task':state.task,'success_criteria':state.success_criteria,'execution_path':state.execution_path,'scope':scope,'review_payload':attempt}
        tool={'type':'function','function':{'name':'agentic_review_decision','description':'Return a structured Villani Ops review decision. Never pass without evidence.','parameters':OpsReviewResult.model_json_schema(),'strict':True}}
        messages=[{'role':'user','content':'Review this attempt using the forced structured review tool. Fail closed on missing evidence, runner failure, validation failure, or scope mismatch.\n'+__import__('json').dumps(payload,indent=2,default=str)[:120000]}]
        resp=self.client.create_message(backend=self.review_backend,messages=messages,system='You are a strict production code reviewer for Villani Ops agentic mode.',tools=[tool],tool_choice={'type':'function','function':{'name':'agentic_review_decision'}},strict=True)
        self.last_response=resp
        raw={'raw_response':getattr(resp,'raw_response',{}),'content':getattr(resp,'content',[])}
        state_reviews=getattr(state,'reviews',None)
        if isinstance(state_reviews,list):
            state_reviews.append({'attempt_id':attempt.get('attempt',{}).get('attempt_id') if isinstance(attempt,dict) else None,'structured_review_request_summary':{'scope':scope,'changed_files':attempt.get('changed_files') if isinstance(attempt,dict) else None},'structured_review_raw_response':raw})
        calls=[b for b in resp.content if b.get('type')=='tool_use' and b.get('name')=='agentic_review_decision']
        if not calls:
            raise ValueError('agentic_structured_review_missing_tool_call')
        parsed=OpsReviewResult.model_validate(calls[0].get('input') or {})
        if isinstance(state_reviews,list): state_reviews[-1]['structured_review_parsed_result']=parsed.model_dump()
        return parsed.model_dump()

def _fakeish(obj):
    n=(getattr(obj,'name',None) or obj.__class__.__name__).lower()
    return 'fake' in n or 'placeholder' in n

def _select_role_backend(mode, backends, role, task):
    node_kind={'orchestration':'plan','investigation':'investigate','decomposition':'decompose','coding':'code','review':'review','selection':'select'}[role]
    node=OrchestrationNode(id=f'agentic_{role}',kind=node_kind,objective=f'agentic {role}')
    sel=policy_for_mode(mode).select_backend(node=node, backends=backends, task_context=TaskContext(objective=task))
    if not sel.backend: raise ValueError(f'agentic_backend_role_unavailable: {role}')
    return sel.backend_name, sel.backend


def _state_aware_no_progress_summary(state):
    reviewed=[c.attempt_id for c in state.candidates if c.review]
    validated=[c.attempt_id for c in state.candidates if c.validation]
    eligible=[]
    try:
        from villani_ops.core.acceptance import is_attempt_acceptance_eligible
        eligible=[c.attempt_id for c in state.candidates if is_attempt_acceptance_eligible(c,state=state)[0]]
    except Exception:
        pass
    if reviewed or validated or eligible:
        return f"The orchestrator made no tool-call progress. Candidates completed={len(state.candidates)}, reviewed={reviewed}, validated={validated}, eligible={eligible}, selection={(state.selection or {}).get('selected_attempt_id')}. No accepted result was finalized."
    return 'agentic_orchestrator_no_progress'

class OpsRunRequest(BaseModel):
    model_config=ConfigDict(extra='forbid', arbitrary_types_allowed=True)
    repo_path:str; task:str; success_criteria:str|None=None; mode:str='performance'; runner:str='villani-code'; candidate_attempts:int=3; timeout_seconds:int|None=DEFAULT_TIMEOUT_SECONDS; workspace:str='.villani-ops'; orchestrator:str='agentic'; backend:object|None=None; backends:object|None=None; runner_adapter:object|None=None; reviewer:object|None=None; production:bool=True; allow_fake_dependencies:bool=False
class OpsRunResult(BaseModel):
    model_config=ConfigDict(arbitrary_types_allowed=True)
    run_id:str; run_dir:str; state:OpsRunState; decision:Decision
class OpsRunner:
    def __init__(self, storage=None, client=None, backend=None, backends=None, runner_adapter=None, reviewer=None, max_turns:int=60, max_recovery_attempts:int=2, progress_reporter=None): self.storage=storage; self.client=client or ToolCallingLLMClient(); self.backend=backend; self.backends=backends; self.runner_adapter=runner_adapter; self.reviewer=reviewer; self.max_turns=max_turns; self.max_recovery_attempts=max_recovery_attempts; self.progress_reporter=progress_reporter
    def run(self, request:OpsRunRequest)->OpsRunResult:
        rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)
        run_dir=(self.storage.create_run_dir(rid) if self.storage else Path(request.workspace)/'runs'/rid); run_dir.mkdir(parents=True,exist_ok=True)
        state=OpsRunState(run_id=rid,run_dir=str(run_dir),repo_path=request.repo_path,task=request.task,success_criteria=request.success_criteria,mode=request.mode,runner=request.runner,candidate_attempts=request.candidate_attempts,orchestrator=request.orchestrator)
        rec=OpsEventRecorder(run_dir,rid,on_event=(self.progress_reporter.on_event if self.progress_reporter else None)); usage_rec=UsageRecorder(run_dir,rid); transcript=[]; state.save(run_dir/'state.json'); rec.record('run_started',payload={'run_dir':str(run_dir)},phase=state.phase); usage_rec.write_artifacts()
        def _update_usage_state():
            summary=usage_rec.summarize(); state.usage_summary=summary.model_dump(mode='json'); state.usage_records_count=summary.calls_count; state.total_input_tokens=summary.input_tokens; state.total_output_tokens=summary.output_tokens; state.total_tokens=summary.total_tokens; state.total_cost=summary.total_cost; state.usage_unavailable_count=summary.unavailable_calls_count; state.input_tokens=summary.input_tokens; state.output_tokens=summary.output_tokens; state.costs={'total':summary.total_cost,'input':summary.input_cost,'output':summary.output_cost}
        backends=request.backends or self.backends
        backend=request.backend or self.backend
        role_backends={}
        try:
            if backends:
                for role in ['orchestration','coding','review','selection']:
                    role_backends[role]=_select_role_backend(request.mode, backends, role, request.task)
                if backend is None: backend=role_backends['orchestration'][1]
            elif backend is not None:
                role_backends={r:(getattr(backend,'name','backend'),backend) for r in ['orchestration','coding','review','selection']}
        except Exception as e:
            state.status='failed'; state.phase='failed'; state.final_decision={'decision':'failed','summary':str(e),'blockers':['agentic_backend_role_unavailable']}; state.save(run_dir/'state.json'); rec.record('run_finalized',payload=state.final_decision); rec.write_digest(state); write_artifacts(run_dir,state,rec.events(),transcript); return OpsRunResult(run_id=rid,run_dir=str(run_dir),state=state,decision=Decision(run_id=rid,accepted=False,mode=state.mode,runner=state.runner,reason=str(e),failure_reason=str(e)))
        if backend is None or not getattr(backend,'model',None) or not (hasattr(backend,'create_message') or getattr(backend,'base_url',None)) or (request.production and not request.allow_fake_dependencies and _fakeish(backend)):
            msg='agentic orchestrator requires a real configured backend with tool-calling support or OpenAI-compatible chat completions'
            state.status='failed'; state.phase='failed'; state.final_decision={'decision':'failed','summary':msg,'blockers':['backend_config_missing']}; state.save(run_dir/'state.json'); rec.record('run_finalized',payload=state.final_decision); rec.write_digest(state); write_artifacts(run_dir,state,rec.events(),transcript); return OpsRunResult(run_id=rid,run_dir=str(run_dir),state=state,decision=Decision(run_id=rid,accepted=False,mode=state.mode,runner=state.runner,reason=msg,failure_reason=msg))
        if request.production and not request.allow_fake_dependencies and _fakeish(self.client):
            msg='fake orchestrator client forbidden in production agentic mode'
            state.status='failed'; state.phase='failed'; state.final_decision={'decision':'failed','summary':msg,'blockers':['agentic_fake_client_forbidden']}; state.save(run_dir/'state.json'); rec.record('run_finalized',payload=state.final_decision); rec.write_digest(state); write_artifacts(run_dir,state,rec.events(),transcript); return OpsRunResult(run_id=rid,run_dir=str(run_dir),state=state,decision=Decision(run_id=rid,accepted=False,mode=state.mode,runner=state.runner,reason=msg,failure_reason=msg))
        runner_adapter=request.runner_adapter or self.runner_adapter or runner_for_name(request.runner)
        reviewer=request.reviewer or self.reviewer or (AgenticLLMReviewer(role_backends['review'][1]) if role_backends.get('review') else None)
        coding_name,coding_backend=role_backends.get('coding',(getattr(backend,'name',None),backend))
        review_name,review_backend=role_backends.get('review',(None,None))
        def _ctx():
            return OpsToolContext(run_dir=run_dir,recorder=rec,transcript=transcript,runner_adapter=runner_adapter,reviewer=reviewer,backend=backend,backend_name=getattr(backend,'name',None),coding_backend=coding_backend,coding_backend_name=coding_name,review_backend=review_backend,review_backend_name=review_name,backends=backends,usage_recorder=usage_rec,timeout_seconds=request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS,max_parallel=getattr(coding_backend,'max_parallel',1),production=request.production,allow_fake_dependencies=request.allow_fake_dependencies)
        def _execute_recommendation(recobj, tool_use_id='recovery'):
            rec.record('recovery_deterministic_action_executed', payload=recobj.model_dump())
            res=execute_tool_with_policy(state,recobj.tool_name,recobj.tool_input or {},tool_use_id,_ctx())
            block={'type':'tool_result','tool_use_id':res.tool_use_id,'content':res.content,'is_error':res.is_error}; transcript.append(block); messages.append({'role':'tool','tool_call_id':res.tool_use_id,'content':str(res.content)}); rec.record('tool_result_appended',tool_name=res.tool_name,payload=block)
            return res
        messages=[initial_user_message(task=request.task,success_criteria=request.success_criteria,mode=request.mode,runner=request.runner,candidate_attempts=request.candidate_attempts,repo_path=request.repo_path,orchestrator=request.orchestrator)]
        system_prompt=SYSTEM_PROMPT + (ADAPTIVE_SYSTEM_APPENDIX if request.orchestrator=='adaptive' else '')
        for _ in range(self.max_turns):
            if state.is_terminal(): break
            rec.record('model_request_started',phase=state.phase)
            resp=self.client.create_message(backend=backend,messages=messages,system=system_prompt,tools=openai_tool_specs(adaptive=(request.orchestrator=='adaptive')),tool_choice='auto',strict=True)
            assistant_msg={'role':'assistant','content':resp.content,'raw_response':getattr(resp,'raw_response',{})}; transcript.append(assistant_msg)
            urec=usage_record_from_response(run_id=rid,phase=state.phase,role='orchestration',backend=backend,response=resp); usage_rec.record(urec); _update_usage_state(); usage_payload={k:getattr(urec,k) for k in ['input_tokens','output_tokens','total_tokens','total_cost','usage_source']}
            rec.record('model_response_received',payload={'finish_reason':getattr(resp,'finish_reason',None),'content':resp.content,'raw_response':getattr(resp,'raw_response',{}),**usage_payload})
            tool_calls=[b for b in resp.content if b.get('type')=='tool_use']
            if tool_calls:
                import json as _json
                text='\n'.join([b.get('text','') for b in resp.content if b.get('type')=='text']) or None
                messages.append({'role':'assistant','content':text,'tool_calls':[{'id':tc.get('id'),'type':'function','function':{'name':tc['name'],'arguments':_json.dumps(tc.get('input') or {})}} for tc in tool_calls]})
            if not tool_calls:
                rr=handle_no_tool_call(state,max_recovery_attempts=self.max_recovery_attempts)
                rec.record('recovery_injected',payload={'message':rr.message,'recommendation':rr.recommendation.model_dump() if rr.recommendation else None})
                if rr.recommendation and rr.recommendation.can_execute_deterministically and rr.recommendation.tool_name:
                    res=_execute_recommendation(rr.recommendation, 'recovery')
                    if not res.is_error:
                        state.recovery_count=0
                        state.turns_since_progress=0
                        state.save(run_dir/'state.json')
                    continue
                messages.append(rr.message); state.save(run_dir/'state.json')
                if rr.should_fail:
                    rec2=recommend_next_agentic_action(state)
                    if rec2.can_execute_deterministically and rec2.tool_name:
                        res=_execute_recommendation(rec2, 'no_progress_finalization')
                        if not res.is_error:
                            break
                    summary=_state_aware_no_progress_summary(state)
                    state.status='failed'; state.phase='failed'; state.final_decision={'decision':'failed','summary':summary,'blockers':['agentic_orchestrator_no_progress']}; rec.record('run_finalized',payload=state.final_decision); break
                continue
            for tc in tool_calls:
                res=execute_tool_with_policy(state,tc['name'],tc.get('input') or {},tc.get('id','tool'),_ctx())
                block={'type':'tool_result','tool_use_id':res.tool_use_id,'content':res.content,'is_error':res.is_error}; transcript.append(block); messages.append({'role':'tool','tool_call_id':res.tool_use_id,'content':str(res.content)}); rec.record('tool_result_appended',tool_name=res.tool_name,payload=block)
        if not state.is_terminal():
            rec2=recommend_next_agentic_action(state)
            if rec2.can_execute_deterministically and rec2.tool_name:
                _execute_recommendation(rec2, 'max_turn_finalization')
            if not state.is_terminal():
                state.status='failed'; state.phase='failed'; state.final_decision={'decision':'failed','summary':'max orchestration turns reached','blockers':['max_orchestration_turns_reached']}; rec.record('run_finalized',payload=state.final_decision)
        _update_usage_state()
        try:
            usage_rec.write_artifacts()
        except Exception as e:
            state.warnings.append(f'usage_summary_write_failed: {e}')
            rec.record('artifact_write_failed', payload={'path':str(run_dir/'usage.json'),'artifact_type':'usage_summary','error':str(e),'attempts':8})
            print('[agentic] Warning: usage summary write failed; usage.jsonl remains available')
        try:
            state.save(run_dir/'state.json'); rec.write_digest(state); write_artifacts(run_dir,state,rec.events(),transcript)
        except Exception as e:
            state.warnings.append(f'artifact_finalization_error: {e}')
            rec.record('artifact_write_failed', payload={'path':str(run_dir),'artifact_type':'final_artifacts','error':str(e),'attempts':8})
            print(f'[agentic] Warning: final artifact write failed: {e}')
        d=Decision(run_id=rid,accepted=state.status=='completed',mode=state.mode,runner=state.runner,orchestration_graph_path=str(run_dir/'orchestration_graph.json'),candidate_attempts_requested=state.candidate_attempts,candidate_attempts_completed=len(state.candidates),winning_attempt_id=(state.selection or {}).get('selected_attempt_id'),reason=(state.final_decision or {}).get('summary',''),decomposition_executed=state.decomposition_executed,subtask_count=len(state.subtasks),subtasks_executed=[s.subtask_id for s in state.subtasks if s.attempts],subtasks_accepted=[s.subtask_id for s in state.subtasks if s.status=='accepted'],attempts_per_subtask=state.candidate_attempts,subtask_attempts_completed=sum(len(s.attempts) for s in state.subtasks),failure_reason='' if state.status=='completed' else (state.final_decision or {}).get('summary','failed'))
        return OpsRunResult(run_id=rid,run_dir=str(run_dir),state=state,decision=d)
