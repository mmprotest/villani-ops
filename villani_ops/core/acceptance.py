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


def _patch_readable(patch_path: Any) -> bool:
    if not patch_path:
        return False
    try:
        Path(patch_path).read_text(errors="replace")
        return True
    except Exception:
        return False


def attempt_requires_patch(state: Any | None, attempt: Any) -> bool:
    """Return whether acceptance requires patch and changed-file evidence.

    Villani Ops normally executes coding attempts, so absence of an explicit
    no-change/analysis-only classification means changes are expected.
    """
    classification = _get(attempt, "classification") or (_get(state, "classification") if state is not None else None) or {}
    if not isinstance(classification, dict):
        classification = getattr(classification, "model_dump", lambda **_: {})()
    category = str(classification.get("category") or classification.get("type") or "").lower()
    no_change = classification.get("requires_code_changes") is False or classification.get("code_change_expected") is False
    if no_change or category in {"analysis", "analysis_only", "no_change", "documentation_review"}:
        return False
    return True


def _validation_blockers(validation: Any) -> list[str]:
    blockers: list[str] = []
    if not validation:
        return blockers
    if not isinstance(validation, dict):
        validation = getattr(validation, "model_dump", lambda **_: {})()
    if validation.get("passed") is False:
        blockers.append("validation_failed")
    for item in validation.get("commands") or []:
        if not isinstance(item, dict):
            item = getattr(item, "model_dump", lambda **_: {})()
        status = str(item.get("status") or "").lower()
        if item.get("passed") is False or status == "failed":
            blockers.append("validation_failed")
        if status == "timeout":
            blockers.append("validation_timed_out")
        if status == "error":
            blockers.append("validation_error")
    return sorted(set(blockers))


def is_attempt_acceptance_eligible(attempt: Any, human_approval: Any | None = None, *, state: Any | None = None) -> tuple[bool, list[str]]:
    """Return whether an attempt may be accepted by the controller.

    Review approval is necessary but never sufficient: runner success,
    artifact evidence, validation evidence, and blocker state are enforced in
    one central gate. Human approval is the only structured override path.
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

    if attempt is None:
        return False, ["attempt_missing"]

    scope = _get(attempt, "scope")
    if scope == "subtask" and not _get(attempt, "subtask_id"):
        blockers.append("subtask_id_missing")
    if status in {None, "scheduled", "running"}:
        blockers.append("attempt_not_completed")
    elif status in {"failed", "rejected"}:
        blockers.append("runner_failed")
    elif status not in {"completed", "reviewed", "validated", "accepted", "human_approved"}:
        blockers.append(f"attempt_status_invalid:{status}")

    exit_code = _get(attempt, "exit_code")
    if exit_code is not None and exit_code != 0:
        blockers.append(f"runner exit code is {exit_code}")
    if _get(attempt, "error") or _get(attempt, "failure_reason"):
        blockers.append("runner_failed")

    if attempt_requires_patch(state, attempt):
        patch_path = _get(attempt, "patch_path")
        if not patch_path:
            blockers.append("missing_patch")
        elif not _patch_readable(patch_path):
            blockers.append("patch_unreadable")
        elif not has_non_empty_patch(patch_path):
            blockers.append("missing_patch")
        if not (_get(attempt, "changed_files") or []):
            blockers.append("empty_changed_files")

    review = _get(attempt, "review")
    if not review:
        blockers.append("review_missing")
        review = {}
    if isinstance(review, dict):
        if review.get("decision") != "pass":
            blockers.append("review_failed")
        if "passed" in review and review.get("passed") is not True:
            blockers.append("review_failed")
        if review.get("recommended_action") != "accept":
            blockers.append("review_failed")
        if review.get("issues"):
            blockers.append("review_blocking_issues")

    blockers.extend(_validation_blockers(_get(attempt, "validation")))
    return (not blockers), sorted(set(blockers))


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
