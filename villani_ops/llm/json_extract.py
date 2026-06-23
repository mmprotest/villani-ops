from __future__ import annotations
import json, re
from typing import Any

def extract_json(text: str) -> dict[str, Any]:
    candidates=[text.strip()]
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S|re.I):
        candidates.insert(0, m.group(1).strip())
    start=text.find('{'); end=text.rfind('}')
    if start!=-1 and end>start: candidates.append(text[start:end+1])
    last=None
    for c in candidates:
        try:
            val=json.loads(c)
            if isinstance(val, dict): return val
            raise ValueError("JSON root is not an object")
        except Exception as e: last=e
    raise ValueError(f"Could not parse JSON response: {last}")
