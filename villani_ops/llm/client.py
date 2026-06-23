from __future__ import annotations
from typing import Any
from pydantic import BaseModel
import httpx
from villani_ops.core.backend import Backend
from .json_extract import extract_json

class LLMCallResult(BaseModel):
    parsed_json: dict[str, Any]
    raw_text: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    backend_name: str
    model: str
    error: str | None = None

class LLMClient:
    def complete_json(self, backend: Backend, system_prompt: str, user_prompt: str, schema_name: str, timeout_seconds: int | None = None) -> LLMCallResult:
        if backend.provider not in {"openai-compatible","local","custom"}:
            raise ValueError(f"Backend provider '{backend.provider}' cannot be used for JSON LLM calls")
        if not backend.base_url:
            raise ValueError(f"Backend '{backend.name}' requires base_url for LLM calls")
        payload={"model": backend.model, "messages":[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}], "temperature":0}
        if backend.max_tokens: payload["max_tokens"]=backend.max_tokens
        headers={"Content-Type":"application/json"}
        key=backend.resolved_api_key()
        if key: headers["Authorization"]=f"Bearer {key}"
        url=backend.base_url.rstrip('/') + "/chat/completions"
        try:
            r=httpx.post(url, json=payload, headers=headers, timeout=timeout_seconds or backend.timeout_seconds or 60)
            r.raise_for_status(); data=r.json()
            raw=data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage=data.get("usage") or {}; inp=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0); out=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            parsed=extract_json(raw)
            return LLMCallResult(parsed_json=parsed, raw_text=raw, input_tokens=inp, output_tokens=out, estimated_cost=backend.estimate_cost(inp,out), backend_name=backend.name, model=backend.model)
        except Exception as e:
            raise RuntimeError(f"LLM JSON call failed for schema {schema_name} on backend {backend.name}: {e}") from e
