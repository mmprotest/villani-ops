from __future__ import annotations
from pathlib import Path
from typing import Any
import json, yaml, shutil, difflib
from villani_ops.core.backend import Backend
from villani_ops.core.task import Task
from villani_ops.core.policy import Policy
from villani_ops.core.attempt import Attempt
from villani_ops.core.decision import Decision
from villani_ops.validation.base import ValidationResult
from villani_ops.isolation.base import EXCLUDED_DIRS

class FileStorage:
    def __init__(self, workspace: str | Path = ".villani-ops"):
        self.workspace = Path(workspace).expanduser().resolve()
    def init_workspace(self):
        self.workspace.mkdir(exist_ok=True); (self.workspace/"policies").mkdir(exist_ok=True); (self.workspace/"runs").mkdir(exist_ok=True)
        if not (self.workspace/"config.yaml").exists(): self.save_config({"runners":{"shell":{"command":None},"villani_code":{"command":None}}})
        if not (self.workspace/"backends.yaml").exists(): self.save_backends([])
    def load_config(self)->dict[str,Any]:
        p=self.workspace/"config.yaml"; return yaml.safe_load(p.read_text()) if p.exists() else {"runners":{}}
    def save_config(self,cfg): self.workspace.mkdir(exist_ok=True); (self.workspace/"config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    def load_backends(self)->dict[str,Backend]:
        p=self.workspace/"backends.yaml"; data=yaml.safe_load(p.read_text()) if p.exists() else []
        items=data.get("backends", data) if isinstance(data,dict) else data or []
        return {b["name"]: Backend.model_validate(b) for b in items}
    def save_backends(self, backends):
        vals=list(backends.values()) if isinstance(backends,dict) else backends
        self.workspace.mkdir(exist_ok=True); (self.workspace/"backends.yaml").write_text(yaml.safe_dump({"backends":[b.model_dump(mode="json") for b in vals]}, sort_keys=False))
    def create_run_dir(self, run_id):
        p=self.workspace/"runs"/run_id; (p/"attempts").mkdir(parents=True, exist_ok=True); return p
    def save_task(self, run_dir, task:Task): (Path(run_dir)/"task.json").write_text(task.model_dump_json(indent=2))
    def save_policy_snapshot(self, run_dir, policy:Policy): policy.save(Path(run_dir)/"policy.yaml")
    def save_attempt(self, attempt_dir, attempt:Attempt): (Path(attempt_dir)/"attempt.json").write_text(attempt.model_dump_json(indent=2))
    def save_validation(self, attempt_dir, validation:ValidationResult): (Path(attempt_dir)/"validation.json").write_text(validation.model_dump_json(indent=2))
    def save_decision(self, run_dir, decision:Decision): (Path(run_dir)/"decision.json").write_text(decision.model_dump_json(indent=2))
    def resolve_latest_run(self):
        runs=sorted((self.workspace/"runs").iterdir(), key=lambda p:p.stat().st_mtime, reverse=True) if (self.workspace/"runs").exists() else []
        return runs[0] if runs else None

def is_binary(path: Path)->bool:
    try: path.read_text(); return False
    except UnicodeDecodeError: return True

def capture_diff(original: str|Path, modified: str|Path, out: str|Path)->Path:
    original=Path(original).resolve(); modified=Path(modified).resolve(); out=Path(out); lines=[]
    def files(root):
        result={}
        for p in root.rglob("*"):
            if p.is_dir(): continue
            if any(part in EXCLUDED_DIRS for part in p.relative_to(root).parts): continue
            result[str(p.relative_to(root))]=p
        return result
    a,b=files(original),files(modified)
    for rel in sorted(set(a)|set(b)):
        if rel not in a: lines += [f"Added file: {rel}\n"]
        elif rel not in b: lines += [f"Deleted file: {rel}\n"]
        elif a[rel].read_bytes()==b[rel].read_bytes(): continue
        if rel in a and rel in b:
            if is_binary(a[rel]) or is_binary(b[rel]): lines += [f"Binary files differ: {rel}\n"]; continue
            lines += list(difflib.unified_diff(a[rel].read_text(errors="replace").splitlines(True), b[rel].read_text(errors="replace").splitlines(True), fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        elif rel not in a and not is_binary(b[rel]):
            lines += list(difflib.unified_diff([], b[rel].read_text(errors="replace").splitlines(True), fromfile=f"/dev/null", tofile=f"b/{rel}"))
        elif rel not in b and not is_binary(a[rel]):
            lines += list(difflib.unified_diff(a[rel].read_text(errors="replace").splitlines(True), [], fromfile=f"a/{rel}", tofile=f"/dev/null"))
    out.write_text("".join(lines)); return out
