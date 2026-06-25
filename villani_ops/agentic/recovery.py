from pydantic import BaseModel
class RecoveryResult(BaseModel): should_fail:bool=False; message:dict
def handle_no_tool_call(state, reason='no_tool_call', max_recovery_attempts:int=2):
    state.recovery_count += 1
    if state.recovery_count>max_recovery_attempts:
        return RecoveryResult(should_fail=True,message={'role':'user','content':'RECOVERY FAILED: agentic_orchestrator_no_progress'})
    return RecoveryResult(message={'role':'user','content':'RECOVERY MODE:\nThe run is active but no valid progress occurred. You must call exactly one valid tool. Call ops_get_state if unsure. Do not respond in prose.'})
