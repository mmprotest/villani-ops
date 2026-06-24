from __future__ import annotations
from pathlib import PurePosixPath
_GENERATED_DIRS={'__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','htmlcov','dist','build','node_modules'}
_GENERATED_SUFFIXES={'.pyc','.pyo'}
_GENERATED_NAMES={'.coverage','coverage.xml','.DS_Store','Thumbs.db'}
def is_generated_or_cache_path(path: str) -> bool:
    p=PurePosixPath(path.replace('\\','/'))
    if p.name in _GENERATED_NAMES or p.suffix in _GENERATED_SUFFIXES: return True
    if p.name.endswith('.egg-info') or any(part.endswith('.egg-info') for part in p.parts): return True
    return any(part in _GENERATED_DIRS for part in p.parts)
def split_generated_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    return ([p for p in paths if not is_generated_or_cache_path(p)], [p for p in paths if is_generated_or_cache_path(p)])
