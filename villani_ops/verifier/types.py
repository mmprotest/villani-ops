from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Literal
VerifierVerdict = Literal['success','failure','unclear','error']
VerifierAction = Literal['accept','retry_same_model','retry_higher_model','run_more_tests','inspect_manually']
@dataclass
class CommandRecord:
    ts:str|None=None; toolCallId:str|None=None; command:str|None=None; cwd:str|None=None; exitCode:int|None=None; stdout:str|None=None; stderr:str|None=None; truncated:bool=False; event:str|None=None; raw:Any=None; index:int=0
@dataclass
class ToolCallRecord:
    toolCallId:str|None=None; turnIndex:int|None=None; toolName:str|None=None; toolCategory:str|None=None; startedAt:str|None=None; endedAt:str|None=None; durationMs:int|None=None; status:str|None=None; args:Any=None; resultSummary:Any=None; error:Any=None; raw:Any=None; index:int=0
@dataclass
class PatchRecord: filePath:str|None=None; ok:bool|None=None; raw:Any=None; index:int=0
@dataclass
class ModelResponseRecord: text:str|None=None; raw:Any=None; index:int=0
@dataclass
class ValidationRecord: raw:Any=None; index:int=0
@dataclass
class EvidenceItem:
    kind:str; source:str; confidence:str; text:str; commandId:str|None=None; turnIndex:int|None=None; timestamp:str|None=None; order:int=0
@dataclass
class RequirementCheck:
    id:str; requirement:str; status:str='unclear'; evidence:list[EvidenceItem]=field(default_factory=list); risks:list[EvidenceItem]=field(default_factory=list)
@dataclass
class DebugRun:
    debugDir:str; runId:str|None=None; objective:str|None=None; repoFromMetadata:str|None=None; model:str|None=None; provider:str|None=None; status:str|None=None; startedAt:str|None=None; endedAt:str|None=None; durationMs:int|None=None; sessionMeta:Any=None; summary:Any=None; finalSummary:Any=None; commands:list[CommandRecord]=field(default_factory=list); toolCalls:list[ToolCallRecord]=field(default_factory=list); patches:list[PatchRecord]=field(default_factory=list); modelResponses:list[ModelResponseRecord]=field(default_factory=list); validations:list[ValidationRecord]=field(default_factory=list); parseWarnings:list[str]=field(default_factory=list); missingArtifacts:list[str]=field(default_factory=list)
def to_jsonable(x):
    if hasattr(x,'__dataclass_fields__'): return {k:to_jsonable(v) for k,v in asdict(x).items() if k!='raw'}
    if isinstance(x,list): return [to_jsonable(v) for v in x]
    if isinstance(x,dict): return {k:to_jsonable(v) for k,v in x.items()}
    return x
