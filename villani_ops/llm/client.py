from __future__ import annotations
from typing import Any
from pydantic import BaseModel
import httpx, json
from villani_ops.core.backend import Backend
from .json_extract import extract_json
from .schema import model_json_schema_for_output, sanitize_json_schema_for_openai_strict, sanitize_json_schema_for_anthropic

class LLMCallError(RuntimeError):
    def __init__(self, message: str, result: "LLMCallResult | None" = None, parse_error: str | None = None):
        super().__init__(message)
        self.result = result
        self.parse_error = parse_error

class LLMCallResult(BaseModel):
    parsed_json: dict[str, Any]
    raw_text: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    backend_name: str
    model: str
    error: str | None = None
    url: str | None = None
    http_status: int | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = {}
    raw_response: dict[str, Any] = {}
    reasoning_content: str | None = None
    max_tokens: int | None = None
    provider: str | None = None
    schema_name: str | None = None
    structured_output_mode: str | None = None
    structured_output_native: bool = False
    schema_validation_success: bool = False
    schema_validation_error: str | None = None
    structured_retry_used: bool = False
    structured_fallback_reason: str | None = None

class LLMClient:
    def _base(self, backend: Backend, schema_name: str, raw: str, url: str, data: dict[str, Any], choice: dict[str, Any] | None, payload: dict[str, Any], estimate_cost: bool) -> dict[str, Any]:
        usage=data.get("usage") or {}; inp=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0); out=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        return dict(parsed_json={}, raw_text=raw, input_tokens=inp, output_tokens=out, estimated_cost=(backend.estimate_cost(inp,out) if estimate_cost else 0.0), backend_name=backend.name, model=backend.model, url=url, http_status=200, finish_reason=(choice or {}).get("finish_reason") or data.get("stop_reason"), usage=usage, raw_response=data, reasoning_content=((choice or {}).get("message") or {}).get("reasoning_content"), max_tokens=payload.get("max_tokens"), provider=backend.provider, schema_name=schema_name, structured_output_mode=backend.structured_output_mode)

    def _validate(self, parsed: dict[str, Any], model: type[BaseModel] | None) -> tuple[dict[str, Any], bool, str | None]:
        if model is None: return parsed, False, None
        try:
            v=model.model_validate(parsed)
            return v.model_dump(mode="json"), True, None
        except Exception as e:
            return parsed, False, str(e)

    def _chat_payload(self, backend, system_prompt, user_prompt):
        p={"model": backend.model, "messages":[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}], "temperature":0}
        if backend.max_tokens: p["max_tokens"]=backend.max_tokens
        return p

    def _headers(self, backend):
        h={"Content-Type":"application/json"}; key=backend.resolved_api_key()
        if key: h["Authorization"]=f"Bearer {key}"
        return h

    def complete_json(self, backend: Backend, system_prompt: str, user_prompt: str, schema_name: str, timeout_seconds: int | None = None, estimate_cost: bool = True) -> LLMCallResult:
        if backend.provider not in {"openai-compatible","local","custom"}:
            raise ValueError(f"Backend provider '{backend.provider}' cannot be used for JSON LLM calls")
        return self._prompt_only(backend, system_prompt, user_prompt, schema_name, None, timeout_seconds, estimate_cost, None)

    def complete_structured(self, backend: Backend, system_prompt: str, user_prompt: str, schema_name: str, output_model: type[BaseModel], timeout_seconds: int | None = None, estimate_cost: bool = True) -> LLMCallResult:
        mode=backend.structured_output_mode
        if mode in {"disabled", "prompt_only"}:
            return self._prompt_only(backend, system_prompt, user_prompt, schema_name, output_model, timeout_seconds, estimate_cost, "structured output disabled" if mode=="disabled" else "prompt_only mode")
        if backend.provider == "openai":
            return self._openai_schema(backend, system_prompt, user_prompt, schema_name, output_model, timeout_seconds, estimate_cost, backend.base_url or "https://api.openai.com/v1")
        if backend.provider == "anthropic":
            return self._anthropic_schema(backend, system_prompt, user_prompt, schema_name, output_model, timeout_seconds, estimate_cost)
        if backend.provider in {"openai-compatible","local","custom"}:
            if mode in {"auto", "openai_json_schema"}:
                try:
                    return self._openai_schema(backend, system_prompt, user_prompt, schema_name, output_model, timeout_seconds, estimate_cost, backend.base_url, native_provider=False)
                except Exception as e:
                    if mode != "auto" or not self._unsupported(e): raise
                    try:
                        r=self._openai_json_object(backend, system_prompt, user_prompt, schema_name, output_model, timeout_seconds, estimate_cost)
                        r.structured_retry_used=True; r.structured_fallback_reason=f"json_schema unsupported: {e}"
                        return r
                    except Exception as e2:
                        return self._prompt_only(backend, system_prompt, user_prompt, schema_name, output_model, timeout_seconds, estimate_cost, f"json_object fallback failed: {e2}")
            if mode == "openai_json_object":
                return self._openai_json_object(backend, system_prompt, user_prompt, schema_name, output_model, timeout_seconds, estimate_cost)
            return self._prompt_only(backend, system_prompt, user_prompt, schema_name, output_model, timeout_seconds, estimate_cost, f"unsupported structured_output_mode {mode}")
        raise ValueError(f"Backend provider '{backend.provider}' cannot be used for structured LLM calls")

    def _unsupported(self, e):
        text=str(e).lower(); return "400" in text or "response_format" in text or "unsupported" in text or isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 400

    def _post_chat(self, backend, payload, timeout, base_url):
        if not base_url: raise ValueError(f"Backend '{backend.name}' requires base_url for LLM calls")
        url=base_url.rstrip('/') + "/chat/completions"
        r=httpx.post(url, json=payload, headers=self._headers(backend), timeout=timeout or backend.timeout_seconds or 60); r.raise_for_status(); return url, r, r.json()

    def _openai_schema(self, backend, system, user, schema_name, model, timeout, estimate, base_url, native_provider=True):
        schema=sanitize_json_schema_for_openai_strict(model_json_schema_for_output(model)); payload=self._chat_payload(backend, system, user)
        payload["response_format"]={"type":"json_schema","json_schema":{"name":schema_name,"strict":backend.structured_output_strict,"schema":schema}}
        url,r,data=self._post_chat(backend,payload,timeout,base_url); choice=data.get("choices",[{}])[0]; msg=choice.get("message",{}); raw=msg.get("content") or ""
        if msg.get("refusal"):
            base=self._base(backend,schema_name,raw,url,data,choice,payload,estimate); base.update(http_status=r.status_code, structured_output_native=True, schema_validation_error=f"refusal: {msg.get('refusal')}", error="refusal")
            raise LLMCallError(f"Structured output refusal for schema {schema_name}: {msg.get('refusal')}", LLMCallResult(**base))
        return self._parse_native(backend,schema_name,raw,url,data,choice,payload,estimate,model,r.status_code)

    def _openai_json_object(self, backend, system, user, schema_name, model, timeout, estimate):
        payload=self._chat_payload(backend, system, user); payload["response_format"]={"type":"json_object"}
        url,r,data=self._post_chat(backend,payload,timeout,backend.base_url); choice=data.get("choices",[{}])[0]; raw=(choice.get("message",{}) or {}).get("content") or ""
        return self._parse_native(backend,schema_name,raw,url,data,choice,payload,estimate,model,r.status_code)

    def _anthropic_schema(self, backend, system, user, schema_name, model, timeout, estimate):
        base=backend.base_url or "https://api.anthropic.com/v1"; url=base.rstrip('/') + "/messages"; schema=sanitize_json_schema_for_anthropic(model_json_schema_for_output(model))
        payload={"model":backend.model,"system":system,"messages":[{"role":"user","content":user}],"max_tokens":backend.max_tokens or 4096,"temperature":0,"output_config":{"format":{"type":"json_schema","schema":schema,"name":schema_name}}}
        key=backend.resolved_api_key(); headers={"x-api-key":key or "","anthropic-version":"2023-06-01","content-type":"application/json"}
        r=httpx.post(url,json=payload,headers=headers,timeout=timeout or backend.timeout_seconds or 60); r.raise_for_status(); data=r.json(); raw="".join([b.get("text","") for b in data.get("content",[]) if b.get("type")=="text"])
        base_d=self._base(backend,schema_name,raw,url,data,None,payload,estimate); base_d["http_status"]=r.status_code; base_d["structured_output_native"]=True
        if data.get("stop_reason") == "max_tokens":
            base_d["schema_validation_error"]="Anthropic structured output stopped at max_tokens"; base_d["error"]="max_tokens"
            raise LLMCallError("Anthropic structured output failed: max_tokens", LLMCallResult(**base_d))
        return self._parse_native(backend,schema_name,raw,url,data,None,payload,estimate,model,r.status_code)

    def _parse_native(self, backend,schema_name,raw,url,data,choice,payload,estimate,model,status):
        base=self._base(backend,schema_name,raw,url,data,choice,payload,estimate); base.update(http_status=status, structured_output_native=True)
        try: parsed=json.loads(raw)
        except Exception as e:
            res=LLMCallResult(**{**base,"schema_validation_error":f"parse_error: {e}","error":str(e)})
            raise LLMCallError(f"Structured JSON parse failed for schema {schema_name}: {e}", res, str(e)) from e
        valid, ok, err=self._validate(parsed, model); base.update(parsed_json=valid, schema_validation_success=ok, schema_validation_error=err)
        if not ok:
            res=LLMCallResult(**base); raise LLMCallError(f"Structured schema validation failed for {schema_name}: {err}", res)
        return LLMCallResult(**base)

    def _prompt_only(self, backend, system, user, schema_name, model, timeout, estimate, reason):
        if backend.provider not in {"openai-compatible","local","custom"}: raise ValueError(f"Backend provider '{backend.provider}' cannot use prompt_only JSON calls")
        payload=self._chat_payload(backend,system,user); url,r,data=self._post_chat(backend,payload,timeout,backend.base_url); choice=data.get("choices",[{}])[0]; msg=choice.get("message",{}); raw=msg.get("content","") or ""; base=self._base(backend,schema_name,raw,url,data,choice,payload,estimate); base.update(http_status=getattr(r, "status_code", None), structured_fallback_reason=reason)
        try: parsed=extract_json(raw)
        except Exception as pe:
            result=LLMCallResult(**{**base,"schema_validation_error":f"parse_error: {pe}","error":str(pe)})
            raise LLMCallError(f"LLM JSON parse failed for schema {schema_name} on backend {backend.name}: {pe}", result=result, parse_error=str(pe)) from pe
        valid, ok, err=self._validate(parsed, model); base.update(parsed_json=valid, schema_validation_success=ok if model else False, schema_validation_error=err)
        return LLMCallResult(**base)


def complete_controller_json(client: Any, backend: Backend, system_prompt: str, user_prompt: str, schema_name: str, output_model: type[BaseModel], **kwargs: Any) -> LLMCallResult:
    # Preserve test/fake clients and legacy local fallback behavior: older fakes often
    # only patch/implement complete_json and may omit a runnable base_url.
    if backend.provider in {"openai-compatible", "local", "custom"} and not backend.base_url:
        return client.complete_json(backend, system_prompt, user_prompt, schema_name, **kwargs)
    structured = getattr(client, "complete_structured", None)
    if callable(structured):
        return structured(backend, system_prompt, user_prompt, schema_name, output_model, **kwargs)
    return client.complete_json(backend, system_prompt, user_prompt, schema_name, **kwargs)
