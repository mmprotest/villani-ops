from __future__ import annotations
from pathlib import Path
from typing import Any
import subprocess, json, re
from villani_ops.core.backend import Backend, select_backend
from villani_ops.core.task import Task, TaskClassification
from villani_ops.llm.client import LLMClient, LLMCallResult
from villani_ops.policy_engine.engine import _write_controller_error
from .prompts import SYSTEM, USER

_NORMALIZED_CLASSIFICATION_WARNING = "Classification failed validation after normalization, so Villani Ops used deterministic fallback classification."


def _repo_context(repo: Path) -> str:
    def run(args):
        try: return subprocess.run(args, cwd=repo, text=True, capture_output=True, timeout=5).stdout.strip()
        except Exception as e: return f"ERROR: {e}"
    files=[]
    for p in repo.rglob('*'):
        if len(files)>=200: break
        if p.is_file() and '.git' not in p.parts and '.villani-ops' not in p.parts:
            files.append(str(p.relative_to(repo)))
    package=[f for f in files if Path(f).name in {'pyproject.toml','package.json','Cargo.toml','go.mod','requirements.txt'}]
    return json.dumps({"repo_path":str(repo),"tree":files[:80],"package_files":package,"git_status":run(['git','status','--porcelain'])}, indent=2)


def _canonical_key(value: Any) -> str:
    text=str(value or "").strip().lower()
    text=re.sub(r"[/\\-]+", "_", text)
    text=re.sub(r"\s+", "_", text)
    text=re.sub(r"_+", "_", text)
    return text.strip("_")


def _normalize_enum(value: Any, mapping: dict[str, str], allowed: set[str], default: str) -> str:
    key=_canonical_key(value)
    if key in allowed: return key
    return mapping.get(key, default)


def _coerce_list(value: Any) -> list[str]:
    if value is None: return []
    if isinstance(value, str): return [value]
    if isinstance(value, list): return [str(item) for item in value if item is not None]
    return []


def _coerce_attempts(value: Any) -> int:
    if isinstance(value, bool): return 2
    try:
        attempts=int(value)
    except (TypeError, ValueError):
        return 2
    return max(1, min(attempts, 5))


def _coerce_needs_tests(value: Any) -> bool:
    if isinstance(value, bool): return value
    if isinstance(value, str):
        key=_canonical_key(value)
        if key in {"true", "yes", "needed"}: return True
        if key in {"false", "no", "not_needed"}: return False
    return True


def _coerce_confidence(value: Any) -> float:
    try:
        if isinstance(value, str):
            text=value.strip()
            if text.endswith("%"):
                conf=float(text[:-1].strip())/100
            else:
                conf=float(text)
        else:
            conf=float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(conf, 1.0))


def fallback_task_classification_payload() -> dict[str, Any]:
    return {"difficulty":"medium","category":"unknown","risk":"medium","estimated_attempts_needed":2,"needs_tests":True,"likely_files":[],"required_capabilities":[],"reasoning_summary":_NORMALIZED_CLASSIFICATION_WARNING,"confidence":0.0}


def normalize_task_classification_payload(raw: dict) -> dict:
    difficulty_map={"trivial":"easy","simple":"easy","straightforward":"easy","low":"easy","low_complexity":"easy","minor":"easy","moderate":"medium","intermediate":"medium","normal":"medium","standard":"medium","medium_complexity":"medium","complex":"hard","difficult":"hard","high":"hard","high_complexity":"hard","very_hard":"hard","very_difficult":"hard","challenging":"hard"}
    risk_map={"minimal":"low","minor":"low","low_risk":"low","moderate":"medium","medium_risk":"medium","low_medium":"medium","medium_low":"medium","low_to_medium":"medium","medium_to_low":"medium","significant":"high","severe":"high","critical":"high","high_risk":"high"}
    payload=dict(raw or {})
    payload["difficulty"]=_normalize_enum(payload.get("difficulty"), difficulty_map, {"easy","medium","hard"}, "medium")
    payload["risk"]=_normalize_enum(payload.get("risk"), risk_map, {"low","medium","high"}, "medium")
    payload["category"]=payload.get("category") if isinstance(payload.get("category"), str) and payload.get("category") else "unknown"
    payload["estimated_attempts_needed"]=_coerce_attempts(payload.get("estimated_attempts_needed"))
    payload["needs_tests"]=_coerce_needs_tests(payload.get("needs_tests"))
    payload["likely_files"]=_coerce_list(payload.get("likely_files"))
    payload["required_capabilities"]=_coerce_list(payload.get("required_capabilities"))
    payload["reasoning_summary"]=payload.get("reasoning_summary") if isinstance(payload.get("reasoning_summary"), str) else "Classification normalized from local model output."
    payload["confidence"]=_coerce_confidence(payload.get("confidence"))
    return payload


class TaskClassifier:
    def __init__(self, client: LLMClient | None=None): self.client=client or LLMClient()
    def select_backend(self, backends: dict[str, Backend]) -> Backend: return select_backend(backends, 'classification')
    def classify(self, task: Task, backends: dict[str, Backend], out_path: str|Path|None=None) -> tuple[TaskClassification, LLMCallResult]:
        backend=self.select_backend(backends); repo=Path(task.repo_path).resolve()
        context={"objective":task.objective,"success_criteria":task.success_criteria,"constraints":task.constraints,"repo":_repo_context(repo)}
        result=self.client.complete_json(backend, SYSTEM, USER.format(context=json.dumps(context, indent=2)), 'TaskClassification')
        normalized=normalize_task_classification_payload(result.parsed_json)
        try:
            cls=TaskClassification.model_validate(normalized)
        except Exception as e:
            fallback=fallback_task_classification_payload()
            _write_controller_error(Path(out_path).parent if out_path else None, 'classification', backend, 'TaskClassification', result, validation_error=str(e), normalized_payload=normalized, raw_payload=result.parsed_json, fallback_used=True, fallback_payload=fallback)
            result.error=_NORMALIZED_CLASSIFICATION_WARNING
            cls=TaskClassification.model_validate(fallback)
        if out_path: Path(out_path).write_text(cls.model_dump_json(indent=2))
        return cls, result
