from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field
import fnmatch, shutil, subprocess
from .artifacts import write_text_utf8

DEFAULT_PATCH_EXCLUDES = [
    '.villani','.villani/**','.villani_code','.villani_code/**','.git','.git/**',
    '__pycache__','**/__pycache__/**','.pytest_cache','.pytest_cache/**','.mypy_cache','.mypy_cache/**',
    '.ruff_cache','.ruff_cache/**','.coverage','coverage.xml','htmlcov','htmlcov/**',
    '.venv','.venv/**','venv','venv/**','node_modules','node_modules/**','*.pyc','*.pyo','*.log',
]
SCRATCH_ARTIFACT_PATTERNS = [
    '_fix.py','*_fix.py','fix_*.py','*_debug.py','debug*.txt','debug*.log',
    'test_debug.py','test_result.txt','test_output.txt','tmp_*.py',
    'scratch*.py','scratch*.txt','notes.txt','size.txt','stash_list.txt',
    'scripts/fix_*.py','scripts/*_fix.py',
]

class GitPatchCaptureResult(BaseModel):
    patch_path: str | None
    changed_files: list[str]
    added_files: list[str]
    deleted_files: list[str]
    modified_files: list[str]
    renamed_files: list[str]
    has_changes: bool
    diff_stat: str | None = None
    name_status: list[dict] = Field(default_factory=list)
    failure_reason: str | None = None


def _run(args, cwd: Path):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True)

def _is_excluded(path: str, patterns: list[str] | None = None) -> bool:
    p = path.replace('\\','/').lstrip('./')
    for pat in (patterns or DEFAULT_PATCH_EXCLUDES):
        q=pat.replace('\\','/').lstrip('./')
        if q.endswith('/**'):
            base=q[:-3].rstrip('/')
            if p == base or p.startswith(base + '/'): return True
        elif q.startswith('**/') and fnmatch.fnmatch(p, q): return True
        elif '/' not in q and (p == q or p.startswith(q + '/') or fnmatch.fnmatch(Path(p).name, q)): return True
        elif fnmatch.fnmatch(p, q): return True
    return False

def is_scratch_artifact_path(path: str) -> bool:
    p = path.replace('\\','/').lstrip('./')
    name = Path(p).name
    return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(name, pat) for pat in SCRATCH_ARTIFACT_PATTERNS)

def clean_untracked_scratch_artifacts(worktree_path: Path) -> list[str]:
    status = _run(['git','status','--porcelain','--untracked-files=all'], worktree_path)
    if status.returncode != 0: return []
    removed=[]
    for line in status.stdout.splitlines():
        if not line.startswith('?? '): continue
        rel=line[3:].strip()
        if rel and is_scratch_artifact_path(rel):
            p=worktree_path/rel
            if p.exists() and p.is_file():
                p.unlink(); removed.append(rel)
    return removed

def patch_contains_internal_artifacts(patch_path: str | Path | None) -> bool:
    if not patch_path: return False
    try: text=Path(patch_path).read_text(encoding='utf-8', errors='replace')
    except Exception: return True
    bad=('.villani', '.villani_code', 'context_state.json', 'mission_state.json', 'transcript', 'checkpoint')
    return any(b in text for b in bad)

def is_git_compatible_patch(patch_path: str | Path | None) -> bool:
    if not patch_path: return False
    try: text=Path(patch_path).read_text(encoding='utf-8', errors='replace').lstrip()
    except Exception: return False
    return text.startswith('diff --git ') and 'Added file:' not in text and 'Removed file:' not in text and 'Deleted file:' not in text

def clean_runner_artifacts_from_worktree(worktree_path: Path) -> None:
    for rel in ['.villani','.villani_code','.pytest_cache','.mypy_cache','.ruff_cache','__pycache__']:
        p=worktree_path/rel
        if p.exists():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    for p in worktree_path.rglob('__pycache__'):
        if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
    for p in list(worktree_path.rglob('*.pyc')) + list(worktree_path.rglob('*.pyo')) + list(worktree_path.rglob('*.log')):
        if '.git' not in p.parts and p.exists(): p.unlink()

def ensure_git_baseline(worktree_path: Path) -> None:
    if (worktree_path/'.git').exists(): return
    _run(['git','init'], worktree_path)
    _run(['git','config','user.email','villani-ops@example.invalid'], worktree_path)
    _run(['git','config','user.name','Villani Ops'], worktree_path)
    clean_runner_artifacts_from_worktree(worktree_path)
    _run(['git','add','-A'], worktree_path)
    _run(['git','commit','-m','baseline'], worktree_path)

def _parse_name_status(text: str):
    changed=[]; added=[]; deleted=[]; modified=[]; renamed=[]; rows=[]
    def add(lst,x):
        if x and x not in lst: lst.append(x)
    for line in text.splitlines():
        parts=line.split('\t')
        if not parts: continue
        status=parts[0]; row={'status':status,'paths':parts[1:]}; rows.append(row)
        code=status[0]; path=parts[-1] if len(parts)>1 else ''
        if _is_excluded(path): continue
        add(changed,path)
        if code=='A': add(added,path)
        elif code=='D': add(deleted,path)
        elif code=='R':
            entry=f'{parts[1]} -> {parts[2]}' if len(parts)>2 else path
            add(renamed,entry)
        elif code=='C': add(added,path)
        else: add(modified,path)
    return changed, added, deleted, modified, renamed, rows

def capture_git_patch(worktree_path: Path, patch_path: Path, *, exclude_patterns: list[str] | None = None) -> GitPatchCaptureResult:
    worktree_path=Path(worktree_path); patch_path=Path(patch_path); patch_path.parent.mkdir(parents=True, exist_ok=True)
    excludes=exclude_patterns or DEFAULT_PATCH_EXCLUDES
    try:
        clean_runner_artifacts_from_worktree(worktree_path)
        clean_untracked_scratch_artifacts(worktree_path)
        status=_run(['git','status','--porcelain'], worktree_path)
        if status.returncode != 0: return GitPatchCaptureResult(patch_path=None,changed_files=[],added_files=[],deleted_files=[],modified_files=[],renamed_files=[],has_changes=False,failure_reason=status.stderr.strip())
        # Stage non-excluded paths explicitly so untracked files are included and internals are never staged.
        candidates=[]
        for line in status.stdout.splitlines():
            rel=line[3:] if len(line)>3 else ''
            if ' -> ' in rel: rel=rel.split(' -> ',1)[-1]
            if rel and not _is_excluded(rel, excludes): candidates.append(rel)
        _run(['git','reset','--'], worktree_path)
        if candidates: _run(['git','add','-A','--',*candidates], worktree_path)
        diff=_run(['git','diff','--cached','--binary'], worktree_path)
        ns=_run(['git','diff','--cached','--name-status'], worktree_path)
        stat=_run(['git','diff','--cached','--stat'], worktree_path)
        _run(['git','reset','--'], worktree_path)
        write_text_utf8(patch_path, diff.stdout or '')
        changed, added, deleted, modified, renamed, rows = _parse_name_status(ns.stdout)
        return GitPatchCaptureResult(patch_path=str(patch_path) if diff.stdout.strip() else None,changed_files=changed,added_files=added,deleted_files=deleted,modified_files=modified,renamed_files=renamed,has_changes=bool(changed),diff_stat=stat.stdout or None,name_status=rows)
    except Exception as e:
        try: write_text_utf8(patch_path, '')
        except Exception: pass
        return GitPatchCaptureResult(patch_path=None,changed_files=[],added_files=[],deleted_files=[],modified_files=[],renamed_files=[],has_changes=False,failure_reason=f'{type(e).__name__}: {e}')
