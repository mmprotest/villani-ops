from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field
from pathlib import Path
import json
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

class LLMReviewer:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def review(self, task: Task, classification: TaskClassification|None, coding_backend: Backend, attempt: dict[str,Any], backends: dict[str, Backend], out_path: str|Path|None=None) -> tuple[ReviewResult, LLMCallResult]:
        backend=select_backend(backends,'review')
        ctx={"task":task.model_dump(mode='json'),"classification":classification.model_dump(mode='json') if classification else None,"coding_backend":coding_backend.redacted_dict(),"attempt":attempt}
        result=self.client.complete_json(backend, SYSTEM, USER.format(context=json.dumps(ctx, indent=2)[:60000]), 'ReviewResult')
        review=ReviewResult.model_validate(result.parsed_json); review.reviewer_backend=backend.name
        if out_path: Path(out_path).write_text(review.model_dump_json(indent=2))
        return review, result
