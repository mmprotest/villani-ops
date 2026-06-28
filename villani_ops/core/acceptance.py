from __future__ import annotations
from typing import Any
from pathlib import Path
import fnmatch
def _is_excluded(path: str) -> bool:
    p=str(path).replace('\\','/')
    if p.startswith('./'): p=p[2:]
    return p.startswith(('.villani/', '.villani_code/')) or p in {'.villani','.villani_code'}

def _is_scratch(path: str) -> bool:
    p=str(path).replace('\\','/').lstrip('./')
    name=Path(p).name
    pats=['_fix.py','*_fix.py','fix_*.py','*_debug.py','debug*.txt','debug*.log','test_debug.py','test_result.txt','test_output.txt','tmp_*.py','scratch*.py','scratch*.txt','notes.txt','size.txt','stash_list.txt','scripts/fix_*.py','scripts/*_fix.py']
    return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(name, pat) for pat in pats)

def patch_contains_internal_artifacts(patch_path: Any) -> bool:
    try: text=Path(patch_path).read_text(errors='replace')
    except Exception: return True
    return any(x in text for x in ('.villani','.villani_code','context_state.json','mission_state.json','transcript','checkpoint'))

def is_git_compatible_patch(patch_path: Any) -> bool:
    try: text=Path(patch_path).read_text(errors='replace').lstrip()
    except Exception: return False
    return text.startswith('diff --git ') and 'Added file:' not in text and 'Removed file:' not in text and 'Deleted file:' not in text


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
        return ["validation_missing"]
    if not isinstance(validation, dict):
        validation = getattr(validation, "model_dump", lambda **_: {})()
    decision = validation.get("decision") or {}
    if decision:
        status = str(decision.get("status") or "").lower()
        if status == "passed":
            if not validation_is_reliable(validation):
                return ["validation_unverified"]
            return []
        if status == "failed":
            failures = decision.get("blocking_failures") or []
            command_rejected = any(str((f or {}).get("status") or "").lower() == "command_rejected" for f in failures if isinstance(f, dict))
            non_rejected = any(str((f or {}).get("status") or "").lower() != "command_rejected" for f in failures if isinstance(f, dict))
            if command_rejected:
                blockers.append("validation_command_rejected")
            if non_rejected or not command_rejected:
                blockers.append("validation_failed")
            return sorted(set(blockers))
        return []
    overall_status = str(validation.get("status") or "").lower()
    if overall_status in {"infrastructure_error", "skipped_no_reliable_command", "diagnostic_failed", "timeout", "inconclusive"}:
        return []
    if overall_status == "command_rejected":
        blockers.append("validation_command_rejected")
    elif validation.get("passed") is False:
        blockers.append("validation_failed")
    for item in validation.get("commands") or []:
        if not isinstance(item, dict):
            item = getattr(item, "model_dump", lambda **_: {})()
        status = str(item.get("status") or "").lower()
        if status == "command_rejected":
            blockers.append("validation_command_rejected")
            continue
        if status in {"infrastructure_error", "diagnostic_failed", "skipped_no_reliable_command"}:
            continue
        if item.get("blocking") is False:
            continue
        if item.get("passed") is False or status in {"failed", "failed_candidate"}:
            blockers.append("validation_failed")
        if status in {"timeout", "timed_out"}:
            blockers.append("validation_timed_out")
        if status in {"error", "infrastructure_error"}:
            blockers.append("validation_infrastructure_error")
    return sorted(set(blockers))

RELIABLE_VALIDATION_STRENGTHS = {
    "authoritative",
    "project_test",
    "explicit_user_command",
    "high_confidence_project_detected",
}
WEAK_VALIDATION_STRENGTHS = {
    "generated_behavioral",
    "generated_smoke",
    "diagnostic_only",
    "skipped",
    "infrastructure_error",
}


def validation_evidence_strength(validation: Any) -> str:
    """Classify validation evidence without assuming a language, OS, or benchmark."""
    if not validation:
        return "skipped"
    if not isinstance(validation, dict):
        validation = getattr(validation, "model_dump", lambda **_: {})()
    explicit = str(validation.get("evidence_strength") or validation.get("validation_strength") or "").lower()
    if explicit:
        return explicit
    status = str(validation.get("status") or "").lower()
    if status in {"infrastructure_error", "command_rejected", "timeout", "timed_out", "error"}:
        return "infrastructure_error"
    if status in {"skipped_no_reliable_command", "not_run"}:
        return "skipped"
    commands = validation.get("commands") or []
    strengths: list[str] = []
    for item in commands:
        if not isinstance(item, dict):
            item = getattr(item, "model_dump", lambda **_: {})()
        strength = str(item.get("evidence_strength") or item.get("validation_strength") or "").lower()
        source = str(item.get("source") or "").lower()
        authority = str(item.get("authority") or "").lower()
        confidence = str(item.get("confidence") or "").lower()
        blocking = item.get("blocking") is True or authority == "acceptance_blocking"
        item_status = str(item.get("status") or "").lower()
        if not strength:
            if item_status in {"infrastructure_error", "command_rejected", "timeout", "timed_out", "error"}:
                strength = "infrastructure_error"
            elif source in {"user_provided", "user_success_criteria", "final", "integration"}:
                strength = "explicit_user_command"
            elif source == "project_detected" and confidence == "high" and blocking:
                strength = "high_confidence_project_detected"
            elif source == "project_detected" and blocking:
                strength = "project_test"
            elif source == "generated" and confidence == "high" and blocking:
                strength = "generated_behavioral"
            elif source == "generated":
                strength = "generated_smoke"
            elif source in {"diagnostic", "exploratory", "runner_trace", "villani_code_debug_trace"} or authority == "diagnostic_only":
                strength = "diagnostic_only"
            elif blocking:
                strength = "project_test"
            else:
                strength = "diagnostic_only"
        strengths.append(strength)
    if any(s in RELIABLE_VALIDATION_STRENGTHS for s in strengths):
        order = ["authoritative", "explicit_user_command", "high_confidence_project_detected", "project_test"]
        return next(s for s in order if s in strengths)
    if any(s == "generated_behavioral" for s in strengths):
        return "generated_behavioral"
    if any(s == "generated_smoke" for s in strengths):
        return "generated_smoke"
    if any(s == "diagnostic_only" for s in strengths):
        return "diagnostic_only"
    if any(s == "infrastructure_error" for s in strengths):
        return "infrastructure_error"
    return "skipped"


def validation_is_reliable(validation: Any) -> bool:
    return validation_evidence_strength(validation) in RELIABLE_VALIDATION_STRENGTHS


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
    if scope == "integration":
        if _get(attempt, "failure_reason") == "agentic_subtask_integration_not_implemented":
            blockers.append("integration_not_implemented")
        if _get(attempt, "merge_conflicts"):
            blockers.append("merge_conflicts")
        if state is not None:
            for st in getattr(state, "subtasks", []) or []:
                st_status = _get(st, "status")
                if st_status in {"pending", "running"}:
                    blockers.append("subtasks_incomplete")
                elif st_status == "failed":
                    blockers.append("subtask_failed")
    if status in {None, "scheduled", "running"}:
        blockers.append("attempt_not_completed")
    elif status in {"failed", "rejected"}:
        blockers.append("runner_failed")
    elif status not in {"completed", "reviewed", "validated", "accepted", "human_approved"}:
        blockers.append(f"attempt_status_invalid:{status}")

    exit_code = _get(attempt, "exit_code")
    if exit_code is not None and exit_code != 0:
        blockers.append(f"runner exit code is {exit_code}")
    if _get(attempt, "runner_error_type"):
        blockers.append("runner_exception")
    if _get(attempt, "error") or _get(attempt, "failure_reason"):
        blockers.append("integration_failed" if scope == "integration" else "runner_failed")

    if attempt_requires_patch(state, attempt):
        patch_path = _get(attempt, "patch_path")
        if not patch_path:
            blockers.append("missing_patch")
        elif not _patch_readable(patch_path):
            blockers.append("patch_unreadable")
        elif not has_non_empty_patch(patch_path):
            blockers.append("missing_patch")
        changed_files = _get(attempt, "changed_files") or []
        if not changed_files:
            blockers.append("empty_changed_files")
        elif all(_is_excluded(str(f)) for f in changed_files):
            blockers.append("internal_artifacts_only")
        if any(_is_scratch(str(f)) for f in changed_files):
            blockers.extend(["scratch_artifact_in_patch","patch_hygiene_failed"])
        if patch_path and _patch_readable(patch_path):
            if patch_contains_internal_artifacts(patch_path):
                blockers.append("patch_contains_internal_artifacts")
            if not is_git_compatible_patch(patch_path):
                blockers.append("invalid_patch_format")
        hygiene = _get(attempt, "patch_hygiene") or {}
        if isinstance(hygiene, dict):
            if hygiene.get("apply_check_passed") is False:
                blockers.append("patch_apply_check_failed")
            if hygiene.get("contains_internal_artifacts") is True:
                blockers.append("patch_contains_internal_artifacts")
            if hygiene.get("format_valid") is False and patch_path:
                blockers.append("invalid_patch_format")
            if hygiene.get("scratch_artifacts_in_patch"):
                blockers.extend(["scratch_artifact_in_patch","patch_hygiene_failed"])
        scope_assessment = _get(attempt, "scope_assessment") or {}
        if isinstance(scope_assessment, dict):
            blockers.extend(scope_assessment.get("blockers") or [])

    review = _get(attempt, "review")
    review_status = _get(attempt, "review_status")
    if review_status in {"unavailable","malformed","provider_error"}:
        blockers.append("review_infrastructure_failed")
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
        if review.get("blockers"):
            blockers.append("review_failed")
        if review.get("issues"):
            blockers.append("review_blocking_issues")

    if state is not None and scope != "subtask":
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
