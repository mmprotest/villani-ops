from __future__ import annotations
from pathlib import Path
import shutil
from .base import ignore_names

class CopyIsolation:
    name = "copy"
    def create(self, source_repo: str | Path, destination: str | Path) -> Path:
        src, dst = Path(source_repo).resolve(), Path(destination)
        if dst.exists(): shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=ignore_names)
        return dst
