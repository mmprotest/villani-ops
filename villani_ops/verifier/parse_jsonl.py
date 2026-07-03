from __future__ import annotations
import json
from pathlib import Path
from typing import Any
def parse_jsonl(path: Path, *, optional: bool=False) -> tuple[list[Any], list[str], bool]:
    if not path.exists():
        if optional: return [], [], False
        raise FileNotFoundError(str(path))
    records=[]; warnings=[]
    text=path.read_text(encoding='utf-8')
    for i,line in enumerate(text.splitlines(),1):
        if not line.strip(): continue
        try: records.append(json.loads(line))
        except json.JSONDecodeError as e: warnings.append(f'{path.name}:{i}: malformed JSONL skipped: {e.msg}')
    return records,warnings,True
