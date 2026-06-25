from __future__ import annotations
from typing import Any
from pathlib import Path


def _get(attempt: Any, key: str, default=None):
    if isinstance(attempt, dict):
        return attempt.get(key, default)
    return getattr(attempt, key, default)


def has_non_empty_patch(patch_path: Any) -> bool:
    if not patch_path:
        return False
    try:
        return bool(Path(patch_path).read_text(errors="replace").strip())
    except Exception:
        return False


def is_attempt_acceptance_eligible(attempt: Any, human_approval: Any | None = None) -> tuple[bool, list[str]]:
    """Return whether an attempt may be accepted by the controller.

    Human approval is the only override for runner failures / uncertain reviews.
    """
    blockers: list[str] = []
    status = _get(attempt, "status")
    human = human_approval or _get(attempt, "human_approval") or {}
    if not isinstance(human, dict) and human is not None:
        human = getattr(human, "model_dump", lambda **_: {})()
    override_ok, override_blockers = human_override_blockers(attempt, human)
    if isinstance(human, dict) and human.get("decision") == "accept" and status == "human_approved":
        if override_ok:
            return True, []
        return False, override_blockers

    patch_path = _get(attempt, "patch_path")
    if not has_non_empty_patch(patch_path):
        blockers.append("patch is missing or empty")
    if not (_get(attempt, "changed_files") or []):
        blockers.append("changed files are missing")
    exit_code = _get(attempt, "exit_code")
    if exit_code != 0:
        blockers.append(f"runner exit code is {exit_code}")
    if _get(attempt, "error"):
        blockers.append(f"runner error: {_get(attempt, 'error')}")
    review = _get(attempt, "review")
    if not review:
        blockers.append("reviewer result is missing")
        review = {}
    if isinstance(review, dict):
        if review.get("decision") != "pass":
            blockers.append(f"review decision is {review.get('decision') or 'missing'}")
        if review.get("passed") is not True:
            blockers.append("review passed is not true")
        if review.get("recommended_action") != "accept":
            blockers.append(f"review recommended action is {review.get('recommended_action') or 'missing'}")
    if status not in {"validated", "human_approved"}:
        blockers.append(f"attempt status is {status or 'missing'}")
    return (not blockers), blockers


def human_override_blockers(attempt: Any, human_approval: Any | None = None) -> tuple[bool, list[str]]:
    """Strictly validate whether a human approval can override normal gates."""
    human = human_approval or _get(attempt, "human_approval") or {}
    if not isinstance(human, dict) and human is not None:
        human = getattr(human, "model_dump", lambda **_: {})()
    blockers: list[str] = []
    if not isinstance(human, dict) or not human:
        return False, ["human approval object is missing"]
    if human.get("decision") != "accept":
        blockers.append(f"human decision is {human.get('decision') or 'missing'}")
    if human.get("valid_override") is not True:
        blockers.append("human valid_override is not true")
    if human.get("requested") is not True:
        blockers.append("human approval was not requested")
    if human.get("prompted") is not True:
        blockers.append("human approval was not prompted")
    if human.get("skipped_reason") is not None:
        blockers.append(f"human approval was skipped: {human.get('skipped_reason')}")
    if not isinstance(human.get("request_reasons"), list) or not human.get("request_reasons"):
        blockers.append("human override requires non-empty request reasons")
    patch_path = _get(attempt, "patch_path")
    changed = _get(attempt, "changed_files") or []
    if not has_non_empty_patch(patch_path):
        blockers.append("human override requires a non-empty patch")
    if not changed:
        blockers.append("human override requires changed-file evidence")
    shown = human.get("shown_evidence")
    if not isinstance(shown, dict):
        blockers.append("human override requires shown evidence")
        shown = {}
    if not shown.get("patch_path"):
        blockers.append("shown evidence is missing patch path")
    if not shown.get("changed_files"):
        blockers.append("shown evidence is missing changed files")
    if not (shown.get("reviewer_summary") or shown.get("reviewer_decision")):
        blockers.append("shown evidence is missing reviewer summary or decision")
    if "acceptance_blockers" not in shown or shown.get("acceptance_blockers") is None:
        blockers.append("shown evidence is missing acceptance blockers")
    return not blockers, blockers
