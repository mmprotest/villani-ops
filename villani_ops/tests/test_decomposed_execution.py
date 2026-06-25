import json
from pathlib import Path

from villani_ops.core.decision import Decision
from villani_ops.core.task import Task
from villani_ops.orchestration.graph import OrchestrationGraph
from villani_ops.orchestration.nodes import OrchestrationNode
from villani_ops.performance.report import write_performance_report


def test_decomposed_graph_shape_has_subtask_and_integration_nodes():
    nodes = [
        OrchestrationNode(id="classify", kind="classify", objective="c"),
        OrchestrationNode(id="investigate", kind="investigate", objective="i", dependencies=["classify"]),
        OrchestrationNode(id="plan", kind="plan", objective="p", dependencies=["investigate"]),
        OrchestrationNode(id="decompose", kind="decompose", objective="d", dependencies=["plan"]),
        OrchestrationNode(id="subtask_fix_a_code", kind="code", objective="a", dependencies=["decompose"]),
        OrchestrationNode(id="subtask_fix_a_review", kind="review", objective="ar", dependencies=["subtask_fix_a_code"]),
        OrchestrationNode(id="subtask_fix_b_code", kind="code", objective="b", dependencies=["subtask_fix_a_review"]),
        OrchestrationNode(id="subtask_fix_b_review", kind="review", objective="br", dependencies=["subtask_fix_b_code"]),
        OrchestrationNode(id="integrate_subtasks", kind="integrate", objective="merge", dependencies=["subtask_fix_a_review", "subtask_fix_b_review"]),
        OrchestrationNode(id="integration_validate", kind="integration_validate", objective="validate", dependencies=["integrate_subtasks"]),
        OrchestrationNode(id="final_review", kind="final_review", objective="review", dependencies=["integration_validate"]),
        OrchestrationNode(id="verify", kind="verify", objective="verify", dependencies=["final_review"]),
    ]
    graph = OrchestrationGraph(nodes=nodes)
    ids = {n.id for n in graph.nodes}
    assert {"subtask_fix_a_code", "subtask_fix_a_review", "integrate_subtasks", "integration_validate", "final_review", "verify"} <= ids
    assert "code_attempt_001" not in ids


def test_decision_records_integrated_decomposition_winner(tmp_path):
    decision = Decision(
        run_id="r",
        accepted=True,
        decomposition_executed=True,
        decomposition_advisory_only=False,
        subtask_count=2,
        subtasks_executed=["fix_a", "fix_b"],
        subtasks_accepted=["fix_a", "fix_b"],
        selected_attempt_id="integrated_decomposition",
        winning_attempt_id="integrated_decomposition",
        winning_patch_path=str(tmp_path / "integration" / "final.patch"),
        winning_worktree_path=str(tmp_path / "worktrees" / "r" / "integration"),
        integration_worktree_path=str(tmp_path / "worktrees" / "r" / "integration"),
        integration_patch_path=str(tmp_path / "integration" / "final.patch"),
        integration_validation={"passed": True, "command": ["python", "-m", "pytest", "-q"], "exit_code": 0},
        final_review={"decision": "pass", "recommended_action": "accept", "score": 0.95},
    )
    data = json.loads(decision.model_dump_json())
    assert data["decomposition_executed"] is True
    assert data["decomposition_advisory_only"] is False
    assert data["selected_attempt_id"] == "integrated_decomposition"
    assert data["winning_patch_path"].endswith("integration/final.patch")


def test_report_has_decomposed_execution_section(tmp_path):
    decision = Decision(
        run_id="r",
        accepted=True,
        decomposition={
            "should_use_decomposition": True,
            "advisory_only": False,
            "subtasks": [{"id": "fix_a", "title": "Fix A", "objective": "Fix A"}],
        },
        decomposition_executed=True,
        subtask_count=1,
        subtasks_accepted=["fix_a"],
        attempts=[
            {
                "attempt_id": "subtask_fix_a",
                "subtask_id": "fix_a",
                "status": "validated",
                "changed_files": ["a.py"],
                "review": {"decision": "pass", "recommended_action": "accept"},
                "patch_path": "subtasks/fix_a/patch.diff",
            }
        ],
        integration_validation={"passed": True, "command": ["python", "-m", "pytest", "-q"]},
        integration_repair_used=False,
        integration_patch_path="integration/final.patch",
        final_review={"decision": "pass", "recommended_action": "accept", "score": 0.95},
    )
    path = write_performance_report(tmp_path, Task(repo_path=str(tmp_path), objective="Fix"), None, [], None, decision, 0)
    text = path.read_text()
    assert "## Decomposed Execution" in text
    assert "Decomposition executed: true" in text
    assert "| fix_a | validated |" in text
    assert "| 1 | pass/accept | true |" in text
    assert "Validation passed: true" in text


def test_advisory_only_report_is_explicit(tmp_path):
    decision = Decision(
        run_id="r",
        decomposition={"should_use_decomposition": True, "advisory_only": True, "subtasks": [{"id": "fix_a", "title": "Fix A"}]},
        decomposition_executed=False,
        decomposition_advisory_only=True,
        subtask_count=1,
    )
    path = write_performance_report(tmp_path, Task(repo_path=str(tmp_path), objective="Fix"), None, [], None, decision, 0)
    assert "advisory-only and does not count as active decomposition" in path.read_text()


def test_decomposition_zero_subtask_fallback_progress_is_explicit(capsys):
    from types import SimpleNamespace
    from villani_ops.orchestration.progress import ConsoleProgressReporter
    reporter=ConsoleProgressReporter()
    node=SimpleNamespace(kind="decompose")
    reporter.node_completed(node, {"should_use_decomposition": True, "subtasks": [], "decomposition_fallback_to_candidate_path": True, "decomposition_fallback_reason": "Planner requested decomposition but decomposition produced no executable subtasks."})
    out=capsys.readouterr().out
    assert "Decomposition fallback to candidate path: no executable subtasks produced" in out
    assert "Decomposition complete" not in out


def test_report_records_decomposition_fallback_reason(tmp_path):
    decision = Decision(
        run_id="r",
        decomposition={
            "should_use_decomposition": True,
            "subtasks": [],
            "decomposition_requested": True,
            "decomposition_executed": False,
            "decomposition_fallback_to_candidate_path": True,
            "decomposition_fallback_used": True,
            "decomposition_fallback_reason": "Planner requested decomposition but decomposition produced no executable subtasks.",
        },
        decomposition_executed=False,
        decomposition_advisory_only=True,
        subtask_count=0,
    )
    path = write_performance_report(tmp_path, Task(repo_path=str(tmp_path), objective="Fix"), None, [], None, decision, 0)
    text = path.read_text()
    assert "Decomposition fallback used: true" in text
    assert "Planner requested decomposition but decomposition produced no executable subtasks." in text
