from __future__ import annotations
import os
from typing import Any, Literal
from pydantic import BaseModel, Field

Provider = Literal["openai-compatible", "villani-code", "local", "custom"]
Role = Literal["coding", "classification", "review", "policy"]

class Backend(BaseModel):
    name: str
    provider: Provider
    base_url: str | None = None
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    roles: list[Role] = Field(default_factory=lambda: ["coding"])
    capability_score: int = 0
    max_tokens: int | None = None
    timeout_seconds: int | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)  # backward compatible
    command_name: str | None = None

    def resolved_api_key(self) -> str | None:
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return self.api_key

    def api_key_configured(self) -> bool:
        if self.api_key is not None and self.api_key != "":
            return True
        if self.api_key_env:
            return bool(os.environ.get(self.api_key_env))
        return False

    def api_key_status(self) -> str:
        if self.api_key is not None and self.api_key != "":
            return "direct_key_configured"
        if self.api_key_env:
            return "env_var_present" if os.environ.get(self.api_key_env) else "env_var_missing"
        return "missing"

    def redacted_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if data.get("api_key"):
            data["api_key"] = "***REDACTED***"
        return data

    def estimate_cost(self, input_tokens:int=0, output_tokens:int=0) -> float:
        return (input_tokens/1_000_000*self.input_cost_per_million) + (output_tokens/1_000_000*self.output_cost_per_million)

def select_backend(backends: dict[str, Backend], role: str) -> Backend:
    eligible=[b for b in backends.values() if b.enabled and role in b.roles]
    if not eligible:
        raise ValueError(f"No enabled backend configured for role '{role}'.")
    return sorted(eligible, key=lambda b: (-b.capability_score, b.output_cost_per_million, b.input_cost_per_million, b.name))[0]

def coding_backends(backends: dict[str, Backend]) -> list[Backend]:
    return [b for b in backends.values() if b.enabled and "coding" in b.roles]
