import json
from typer.testing import CliRunner
import pytest

from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMClient, LLMCallError
from villani_ops.performance.models import InvestigationResult

class OutModel(InvestigationResult):
    pass

class Resp:
    def __init__(self, payload, status_code=200):
        self._payload=payload; self.status_code=status_code
    def json(self): return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(f"{self.status_code} response_format unsupported", request=None, response=self)


def ok_payload(obj):
    return {"choices":[{"message":{"content":json.dumps(obj)},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":4}}


def backend(provider="openai", **kw):
    return Backend(name="b", provider=provider, base_url=kw.pop("base_url", None), model="m", api_key="k", **kw)


def test_openai_provider_sends_json_schema_name_strict_and_validates(monkeypatch):
    seen={}
    def post(url, json, headers, timeout):
        seen.update(url=url, payload=json, headers=headers)
        return Resp(ok_payload({"summary":"s","validation_plan":{"commands":[]}}))
    monkeypatch.setattr("httpx.post", post)
    res=LLMClient().complete_structured(backend("openai"), "sys", "user", "InvestigationResult", InvestigationResult)
    rf=seen["payload"]["response_format"]
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "InvestigationResult"
    assert rf["json_schema"]["strict"] is True
    assert res.parsed_json["summary"] == "s"
    assert res.schema_validation_success is True
    assert res.schema_name == "InvestigationResult"
    assert res.structured_output_mode == "auto"


def test_openai_refusal_is_not_validation_success(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp({"choices":[{"message":{"content":"","refusal":"no"},"finish_reason":"stop"}]}))
    with pytest.raises(LLMCallError) as ei:
        LLMClient().complete_structured(backend("openai"), "s", "u", "InvestigationResult", InvestigationResult)
    assert ei.value.result.schema_validation_success is False
    assert "refusal" in (ei.value.result.schema_validation_error or "")


def test_openai_compatible_auto_retries_json_schema_to_json_object(monkeypatch):
    calls=[]
    def post(url, json, headers, timeout):
        calls.append(json["response_format"]["type"] if "response_format" in json else "none")
        if calls[-1] == "json_schema": return Resp({"error":"unsupported response_format"}, 400)
        return Resp(ok_payload({"summary":"s","validation_plan":{"commands":[]}}))
    monkeypatch.setattr("httpx.post", post)
    res=LLMClient().complete_structured(backend("openai-compatible", base_url="http://llm"), "s", "u", "InvestigationResult", InvestigationResult)
    assert calls == ["json_schema", "json_object"]
    assert res.structured_retry_used is True
    assert res.schema_validation_success is True


def test_openai_compatible_prompt_only_uses_extract_json(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp({"choices":[{"message":{"content":"prefix {\"summary\":\"s\",\"validation_plan\":{\"commands\":[]}} suffix"},"finish_reason":"stop"}]}))
    res=LLMClient().complete_structured(backend("openai-compatible", base_url="http://llm", structured_output_mode="prompt_only"), "s", "u", "InvestigationResult", InvestigationResult)
    assert res.parsed_json["summary"] == "s"
    assert res.structured_output_native is False
    assert res.schema_validation_success is True


def test_anthropic_native_messages_payload_and_text_blocks(monkeypatch):
    seen={}
    def post(url, json, headers, timeout):
        seen.update(url=url, payload=json, headers=headers)
        return Resp({"content":[{"type":"text","text":"{\"summary\":\"s\",\"validation_plan\":{\"commands\":[]}}"}],"stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":2}})
    monkeypatch.setattr("httpx.post", post)
    res=LLMClient().complete_structured(backend("anthropic"), "sys", "user", "InvestigationResult", InvestigationResult)
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["payload"]["output_config"]["format"]["type"] == "json_schema"
    assert res.parsed_json["summary"] == "s"
    assert res.schema_validation_success is True


def test_anthropic_max_tokens_structured_failure(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp({"content":[{"type":"text","text":"{}"}],"stop_reason":"max_tokens"}))
    with pytest.raises(LLMCallError) as ei:
        LLMClient().complete_structured(backend("anthropic"), "s", "u", "InvestigationResult", InvestigationResult)
    assert ei.value.result.schema_validation_success is False
    assert "max_tokens" in (ei.value.result.schema_validation_error or "")


def test_backend_cli_stores_lists_and_shows_structured_output_mode(tmp_path):
    runner=CliRunner()
    ws=str(tmp_path/"ws")
    r=runner.invoke(app,["backend","add","b","--provider","openai-compatible","--base-url","http://x","--model","m","--structured-output-mode","openai_json_schema","--workspace",ws])
    assert r.exit_code == 0, r.output
    r=runner.invoke(app,["backend","list","--workspace",ws])
    assert "openai_json_schema" in r.output
    r=runner.invoke(app,["backend","show","b","--workspace",ws])
    assert "openai_json_schema" in r.output


def test_missing_validation_plan_commands_reports_validation_not_keyerror(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp(ok_payload({"summary":"s","validation_plan":{"commands":"bad"}})))
    with pytest.raises(LLMCallError) as ei:
        LLMClient().complete_structured(backend("openai"), "s", "u", "InvestigationResult", InvestigationResult)
    msg=ei.value.result.schema_validation_error or ""
    assert "commands" in msg
    assert 'KeyError' not in msg and '"commands"' != msg
