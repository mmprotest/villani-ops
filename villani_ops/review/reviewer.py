from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field
from pathlib import Path
import json, re
from villani_ops.core.backend import Backend, select_backend
from villani_ops.core.task import Task, TaskClassification
from villani_ops.llm.client import LLMClient, LLMCallResult
from .prompts import SYSTEM, USER

class ReviewResult(BaseModel):
    passed: bool=False
    score: float=0.0
    decision: Literal['pass','fail','uncertain']='fail'
    summary: str=''
    evidence: list[str]=Field(default_factory=list)
    issues: list[str]=Field(default_factory=list)
    recommended_action: Literal['accept','retry_same_backend','escalate','ask_human','fail']='fail'
    confidence: float=0.0
    requires_human_approval: bool=False
    reviewer_backend: str|None=None
    performance_backend: dict[str, str]|None=None

def _key(v: Any) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(v or '').strip().lower()).strip('_')

def _bool(v: Any, default: bool=False) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, str):
        k=_key(v)
        if k in {'true','yes','y','1'}: return True
        if k in {'false','no','n','0'}: return False
    return default

def _float(v: Any, default: float=0.0) -> float:
    try:
        if isinstance(v, str) and v.strip().endswith('%'):
            return float(v.strip()[:-1]) / 100.0
        return float(v)
    except Exception:
        return default

def normalize_review_payload(raw: dict) -> dict:
    data=dict(raw or {})
    pass_map={'accept','accepted','approve','approved','pass','passed','success','successful','valid'}
    fail_map={'reject','rejected','fail','failed','invalid','block','blocked'}
    uncertain_map={'unsure','unclear','maybe','needs_review','requires_human','human_review'}
    dk=_key(data.get('decision'))
    if dk in pass_map: decision='pass'
    elif dk in fail_map: decision='fail'
    elif dk in uncertain_map: decision='uncertain'
    elif 'passed' in data: decision='pass' if _bool(data.get('passed')) else 'fail'
    else: decision='uncertain'
    data['decision']=decision
    action_map={
        'accept_attempt':'accept','accept':'accept','approve':'accept','approved':'accept','use_this':'accept','ship':'accept',
        'retry':'retry_same_backend','try_again':'retry_same_backend','retry_same_backend':'retry_same_backend','rerun':'retry_same_backend',
        'escalate':'escalate','use_stronger_model':'escalate','stronger_model':'escalate','next_backend':'escalate',
        'ask_human':'ask_human','human':'ask_human','human_review':'ask_human','manual_review':'ask_human','needs_human':'ask_human',
        'reject':'fail','fail':'fail','stop':'fail',
    }
    ak=_key(data.get('recommended_action'))
    data['recommended_action']=action_map.get(ak) or ({'pass':'accept','fail':'fail','uncertain':'ask_human'}[decision])
    ev=data.get('evidence', [])
    if ev is None: ev=[]
    elif isinstance(ev, str): ev=[ev]
    elif isinstance(ev, list): ev=[json.dumps(x, sort_keys=True) if isinstance(x,(dict,list)) else str(x) for x in ev]
    else: ev=[json.dumps(ev, sort_keys=True) if isinstance(ev,(dict,list)) else str(ev)]
    data['evidence']=ev
    issues=data.get('issues', [])
    if issues is None: issues=[]
    elif isinstance(issues, str): issues=[issues]
    elif isinstance(issues, list): issues=[str(x) for x in issues]
    else: issues=[str(issues)]
    data['issues']=issues
    score=_float(data.get('score', 0.0))
    if score > 1.0: score = score / 10.0
    data['score']=max(0.0, min(1.0, score))
    data['confidence']=max(0.0, min(1.0, _float(data.get('confidence', 0.0))))
    data['requires_human_approval']=_bool(data.get('requires_human_approval'), False)
    if 'passed' not in data:
        data['passed'] = decision == 'pass'
    else:
        data['passed'] = _bool(data.get('passed'), decision == 'pass')
    data.setdefault('summary','')
    return data

class LLMReviewer:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def review(self, task: Task, classification: TaskClassification|None, coding_backend: Backend, attempt: dict[str,Any], backends: dict[str, Backend], out_path: str|Path|None=None, backend_override: Backend|None=None) -> tuple[ReviewResult, LLMCallResult]:
        backend=backend_override or select_backend(backends,'review')
        ctx={"task":task.model_dump(mode='json'),"classification":classification.model_dump(mode='json') if classification else None,"coding_backend":coding_backend.redacted_dict(),"attempt":attempt}
        result=self.client.complete_json(backend, SYSTEM, USER.format(context=json.dumps(ctx, indent=2)[:60000]), 'ReviewResult')
        normalized=normalize_review_payload(result.parsed_json)
        try:
            review=ReviewResult.model_validate(normalized)
        except Exception as e:
            setattr(e, 'llm_result', result); setattr(e, 'schema_name', 'ReviewResult'); setattr(e, 'backend', backend); setattr(e, 'normalized_payload', normalized)
            raise
        review.reviewer_backend=backend.name
        if backend_override is not None:
            review.performance_backend={'name': backend.name, 'model': backend.model}
        if out_path: Path(out_path).write_text(review.model_dump_json(indent=2))
        return review, result
