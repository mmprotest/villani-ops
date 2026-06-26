from __future__ import annotations
from pathlib import Path
import html, json
from importlib.resources import files
from .adapter import build_viewer_snapshot

def static_index_html() -> str:
    return (files('villani_ops.viewer.static') / 'index.html').read_text(encoding='utf-8')

def render_viewer_html(snapshot: dict|None=None) -> str:
    base=static_index_html()
    if snapshot is None: return base
    blob=html.escape(json.dumps(snapshot, ensure_ascii=False, default=str), quote=False)
    return base.replace('<script>\nconst embedded=', f'<script id="villani-run-snapshot" type="application/json">{blob}</script><script>\nconst embedded=', 1)

def write_offline_viewer(run_dir: Path) -> Path:
    run_dir=Path(run_dir); out=run_dir/'viewer'/'index.html'; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_viewer_html(build_viewer_snapshot(run_dir)), encoding='utf-8')
    return out
