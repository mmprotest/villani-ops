from pydantic import BaseModel, Field, ConfigDict
class OpsEvent(BaseModel):
    model_config=ConfigDict(extra='forbid')
    event_id:str; run_id:str; timestamp:str; type:str; phase:str|None=None; tool_name:str|None=None; node_id:str|None=None; attempt_id:str|None=None; subtask_id:str|None=None; payload:dict=Field(default_factory=dict)
