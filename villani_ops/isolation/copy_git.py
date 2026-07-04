from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from villani_ops.agentic.git_artifacts import (
    DEFAULT_PATCH_EXCLUDES,
    GitPatchCaptureResult,
    capture_git_patch,
    ensure_git_baseline,
)


@dataclass(frozen=True)
class CopiedGitCandidate:
    source_repo: Path
    candidate_dir: Path
    worktree_path: Path
    patch_path: Path


def source_is_git_repo(path: Path) -> bool:
    """Return True when path is inside a valid Git worktree without mutating it."""
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=Path(path),
        text=True,
        capture_output=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def copy_worktree(src: Path, dst: Path, *, excludes: list[str] | None = None) -> None:
    """Copy a repository/project tree while excluding VCS and local runner artifacts."""
    patterns = [".git", ".villani-ops", ".v", "__pycache__"]
    if excludes:
        patterns.extend(excludes)
    ignore = shutil.ignore_patterns(*patterns)
    shutil.copytree(Path(src), Path(dst), ignore=ignore, dirs_exist_ok=True)


def create_git_baselined_copy(
    source_repo: Path,
    candidate_dir: Path,
    *,
    excludes: list[str] | None = None,
) -> CopiedGitCandidate:
    """
    Copy source_repo into candidate_dir / "worktree" and initialize a temporary Git baseline.

    The source directory is never mutated; Git is initialized only in the copied worktree.
    """
    source_repo = Path(source_repo).resolve()
    candidate_dir = Path(candidate_dir).resolve()
    worktree_path = candidate_dir / "worktree"
    patch_path = candidate_dir / "diff.patch"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    copy_worktree(source_repo, worktree_path, excludes=excludes)
    ensure_git_baseline(worktree_path)
    return CopiedGitCandidate(
        source_repo=source_repo,
        candidate_dir=candidate_dir,
        worktree_path=worktree_path,
        patch_path=patch_path,
    )


def capture_candidate_patch(
    worktree_path: Path,
    patch_path: Path,
    *,
    excludes: list[str] | None = None,
) -> GitPatchCaptureResult:
    """Capture a candidate patch using the adaptive Git artifact implementation."""
    return capture_git_patch(
        Path(worktree_path),
        Path(patch_path),
        exclude_patterns=excludes or DEFAULT_PATCH_EXCLUDES,
    )
