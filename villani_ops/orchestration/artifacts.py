from __future__ import annotations
import json
from pathlib import Path
from typing import Any

def write_json(path: str|Path, data: Any) -> Path:
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(data, 'model_dump'):
        data=data.model_dump(mode='json')
    p.write_text(json.dumps(data, indent=2))
    return p

def append_jsonl(path: str|Path, data: Any) -> None:
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(data, 'model_dump'):
        data=data.model_dump(mode='json')
    with p.open('a') as f: f.write(json.dumps(data)+'\n')
