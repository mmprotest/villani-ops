from __future__ import annotations
from copy import deepcopy
from typing import Any
from pydantic import BaseModel

_UNSUPPORTED = {"$schema", "examples", "default", "deprecated", "readOnly", "writeOnly"}

def model_json_schema_for_output(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    return deepcopy(schema)

def _sanitize(node: Any) -> Any:
    if isinstance(node, list):
        return [_sanitize(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in _UNSUPPORTED:
            continue
        if k == "anyOf":
            # Preserve nullable unions; sanitize children.
            out[k] = [_sanitize(x) for x in v if isinstance(x, dict)] if isinstance(v, list) else v
            continue
        if k in {"oneOf", "allOf"}:
            out[k] = [_sanitize(x) for x in v] if isinstance(v, list) else _sanitize(v)
            continue
        out[k] = _sanitize(v)
    typ = out.get("type")
    if typ == "object" or "properties" in out:
        out.setdefault("type", "object")
        out.setdefault("additionalProperties", False)
        props = out.get("properties")
        if isinstance(props, dict):
            out["properties"] = {name: _sanitize(prop) for name, prop in props.items()}
    if typ == "array" and "items" in out:
        out["items"] = _sanitize(out["items"])
    defs = out.get("$defs")
    if isinstance(defs, dict):
        out["$defs"] = {name: _sanitize(defn) for name, defn in defs.items()}
    return out

def sanitize_json_schema_for_openai_strict(schema: dict[str, Any]) -> dict[str, Any]:
    return _sanitize(deepcopy(schema))

def sanitize_json_schema_for_anthropic(schema: dict[str, Any]) -> dict[str, Any]:
    return _sanitize(deepcopy(schema))
