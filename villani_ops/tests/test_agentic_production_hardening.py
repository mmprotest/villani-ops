import pytest

pytestmark = pytest.mark.integration
from pathlib import Path

from villani_ops.agentic.tools import extract_changed_file_metadata, _set_acceptance_from_gate
from villani_ops.agentic.state import CandidateAttemptState, OpsRunState
from villani_ops.core.acceptance import is_attempt_acceptance_eligible


def test_deletion_only_diff_counts_as_changed_file(tmp_path):
    patch = tmp_path / "delete.patch"
    patch.write_text("""diff --git a/obsolete.txt b/obsolete.txt
deleted file mode 100644
index 3367afd..0000000
--- a/obsolete.txt
+++ /dev/null
@@ -1 +0,0 @@
-old
""")
    meta = extract_changed_file_metadata(patch.read_text())
    assert meta["changed_files"] == ["obsolete.txt"]
    assert meta["deleted_files"] == ["obsolete.txt"]
    attempt = CandidateAttemptState(
        attempt_id="candidate_001",
        status="reviewed",
        scope="candidate",
        patch_path=str(patch),
        changed_files=meta["changed_files"],
        deleted_files=meta["deleted_files"],
        review={"decision":"pass","recommended_action":"accept"},
        exit_code=0,
    )
    ok, blockers = is_attempt_acceptance_eligible(attempt)
    assert ok, blockers


def test_runner_failure_details_block_even_with_passing_review(tmp_path):
    patch = tmp_path / "diff.patch"
    patch.write_text("diff --git a/a.txt b/a.txt\nindex 7898192..6178079 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n")
    attempt = CandidateAttemptState(
        attempt_id="candidate_001",
        status="failed",
        scope="candidate",
        patch_path=str(patch),
        changed_files=["a.txt"],
        review={"decision":"pass","recommended_action":"accept"},
        exit_code=2,
        exit_reason="tests_failed",
        failure_reason="runner exited 2",
        runner_status="failed",
        runner_error_type=None,
    )
    ok, blockers = is_attempt_acceptance_eligible(attempt)
    assert not ok
    assert "runner_failed" in blockers
    assert "runner exit code is 2" in blockers
    assert attempt.failure_reason == "runner exited 2"


def test_integration_result_requires_review_and_subtasks(tmp_path):
    patch = tmp_path / "integration.patch"
    patch.write_text("diff --git a/a.txt b/a.txt\nindex 7898192..6178079 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n")
    state = OpsRunState(run_id="r", run_dir=str(tmp_path), repo_path=str(tmp_path), task="t", mode="m", runner="r", candidate_attempts=1, execution_path="decomposed_subtasks", decomposition_accepted=True)
    integration = {"attempt_id":"integration_001","scope":"integration","status":"completed","patch_path":str(patch),"changed_files":["a.txt"],"merge_conflicts":[],"review":None}
    ok, blockers = is_attempt_acceptance_eligible(integration, state=state)
    assert not ok
    assert "review_missing" in blockers

def test_binary_git_diff_extracts_only_real_path():
    diff = """diff --git a/path.bin b/path.bin
index 111..222 100644
Binary files a/path.bin and b/path.bin differ
"""
    meta = extract_changed_file_metadata(diff)
    assert meta["changed_files"] == ["path.bin"]
    assert meta["modified_files"] == ["path.bin"]
    for bad in ["differ:", "files", "a/path.bin", "b/path.bin"]:
        assert bad not in meta["changed_files"]


def test_legacy_binary_internal_format_extracts_path():
    meta = extract_changed_file_metadata("Binary files differ: rel\n")
    assert meta["changed_files"] == ["rel"]
    assert "differ:" not in meta["changed_files"]
