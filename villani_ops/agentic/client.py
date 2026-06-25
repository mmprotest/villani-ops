from __future__ import annotations
from pydantic import BaseModel, Field, ConfigDict
import httpx, os, uuid
class ToolMessageResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    content:list[dict]=Field(default_factory=list); raw_response:dict=Field(default_factory=dict); usage:dict=Field(default_factory=dict); model:str|None=None; finish_reason:str|None=None; provider_metadata:dict=Field(default_factory=dict)
class ToolCallingLLMClient:
    def create_message(self,backend,messages,system,tools,max_tokens=None,tool_choice=None,strict=True):
        if hasattr(backend,'create_message'): return backend.create_message(backend=backend,messages=messages,system=system,tools=tools,max_tokens=max_tokens,tool_choice=tool_choice,strict=strict)
        base=getattr(backend,'base_url',None) or os.getenv('OPENAI_BASE_URL','https://api.openai.com/v1')
        key=getattr(backend,'api_key',None) or os.getenv(getattr(backend,'api_key_env',None) or 'OPENAI_API_KEY','')
        model=getattr(backend,'model',None) or 'gpt-4o-mini'
        msgs=[{'role':'system','content':system if isinstance(system,str) else str(system)}]+messages
        payload={'model':model,'messages':msgs,'tools':tools}
        if max_tokens: payload['max_tokens']=max_tokens
        if tool_choice: payload['tool_choice']=tool_choice
        if not strict:
            for t in payload['tools']: t.get('function',{}).pop('strict',None)
        r=httpx.post(base.rstrip('/')+'/chat/completions',headers={'Authorization':f'Bearer {key}'},json=payload,timeout=60)
        if r.status_code>=400 and strict:
            return self.create_message(backend,messages,system,tools,max_tokens=max_tokens,tool_choice=None,strict=False)
        r.raise_for_status(); data=r.json(); ch=data['choices'][0]; msg=ch['message']; blocks=[]
        if msg.get('content'): blocks.append({'type':'text','text':msg['content']})
        for tc in msg.get('tool_calls') or []:
            import json
            blocks.append({'type':'tool_use','id':tc.get('id') or str(uuid.uuid4()),'name':tc['function']['name'],'input':json.loads(tc['function'].get('arguments') or '{}')})
        return ToolMessageResult(content=blocks,raw_response=data,usage=data.get('usage') or {},model=data.get('model'),finish_reason=ch.get('finish_reason'))
