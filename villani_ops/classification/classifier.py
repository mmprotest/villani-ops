from __future__ import annotations
from pathlib import Path
from typing import Any
import subprocess, json, re
from villani_ops.core.backend import Backend, select_backend
from villani_ops.core.task import Task, TaskClassification
from villani_ops.llm.client import LLMClient, LLMCallResult
from villani_ops.policy_engine.engine import _write_controller_error
from .prompts import SYSTEM, USER
from .context import collect_relevant_file_snippets, RelevantFileSnippet, is_skipped_repo_file

_NORMALIZED_CLASSIFICATION_WARNING = "Classification failed validation after normalization, so Villani Ops used deterministic fallback classification."


def _repo_tree(repo: Path) -> list[str]:
    files=[]
    for p in repo.rglob('*'):
        if len(files)>=200: break
        if p.is_file():
            rel=str(p.relative_to(repo))
            if not is_skipped_repo_file(rel):
                files.append(rel)
    return files

def _repo_context(repo: Path) -> str:
    def run(args):
        try: return subprocess.run(args, cwd=repo, text=True, capture_output=True, timeout=5).stdout.strip()
        except Exception as e: return f"ERROR: {e}"
    files=_repo_tree(repo)
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



def _shape_signals(task_text: str, snippets: list[RelevantFileSnippet], likely_files: list[str]|None=None) -> dict[str, Any]:
    text=(task_text or '').lower()
    return {
        "relevant_file_count": len(snippets),
        "likely_file_count": len(likely_files or []),
        "explicit_tests_mentioned": bool(re.search(r"\b(pytest|tests?/|test_\w+|tests?)\b", text)),
        "failing_tests_mentioned": bool(re.search(r"\b(failing|failed|failure|regression)\b", text)),
        "do_not_change_tests": bool(re.search(r"do not (change|modify|edit) tests|don['’]t (change|modify|edit) tests", text)),
        "target_files_found": bool(snippets),
        "broad_change": bool(re.search(r"\b(entire app|architecture|redesign|migrate|rewrite|replatform)\b", text)),
    }

def _lower_level(value: str, levels: list[str]) -> str:
    try: i=levels.index(value)
    except ValueError: return value
    return levels[max(0, i-1)]

def adjust_classification_from_task_shape(classification: TaskClassification, task: str, relevant_files: list[RelevantFileSnippet]) -> TaskClassification:
    cls=classification.model_copy(deep=True)
    signals=_shape_signals(task, relevant_files, cls.likely_files)
    notes=list(cls.adjustment_notes)
    original_difficulty, original_risk=cls.difficulty, cls.risk
    narrow=signals["relevant_file_count"] <= 3 and signals["target_files_found"]
    tests_clear=signals["explicit_tests_mentioned"] or signals["failing_tests_mentioned"]
    if cls.confidence >= .5 and narrow and tests_clear and not signals["broad_change"]:
        new=_lower_level(cls.risk, ["low","medium","high"])
        if new != cls.risk:
            notes.append(f"Classification adjusted: risk {cls.risk} -> {new} because relevant context is narrow and explicit tests or success criteria are present.")
            cls.risk=new
    if cls.confidence >= .80 and signals["relevant_file_count"] <= 2 and tests_clear and not signals["broad_change"]:
        new=_lower_level(cls.difficulty, ["easy","medium","hard"])
        if new != cls.difficulty:
            notes.append(f"Classification adjusted: difficulty {cls.difficulty} -> {new} because relevant context is narrow, validation is explicit, and confidence is high.")
            cls.difficulty=new
    cls.adjustment_notes=notes
    cls.relevant_file_paths=[s.path for s in relevant_files]
    cls.task_shape_signals=signals
    cls.original_difficulty=original_difficulty
    cls.original_risk=original_risk
    return cls

class TaskClassifier:
    def __init__(self, client: LLMClient | None=None): self.client=client or LLMClient()
    def select_backend(self, backends: dict[str, Backend]) -> Backend: return select_backend(backends, 'classification')
    def classify(self, task: Task, backends: dict[str, Backend], out_path: str|Path|None=None) -> tuple[TaskClassification, LLMCallResult]:
        backend=self.select_backend(backends); repo=Path(task.repo_path).resolve()
        tree=_repo_tree(repo)
        task_text="\n".join(str(x or "") for x in [task.objective, task.instruction, task.success_criteria, "\n".join(task.constraints)])
        snippets=collect_relevant_file_snippets(repo, task_text, tree)
        relevant=[{"path":s.path,"reason":s.reason,"content_excerpt":s.content_excerpt} for s in snippets]
        context={"objective":task.objective,"success_criteria":task.success_criteria,"constraints":task.constraints,"repo":_repo_context(repo),"relevant_files":relevant}
        result=self.client.complete_json(backend, SYSTEM, USER.format(context=json.dumps(context, indent=2)), 'TaskClassification')
        normalized=normalize_task_classification_payload(result.parsed_json)
        try:
            cls=TaskClassification.model_validate(normalized)
            cls=adjust_classification_from_task_shape(cls, task_text, snippets)
        except Exception as e:
            fallback=fallback_task_classification_payload()
            _write_controller_error(Path(out_path).parent if out_path else None, 'classification', backend, 'TaskClassification', result, validation_error=str(e), normalized_payload=normalized, raw_payload=result.parsed_json, fallback_used=True, fallback_payload=fallback)
            result.error=_NORMALIZED_CLASSIFICATION_WARNING
            cls=TaskClassification.model_validate(fallback)
            cls=adjust_classification_from_task_shape(cls, task_text, snippets)
        if out_path: Path(out_path).write_text(cls.model_dump_json(indent=2))
        return cls, result
