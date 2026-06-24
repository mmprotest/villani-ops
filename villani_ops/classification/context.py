from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re

_SKIP_PARTS={'.git','.villani-ops','__pycache__','.pytest_cache','node_modules','dist','build','.venv','venv'}
_SKIP_SUFFIXES={'.pyc','.pyo','.so','.dll','.dylib','.png','.jpg','.jpeg','.gif','.webp','.pdf','.zip','.tar','.gz'}

@dataclass(frozen=True)
class RelevantFileSnippet:
    path: str
    reason: str
    content_excerpt: str


def is_skipped_repo_file(path: str|Path) -> bool:
    p=Path(path)
    if any(part in _SKIP_PARTS for part in p.parts):
        return True
    return p.suffix.lower() in _SKIP_SUFFIXES


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower()) if t not in {'the','and','for','with','from','this','that','task','test','tests','file','files','change','fix'}}


def _mentioned_paths(task: str) -> set[str]:
    vals=set()
    for m in re.findall(r"[\w./\\-]+\.[A-Za-z0-9]+", task):
        vals.add(m.strip('`"\''))
    return vals


def _read_excerpt(repo: Path, rel: str, max_bytes: int) -> str|None:
    p=repo/rel
    try:
        data=p.read_bytes()[:max_bytes]
        if b'\0' in data:
            return None
        return data.decode('utf-8', errors='replace')
    except OSError:
        return None


def collect_relevant_file_snippets(repo_path: Path, task: str, tree: list[str], likely_files: list[str]|None=None, max_files:int=6, max_bytes_per_file:int=8000, max_total_bytes:int=30000) -> list[RelevantFileSnippet]:
    repo=Path(repo_path)
    task_l=task.lower()
    toks=_tokens(task)
    mentions=_mentioned_paths(task)
    basenames={Path(m).name.lower() for m in mentions}
    likely=set(likely_files or [])
    candidates=[]
    def add(rel, score, reason):
        rel=rel.replace('\\','/')
        if rel not in tree or is_skipped_repo_file(rel): return
        candidates.append((score, rel, reason))
    for rel in tree:
        r=rel.replace('\\','/')
        low=r.lower(); base=Path(r).name.lower()
        if r in mentions or low in {m.lower() for m in mentions}: add(r,100,'path mentioned in task text')
        elif base in basenames: add(r,90,'basename mentioned in task text')
        elif r in likely or base in {Path(x).name for x in likely}: add(r,85,'likely file from classification metadata')
        overlap=len(toks & set(re.split(r"[^a-z0-9_]+", low)))
        if overlap: add(r,20+overlap,f'path contains {overlap} task token(s)')
    # failing/tests mention: include small number of tests matching task tokens
    if re.search(r"\b(failing|failed|pytest|tests?)\b", task_l):
        for rel in tree:
            low=rel.lower()
            if ('test' in Path(low).name or '/tests/' in '/'+low) and not is_skipped_repo_file(rel):
                overlap=len(toks & set(re.split(r"[^a-z0-9_]+", low)))
                add(rel, 50+overlap, 'test file inferred from test-related task text')
    # infer source siblings from test basenames: test_foo.py -> foo.py under non-test dirs
    mentioned_test_bases={Path(r).name.lower() for _,r,_ in candidates if 'test' in Path(r).name.lower()}
    source_bases={b.replace('test_','',1) for b in mentioned_test_bases} | {b.replace('_test','') for b in mentioned_test_bases}
    for rel in tree:
        base=Path(rel).name.lower(); low=rel.lower()
        if base in source_bases and '/test' not in '/'+low and not is_skipped_repo_file(rel):
            add(rel,80,'source file related to mentioned or inferred test file')
    seen={}; ordered=[]
    for score,rel,reason in sorted(candidates, key=lambda x:(-x[0], x[1])):
        if rel not in seen:
            seen[rel]=reason; ordered.append(rel)
    out=[]; total=0
    for rel in ordered:
        if len(out)>=max_files or total>=max_total_bytes: break
        excerpt=_read_excerpt(repo, rel, min(max_bytes_per_file, max_total_bytes-total))
        if excerpt is None: continue
        total += len(excerpt.encode('utf-8', errors='replace'))
        out.append(RelevantFileSnippet(rel, seen[rel], excerpt))
    return out
