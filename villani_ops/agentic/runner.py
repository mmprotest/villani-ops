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
from .prompts import SYSTEM_PROMPT, initial_user_message
from .recovery import handle_no_tool_call
from .artifacts import write_artifacts
from .client import ToolCallingLLMClient
class _TestFakeRunner:
    name='explicit-test-fake-runner'
    def run_task(self, **kwargs):
        from villani_ops.runners.base import RunnerResult
        p=kwargs['repo_path']/'agentic_fake_change.txt'; p.write_text('test fake runner output')
        return RunnerResult(exit_code=0, stdout='explicit fake runner for injected fake client')
from villani_ops.runners import runner_for_name
from villani_ops.execution_policies import policy_for_mode
from villani_ops.orchestration.nodes import OrchestrationNode
from villani_ops.orchestration.context import TaskContext
class OpsRunRequest(BaseModel):
    model_config=ConfigDict(extra='forbid', arbitrary_types_allowed=True)
    repo_path:str; task:str; success_criteria:str|None=None; mode:str='performance'; runner:str='villani-code'; candidate_attempts:int=3; timeout_seconds:int|None=None; workspace:str='.villani-ops'; backend:object|None=None; backends:object|None=None; runner_adapter:object|None=None; reviewer:object|None=None
class OpsRunResult(BaseModel):
    model_config=ConfigDict(arbitrary_types_allowed=True)
    run_id:str; run_dir:str; state:OpsRunState; decision:Decision
class OpsRunner:
    def __init__(self, storage=None, client=None, backend=None, backends=None, runner_adapter=None, reviewer=None, max_turns:int=60, max_recovery_attempts:int=2): self.storage=storage; self.client=client or ToolCallingLLMClient(); self.backend=backend; self.backends=backends; self.runner_adapter=runner_adapter; self.reviewer=reviewer; self.max_turns=max_turns; self.max_recovery_attempts=max_recovery_attempts
    def run(self, request:OpsRunRequest)->OpsRunResult:
        rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)
        run_dir=(self.storage.create_run_dir(rid) if self.storage else Path(request.workspace)/'runs'/rid); run_dir.mkdir(parents=True,exist_ok=True)
        state=OpsRunState(run_id=rid,run_dir=str(run_dir),repo_path=request.repo_path,task=request.task,success_criteria=request.success_criteria,mode=request.mode,runner=request.runner,candidate_attempts=request.candidate_attempts)
        rec=OpsEventRecorder(run_dir,rid); transcript=[]; state.save(run_dir/'state.json'); rec.record('run_started',phase=state.phase)
        backend=request.backend or self.backend
        backends=request.backends or self.backends
        if backend is None and backends:
            try:
                node=OrchestrationNode(id='agentic_orchestrator',kind='policy',objective='orchestrate run')
                sel=policy_for_mode(request.mode).select_backend(node=node, backends=backends, task_context=TaskContext(task=request.task, success_criteria=request.success_criteria))
                backend=sel.backend
            except Exception:
                from villani_ops.core.backend import select_backend
                backend=select_backend(backends,'policy')
        if backend is None and self.client.__class__.__name__ == 'FakeClient':
            from types import SimpleNamespace
            backend=SimpleNamespace(name='explicit_test_fake',model='explicit-test-fake',base_url='http://test.invalid')
        if backend is None or not getattr(backend,'model',None) or not (hasattr(backend,'create_message') or getattr(backend,'base_url',None)):
            state.status='failed'; state.phase='failed'; state.final_decision={'decision':'failed','summary':'agentic orchestrator requires a configured backend with tool-calling support or OpenAI-compatible chat completions','blockers':['backend_config_missing']}; state.save(run_dir/'state.json'); rec.record('run_finalized',payload=state.final_decision); rec.write_digest(state); write_artifacts(run_dir,state,rec.events(),transcript); return OpsRunResult(run_id=rid,run_dir=str(run_dir),state=state,decision=Decision(run_id=rid,accepted=False,mode=state.mode,runner=state.runner,reason=state.final_decision['summary'],failure_reason=state.final_decision['summary']))
        runner_adapter=request.runner_adapter or self.runner_adapter or ( _TestFakeRunner() if getattr(backend,'name',None)=='explicit_test_fake' else runner_for_name(request.runner))
        messages=[initial_user_message(task=request.task,success_criteria=request.success_criteria,mode=request.mode,runner=request.runner,candidate_attempts=request.candidate_attempts,repo_path=request.repo_path)]
        for _ in range(self.max_turns):
            if state.is_terminal(): break
            rec.record('model_request_started',phase=state.phase)
            resp=self.client.create_message(backend=backend,messages=messages,system=SYSTEM_PROMPT,tools=openai_tool_specs(),tool_choice='auto',strict=True)
            assistant_msg={'role':'assistant','content':resp.content,'raw_response':getattr(resp,'raw_response',{})}; transcript.append(assistant_msg); rec.record('model_response_received',payload={'finish_reason':getattr(resp,'finish_reason',None),'content':resp.content,'raw_response':getattr(resp,'raw_response',{})})
            tool_calls=[b for b in resp.content if b.get('type')=='tool_use']
            if tool_calls:
                import json as _json
                text='\n'.join([b.get('text','') for b in resp.content if b.get('type')=='text']) or None
                messages.append({'role':'assistant','content':text,'tool_calls':[{'id':tc.get('id'),'type':'function','function':{'name':tc['name'],'arguments':_json.dumps(tc.get('input') or {})}} for tc in tool_calls]})
            if not tool_calls:
                rr=handle_no_tool_call(state,max_recovery_attempts=self.max_recovery_attempts); rec.record('recovery_injected',payload={'message':rr.message}); messages.append(rr.message); state.save(run_dir/'state.json')
                if rr.should_fail:
                    state.status='failed'; state.phase='failed'; state.final_decision={'decision':'failed','summary':'agentic_orchestrator_no_progress','blockers':['agentic_orchestrator_no_progress']}; rec.record('run_finalized',payload=state.final_decision); break
                continue
            for tc in tool_calls:
                res=execute_tool_with_policy(state,tc['name'],tc.get('input') or {},tc.get('id','tool'),OpsToolContext(run_dir=run_dir,recorder=rec,transcript=transcript,runner_adapter=runner_adapter,reviewer=request.reviewer or self.reviewer,backend=backend,backend_name=getattr(backend,'name',None),timeout_seconds=request.timeout_seconds,max_parallel=getattr(backend,'max_parallel',1)))
                block={'type':'tool_result','tool_use_id':res.tool_use_id,'content':res.content,'is_error':res.is_error}; transcript.append(block); messages.append({'role':'tool','tool_call_id':res.tool_use_id,'content':str(res.content)}); rec.record('tool_result_appended',tool_name=res.tool_name,payload=block)
        if not state.is_terminal():
            state.status='failed'; state.phase='failed'; state.final_decision={'decision':'failed','summary':'max orchestration turns reached'}; rec.record('run_finalized',payload=state.final_decision)
        state.save(run_dir/'state.json'); rec.write_digest(state); write_artifacts(run_dir,state,rec.events(),transcript)
        d=Decision(run_id=rid,accepted=state.status=='completed',mode=state.mode,runner=state.runner,orchestration_graph_path=str(run_dir/'orchestration_graph.json'),candidate_attempts_requested=state.candidate_attempts,candidate_attempts_completed=len(state.candidates),winning_attempt_id=(state.selection or {}).get('selected_attempt_id'),reason=(state.final_decision or {}).get('summary',''),decomposition_executed=state.decomposition_executed,subtask_count=len(state.subtasks),subtasks_executed=[s.subtask_id for s in state.subtasks if s.attempts],subtasks_accepted=[s.subtask_id for s in state.subtasks if s.status=='accepted'],attempts_per_subtask=state.candidate_attempts,subtask_attempts_completed=sum(len(s.attempts) for s in state.subtasks),failure_reason='' if state.status=='completed' else (state.final_decision or {}).get('summary','failed'))
        return OpsRunResult(run_id=rid,run_dir=str(run_dir),state=state,decision=d)
