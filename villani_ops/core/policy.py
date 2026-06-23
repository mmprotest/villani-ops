from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field
import yaml

class ObjectiveConfig(BaseModel):
    primary: str = "maximize_valid_solutions_per_dollar"
    secondary: list[str] = Field(default_factory=lambda: ["minimize_tokens", "minimize_attempts", "minimize_wall_time"])

class AttemptPlan(BaseModel):
    backend: str
    max_attempts: int = 1
    timeout_seconds: int = 900
    runner: str = "shell"

class ValidationConfig(BaseModel):
    mode: str = "diff_review"
    reviewer_backend: str | None = None
    require_diff_review: bool = True
    require_test_evidence: bool = False
    allow_human_override: bool = True

class StoppingConfig(BaseModel):
    stop_on_first_valid: bool = True
    stop_on_repeated_same_failure: bool = True

class SelectionConfig(BaseModel):
    choose_lowest_cost_valid_attempt: bool = False
    choose_best_valid_attempt: bool = True

class Policy(BaseModel):
    name: str
    objective: ObjectiveConfig = Field(default_factory=ObjectiveConfig)
    attempts: list[AttemptPlan] = Field(default_factory=list)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    stopping: StoppingConfig = Field(default_factory=StoppingConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(data)

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False))
