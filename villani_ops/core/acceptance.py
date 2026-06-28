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




def _normalize_unit_interval(value: Any, *, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    if number < 0.0:
        return 0.0
    if number <= 1.0:
        return number
    if number <= 100.0:
        return number / 100.0
    return 1.0


def normalize_review_score(value: Any) -> float:
    """Normalize reviewer scores onto a single 0.0..1.0 comparison scale."""
    return _normalize_unit_interval(value, default=0.0)


def normalize_confidence(value: Any) -> float:
    """Normalize confidence values onto a single 0.0..1.0 comparison scale."""
    return _normalize_unit_interval(value, default=0.0)


def normalized_review_metrics(review: Any) -> dict[str, Any]:
    if not isinstance(review, dict):
        review = getattr(review, "model_dump", lambda **_: {})() if review is not None else {}
    raw_score = review.get("score") if isinstance(review, dict) else None
    raw_confidence = review.get("confidence") if isinstance(review, dict) else None
    return {
        "raw_review_score": raw_score,
        "normalized_review_score": normalize_review_score(raw_score),
        "raw_confidence": raw_confidence,
        "normalized_confidence": normalize_confidence(raw_confidence),
    }

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
        if status in {"inconclusive", "skipped", "skipped_no_reliable_command", "not_run"} and not validation_is_reliable(validation):
            return ["validation_unverified"]
        # Older validation payloads may include an auxiliary decision object
        # without an acceptance status; fall through to the legacy overall
        # status/command checks below for compatibility.
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
            blockers.append("review_blocking_issues")
        for key in ("blocking_issues", "fatal_issues", "severe_issues"):
            if review.get(key):
                blockers.append("review_blocking_issues")
        for issue in review.get("issues") or []:
            if isinstance(issue, dict) and (
                issue.get("blocking") is True
                or str(issue.get("severity") or "").lower() in {"blocking", "severe", "fatal"}
                or str(issue.get("type") or "").lower() in {"blocking", "blocker", "fatal"}
            ):
                blockers.append("review_blocking_issues")
                break

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

_SERIOUS_UNVERIFIED_BLOCKERS = {
    "missing_patch",
    "empty_changed_files",
    "runner_failed",
    "runner_exception",
    "patch_unreadable",
    "internal_artifacts_only",
    "scratch_artifact_in_patch",
    "patch_hygiene_failed",
    "patch_contains_internal_artifacts",
    "invalid_patch_format",
    "patch_apply_check_failed",
    "scope_failed",
    "validation_failed",
    "review_failed",
    "review_blocking_issues",
}

_EVIDENCE_RANK = {
    "authoritative": 5,
    "explicit_user_command": 5,
    "high_confidence_project_detected": 4,
    "project_test": 4,
    "generated_behavioral": 2,
    "generated_smoke": 1,
    "diagnostic_only": 0,
    "skipped": -1,
    "infrastructure_error": -2,
}


def serious_unverified_blockers(blockers: list[str] | tuple[str, ...] | None, attempt: Any = None) -> list[str]:
    serious: list[str] = []
    for blocker in blockers or []:
        b = str(blocker)
        if b in _SERIOUS_UNVERIFIED_BLOCKERS or b.startswith(("attempt_status_invalid", "runner exit code is")):
            serious.append(b)
    if attempt is not None:
        if not has_non_empty_patch(_get(attempt, "patch_path")):
            serious.append("missing_patch")
        if not (_get(attempt, "changed_files") or []):
            serious.append("empty_changed_files")
        exit_code = _get(attempt, "exit_code")
        if exit_code is not None and exit_code != 0:
            serious.append(f"runner exit code is {exit_code}")
    return sorted(set(serious))


def candidate_ranking_evidence(attempt: Any, *, state: Any | None = None) -> dict[str, Any]:
    review = _get(attempt, "review") or {}
    validation = _get(attempt, "validation") or {}
    metrics = normalized_review_metrics(review)
    try:
        eligible, blockers = is_attempt_acceptance_eligible(attempt, state=state)
    except Exception as exc:
        eligible, blockers = False, [f"acceptance_check_error:{type(exc).__name__}"]
    strength = validation_evidence_strength(validation)
    reliable = validation_is_reliable(validation)
    validation_status = str((_get(attempt, "validation_status") or (validation or {}).get("status") or "not_run")).lower()
    review_decision = review.get("decision") if isinstance(review, dict) else None
    serious = serious_unverified_blockers(blockers, attempt)
    if isinstance(review, dict):
        blocking_parts = list(review.get("blockers") or []) + list(review.get("blocking_issues") or []) + list(review.get("fatal_issues") or []) + list(review.get("severe_issues") or [])
        blocking_parts += [i for i in (review.get("issues") or []) if isinstance(i, dict) and (i.get("blocking") is True or str(i.get("severity") or "").lower() in {"blocking", "severe", "fatal"})]
        review_text = " ".join(str(x).lower() for x in blocking_parts)
        serious_markers = ("core requirement", "unimplemented", "runtime failure", "unsafe", "incomplete", "failed approach", "does not address", "missing required")
        if review_text and any(marker in review_text for marker in serious_markers):
            serious.append("review_identified_serious_requirement_or_runtime_risk")
            serious = sorted(set(serious))
    changed = _get(attempt, "changed_files") or []
    patch_ok = has_non_empty_patch(_get(attempt, "patch_path"))
    exit_code = _get(attempt, "exit_code")
    observations = [o for o in (getattr(state, "attempt_observations", []) or []) if getattr(o, "attempt_id", None) == _get(attempt, "attempt_id")] if state is not None else []
    latest_obs = observations[-1] if observations else None
    addressed_feedback = bool(latest_obs and (getattr(latest_obs, "should_repair", False) or getattr(latest_obs, "next_attempt_directives", None)))
    repeated_weak = bool(latest_obs and getattr(latest_obs, "should_retry_same_plan", False) and not addressed_feedback)
    reliable_failed = reliable and (((validation or {}).get("decision") or {}).get("status") == "failed" or validation_status in {"failed", "failed_candidate"})
    composite = (
        metrics["normalized_review_score"] * 100.0
        + metrics["normalized_confidence"] * 20.0
        + _EVIDENCE_RANK.get(strength, 0) * 8.0
        + (25.0 if eligible and reliable else 0.0)
        + (8.0 if validation_status == "passed" and reliable else 0.0)
        + (2.0 if validation_status == "passed" and not reliable else 0.0)
        + (3.0 if patch_ok else -40.0)
        + (2.0 if changed else -40.0)
        + (4.0 if exit_code in {None, 0} else -35.0)
        + (4.0 if review_decision == "pass" else -8.0)
        + (5.0 if addressed_feedback else 0.0)
        - (18.0 * len(serious))
        - (35.0 if reliable_failed else 0.0)
        - (8.0 if repeated_weak else 0.0)
    )
    return {**metrics, "attempt_id": _get(attempt, "attempt_id"), "composite_score": composite, "acceptance_eligible": eligible, "acceptance_blockers": blockers, "serious_blockers": serious, "validation_strength": strength, "validation_authoritative": reliable, "validation_status": validation_status, "patch_non_empty": patch_ok, "changed_files_present": bool(changed), "runner_exit_code": exit_code, "review_decision": review_decision, "addressed_prior_feedback": addressed_feedback, "repeated_weak_attempt": repeated_weak}


def candidate_ranking_key(attempt: Any, *, state: Any | None = None) -> tuple:
    ev = candidate_ranking_evidence(attempt, state=state)
    aid = str(ev.get("attempt_id") or "")
    try:
        idx = int(aid.rsplit("_", 1)[-1])
    except Exception:
        idx = 0
    return (ev["acceptance_eligible"] and ev["validation_authoritative"], ev["composite_score"], ev["normalized_review_score"], ev["normalized_confidence"], ev["validation_authoritative"], _EVIDENCE_RANK.get(ev["validation_strength"], 0), -len(ev["serious_blockers"]), ev["addressed_prior_feedback"], not ev["repeated_weak_attempt"], -idx)


def explain_candidate_selection(winner: Any, alternatives: list[Any], *, state: Any | None = None, limit: int = 3) -> dict[str, Any]:
    win = candidate_ranking_evidence(winner, state=state)
    ranked = sorted((candidate_ranking_evidence(a, state=state) for a in alternatives if _get(a, "attempt_id") != win.get("attempt_id")), key=lambda e: (e["composite_score"], e["normalized_review_score"], e["normalized_confidence"]), reverse=True)[:limit]
    reasons = [f"{win['attempt_id']} selected with normalized score {win['normalized_review_score']:.3f}, normalized confidence {win['normalized_confidence']:.3f}, validation strength {win['validation_strength']}, serious blockers {len(win['serious_blockers'])}."]
    for alt in ranked:
        if alt["composite_score"] > win["composite_score"]:
            excluded = alt.get("serious_blockers") or []
            if excluded:
                reasons.append(f"Higher-scored alternative {alt['attempt_id']} was excluded by serious blockers {excluded[:4]}; composite {win['composite_score']:.2f} vs {alt['composite_score']:.2f}.")
            else:
                reasons.append(f"Selected candidate did not beat {alt['attempt_id']} by composite score ({win['composite_score']:.2f} vs {alt['composite_score']:.2f}); this indicates a model-selected lower-ranked candidate unless selection was overridden.")
        else:
            reasons.append(f"Beat {alt['attempt_id']} because composite {win['composite_score']:.2f} vs {alt['composite_score']:.2f}; score {win['normalized_review_score']:.3f} vs {alt['normalized_review_score']:.3f}; confidence {win['normalized_confidence']:.3f} vs {alt['normalized_confidence']:.3f}; evidence {win['validation_strength']} vs {alt['validation_strength']}; serious blockers {len(win['serious_blockers'])} vs {len(alt['serious_blockers'])}.")
    if win["serious_blockers"]:
        reasons.append("Unresolved serious blockers: " + ", ".join(win["serious_blockers"][:6]))
    if not win["validation_authoritative"]:
        reasons.append("Selection is unverified; ranking does not upgrade validation to verified acceptance.")
    return {"winner": win, "nearest_alternatives": ranked, "reasons": reasons, "summary": " ".join(reasons)}
