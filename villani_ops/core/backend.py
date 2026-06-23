from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field

Provider = Literal["openai-compatible", "local", "custom"]

class Backend(BaseModel):
    name: str
    provider: Provider
    base_url: str | None = None
    model: str
    input_cost_per_million: float
    output_cost_per_million: float
    env: dict[str, str] = Field(default_factory=dict)
