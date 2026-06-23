from pathlib import Path
EXCLUDED_DIRS = {".git", ".villani-ops", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build"}

def ignore_names(_dir, names):
    return [n for n in names if n in EXCLUDED_DIRS]
