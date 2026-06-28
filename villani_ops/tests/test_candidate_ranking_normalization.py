from pathlib import Path
from types import SimpleNamespace

from villani_ops.agentic.recovery import recommend_next_agentic_action
from villani_ops.agentic.state import CandidateAttemptState, OpsRunState
from villani_ops.core.acceptance import candidate_ranking_evidence, candidate_ranking_key, normalize_confidence, normalize_review_score


def make_state(tmp_path, attempts=5):
    repo = tmp_path / "repo"
    run = tmp_path / "run"
    repo.mkdir(exist_ok=True)
    run.mkdir(exist_ok=True)
    return OpsRunState(run_id="r", run_dir=str(run), repo_path=str(repo), task="fix", mode="performance", runner="villani-code", candidate_attempts=attempts, investigation={"summary": "i"}, plan={"strategy": "single_task"}, execution_path="single_task", phase="selecting")


def patch_file(tmp_path, name="p.diff"):
    p = tmp_path / name
    p.write_text("diff --git a/app.txt b/app.txt\n--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-a\n+b\n")
    return str(p)


def cand(tmp_path, aid, score, confidence=0.69, *, strength="generated_smoke", status="passed", changed=True, patch=True, review_decision="pass", action="accept", blockers=None, exit_code=0):
    a = CandidateAttemptState(attempt_id=aid, status="completed", scope="candidate", changed_files=["app.txt"] if changed else [], patch_path=patch_file(tmp_path, aid + ".diff") if patch else None, exit_code=exit_code)
    a.review = {"decision": review_decision, "recommended_action": action, "score": score, "confidence": confidence, "blockers": blockers or [], "issues": []}
    a.review_status = "passed" if review_decision == "pass" and action == "accept" and not blockers else "failed"
    a.validation = {"passed": status == "passed", "status": status, "evidence_strength": strength, "authoritative": strength in {"authoritative", "explicit_user_command", "high_confidence_project_detected", "project_test"}, "commands": []}
    if strength in {"authoritative", "explicit_user_command", "high_confidence_project_detected", "project_test"}:
        a.validation["decision"] = {"status": status, "scope": "candidate", "blocking_failures": [] if status == "passed" else [{"status": "failed_candidate"}]}
    a.validation_status = status
    a.validation_results = [a.validation]
    return a


def test_review_score_normalization():
    assert normalize_review_score(0.85) == 0.85
    assert normalize_review_score(85) == 0.85
    assert normalize_review_score(85.0) == 0.85
    assert normalize_review_score(100) == 1.0
    assert normalize_review_score(150) == 1.0
    assert normalize_review_score(-5) == 0.0
    assert normalize_review_score("bad") == 0.0
    assert normalize_review_score(None) == 0.0


def test_confidence_normalization():
    assert normalize_confidence(0.85) == 0.85
    assert normalize_confidence(85) == 0.85
    assert normalize_confidence(85.0) == 0.85
    assert normalize_confidence(100) == 1.0
    assert normalize_confidence(150) == 1.0
    assert normalize_confidence(-5) == 0.0
    assert normalize_confidence("bad") == 0.0
    assert normalize_confidence(None) == 0.0


def test_candidate_selection_compares_normalized_scores(tmp_path):
    s = make_state(tmp_path)
    c85 = cand(tmp_path, "candidate_001", 85.0)
    c90 = cand(tmp_path, "candidate_002", 0.9)
    s.candidates = [c85, c90]
    assert candidate_ranking_evidence(c85, state=s)["normalized_review_score"] == 0.85
    assert candidate_ranking_evidence(c90, state=s)["normalized_review_score"] == 0.9
    assert max(s.candidates, key=lambda c: candidate_ranking_key(c, state=s)).attempt_id == "candidate_002"
    assert candidate_ranking_key(c90, state=s) > candidate_ranking_key(c85, state=s)


def test_raw_85_does_not_automatically_beat_raw_point_9(tmp_path):
    s = make_state(tmp_path)
    s.candidates = [cand(tmp_path, "candidate_001", 85.0), cand(tmp_path, "candidate_002", 0.9)]
    rec = recommend_next_agentic_action(s)
    assert rec.tool_name == "ops_select_winner"
    assert rec.tool_input["selected_attempt_id"] == "candidate_002"


def test_equal_scores_use_blockers_and_evidence_quality(tmp_path):
    s = make_state(tmp_path)
    weak_blocked = cand(tmp_path, "candidate_001", 0.85, strength="generated_smoke", blockers=["core requirement unimplemented"], review_decision="fail", action="retry")
    reliable = cand(tmp_path, "candidate_002", 85.0, strength="project_test")
    s.candidates = [weak_blocked, reliable]
    assert max(s.candidates, key=lambda c: candidate_ranking_key(c, state=s)).attempt_id == "candidate_002"


def test_reliable_validation_pass_outranks_generated_smoke(tmp_path):
    s = make_state(tmp_path)
    s.candidates = [cand(tmp_path, "candidate_001", 0.95, strength="generated_smoke"), cand(tmp_path, "candidate_002", 0.90, strength="project_test")]
    assert max(s.candidates, key=lambda c: candidate_ranking_key(c, state=s)).attempt_id == "candidate_002"


def test_reliable_validation_failure_strongly_penalized(tmp_path):
    s = make_state(tmp_path)
    failed = cand(tmp_path, "candidate_001", 0.99, strength="project_test", status="failed")
    smoke = cand(tmp_path, "candidate_002", 0.80, strength="generated_smoke")
    s.candidates = [failed, smoke]
    assert candidate_ranking_key(smoke, state=s) > candidate_ranking_key(failed, state=s)


def test_no_patch_or_changed_files_not_best_when_alternative_exists(tmp_path):
    s = make_state(tmp_path)
    s.candidates = [cand(tmp_path, "candidate_001", 0.99, patch=False, changed=False), cand(tmp_path, "candidate_002", 0.5)]
    rec = recommend_next_agentic_action(s)
    assert rec.tool_input["selected_attempt_id"] == "candidate_002"


def test_no_usable_candidate_finalizes_no_usable(tmp_path):
    s = make_state(tmp_path, attempts=1)
    s.candidates = [cand(tmp_path, "candidate_001", 0.99, patch=False, changed=False)]
    rec = recommend_next_agentic_action(s)
    assert rec.tool_name == "ops_finalize_run"
    assert rec.tool_input["decision"] == "rejected"
    assert "no_usable_candidate" in rec.tool_input["blockers"]


def test_later_candidate_addressing_feedback_can_win_but_churn_does_not(tmp_path):
    s = make_state(tmp_path)
    early = cand(tmp_path, "candidate_001", 0.85)
    later = cand(tmp_path, "candidate_002", 0.85)
    s.attempt_observations = [SimpleNamespace(attempt_id="candidate_002", should_repair=True, next_attempt_directives=["address blocker"], should_retry_same_plan=False)]
    s.candidates = [early, later]
    assert max(s.candidates, key=lambda c: candidate_ranking_key(c, state=s)).attempt_id == "candidate_002"
    churn = cand(tmp_path, "candidate_003", 0.85)
    s.attempt_observations = [SimpleNamespace(attempt_id="candidate_003", should_repair=False, next_attempt_directives=[], should_retry_same_plan=True)]
    s.candidates = [early, churn]
    assert max(s.candidates, key=lambda c: candidate_ranking_key(c, state=s)).attempt_id == "candidate_001"


def test_final_selection_recommendation_includes_normalized_reasoning(tmp_path):
    s = make_state(tmp_path)
    s.candidates = [cand(tmp_path, "candidate_001", 85.0), cand(tmp_path, "candidate_002", 0.9)]
    rec = recommend_next_agentic_action(s)
    text = rec.tool_input["summary"] + " " + " ".join(rec.tool_input["reasons"])
    assert "normalized score" in text
    assert "normalized confidence" in text
    assert "Beat candidate_001" in text


def test_generated_smoke_passing_does_not_make_verified_but_explicit_and_project_do(tmp_path):
    s = make_state(tmp_path)
    smoke = cand(tmp_path, "candidate_001", 1.0, strength="generated_smoke")
    explicit = cand(tmp_path, "candidate_002", 0.8, strength="explicit_user_command")
    project = cand(tmp_path, "candidate_003", 0.8, strength="high_confidence_project_detected")
    assert not candidate_ranking_evidence(smoke, state=s)["validation_authoritative"]
    assert candidate_ranking_evidence(explicit, state=s)["validation_authoritative"]
    assert candidate_ranking_evidence(project, state=s)["validation_authoritative"]


def test_adaptive_single_task_and_agentic_decomposition_semantics_unchanged(tmp_path):
    adaptive = make_state(tmp_path)
    adaptive.orchestrator = "adaptive"
    adaptive.execution_path = "unknown"
    assert "ops_select_execution_path" in adaptive.allowed_next_actions()
    agentic = make_state(tmp_path)
    agentic.execution_path = "unknown"
    agentic.decomposition = {"subtasks": []}
    agentic.decomposition_validated = True
    agentic.decomposition_accepted = True
    assert agentic.orchestrator == "agentic"
