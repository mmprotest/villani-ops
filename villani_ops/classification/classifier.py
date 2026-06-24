from __future__ import annotations
from pathlib import Path
import subprocess, json
from villani_ops.core.backend import Backend, select_backend
from villani_ops.core.task import Task, TaskClassification
from villani_ops.llm.client import LLMClient, LLMCallResult
from .prompts import SYSTEM, USER

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

class TaskClassifier:
    def __init__(self, client: LLMClient | None=None): self.client=client or LLMClient()
    def select_backend(self, backends: dict[str, Backend]) -> Backend: return select_backend(backends, 'classification')
    def classify(self, task: Task, backends: dict[str, Backend], out_path: str|Path|None=None) -> tuple[TaskClassification, LLMCallResult]:
        backend=self.select_backend(backends); repo=Path(task.repo_path).resolve()
        context={"objective":task.objective,"success_criteria":task.success_criteria,"constraints":task.constraints,"repo":_repo_context(repo)}
        result=self.client.complete_json(backend, SYSTEM, USER.format(context=json.dumps(context, indent=2)), 'TaskClassification')
        try:
            cls=TaskClassification.model_validate(result.parsed_json)
        except Exception as e:
            setattr(e, 'llm_result', result); setattr(e, 'schema_name', 'TaskClassification'); setattr(e, 'backend', backend)
            raise
        if out_path: Path(out_path).write_text(cls.model_dump_json(indent=2))
        return cls, result
