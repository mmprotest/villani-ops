from __future__ import annotations
from typing import Any


def _get(attempt: Any, key: str, default=None):
    if isinstance(attempt, dict):
        return attempt.get(key, default)
    return getattr(attempt, key, default)


def is_attempt_acceptance_eligible(attempt: Any) -> tuple[bool, list[str]]:
    """Return whether an attempt may be accepted by the controller.

    Human approval is the only override for runner failures / uncertain reviews.
    """
    blockers: list[str] = []
    status = _get(attempt, "status")
    human = _get(attempt, "human_approval") or {}
    human_accept = isinstance(human, dict) and human.get("decision") == "accept"

    if human_accept and status == "human_approved":
        return True, []

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
