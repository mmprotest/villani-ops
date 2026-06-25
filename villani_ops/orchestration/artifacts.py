from __future__ import annotations
import json
from pathlib import Path
from typing import Any

def write_text_utf8(path: str|Path, text: str) -> Path:
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return p

def write_json_utf8(path: str|Path, data: Any) -> Path:
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(data, 'model_dump'):
        data=data.model_dump(mode='json')
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    return p

def write_json(path: str|Path, data: Any) -> Path:
    return write_json_utf8(path, data)

def append_jsonl(path: str|Path, data: Any) -> None:
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(data, 'model_dump'):
        data=data.model_dump(mode='json')
    with p.open('a', encoding='utf-8') as f: f.write(json.dumps(data, ensure_ascii=False, default=str)+'\n')
