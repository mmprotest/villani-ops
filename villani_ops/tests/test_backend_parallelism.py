import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from typer.testing import CliRunner

from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.core.concurrency import BackendConcurrencyLimiter
from villani_ops.storage.files import FileStorage


def test_backend_config_default_max_parallel():
    b = Backend(name="b", provider="openai", model="m")
    assert b.max_parallel == 1


def test_old_backend_yaml_loads_default_max_parallel(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "backends.yaml").write_text("""backends:
- name: old
  provider: openai
  model: m
""")
    backends = FileStorage(ws).load_backends()
    assert backends["old"].max_parallel == 1


def test_backend_yaml_writes_max_parallel(tmp_path):
    s = FileStorage(tmp_path / "ws")
    s.save_backends({"b": Backend(name="b", provider="openai", model="m", max_parallel=2)})
    assert "max_parallel: 2" in (s.workspace / "backends.yaml").read_text()


def test_backend_add_accepts_and_list_displays_max_parallel(tmp_path):
    runner = CliRunner()
    ws = str(tmp_path / "ws")
    result = runner.invoke(app, ["backend", "add", "qwen35b", "--provider", "openai-compatible", "--model", "m", "--api-key", "dummy", "--max-parallel", "2", "--workspace", ws])
    assert result.exit_code == 0, result.output
    assert FileStorage(ws).load_backends()["qwen35b"].max_parallel == 2
    listed = runner.invoke(app, ["backend", "list", "--workspace", ws])
    assert listed.exit_code == 0, listed.output
    assert "max_parallel" in listed.output
    assert "2" in listed.output


def test_backend_add_rejects_zero_max_parallel(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["backend", "add", "bad", "--provider", "openai", "--model", "m", "--max-parallel", "0", "--workspace", str(tmp_path / "ws")])
    assert result.exit_code != 0


def test_limiter_caps_per_backend_and_releases_after_failure():
    limiter = BackendConcurrencyLimiter({"a": Backend(name="a", provider="openai", model="m", max_parallel=2)})
    active = 0
    observed = 0
    lock = threading.Lock()
    entered = threading.Barrier(2)

    def task(i):
        nonlocal active, observed
        def body():
            nonlocal active, observed
            with lock:
                active += 1
                observed = max(observed, active)
            if i < 2:
                entered.wait(timeout=2)
            time.sleep(0.02)
            with lock:
                active -= 1
            if i == 2:
                raise RuntimeError("boom")
            return i
        return limiter.run("a", body)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(task, i) for i in range(4)]
        results = []
        failures = 0
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except RuntimeError:
                failures += 1
    assert observed == 2
    assert failures == 1
    assert sorted(results) == [0, 1, 3]
    assert limiter.run("a", lambda: "released") == "released"


def test_limiter_permits_different_backends_concurrently():
    limiter = BackendConcurrencyLimiter({
        "a": Backend(name="a", provider="openai", model="m", max_parallel=1),
        "b": Backend(name="b", provider="openai", model="m", max_parallel=1),
    })
    active = 0
    observed = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def task(name):
        nonlocal active, observed
        def body():
            nonlocal active, observed
            with lock:
                active += 1
                observed = max(observed, active)
            barrier.wait(timeout=2)
            with lock:
                active -= 1
        limiter.run(name, body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(task, ["a", "b"]))
    assert observed == 2

from pathlib import Path
from types import SimpleNamespace

from villani_ops.execution_policies.base import BackendSelection
from villani_ops.orchestration.engine import OrchestrationEngine
from villani_ops.orchestration.graph import OrchestrationGraph
from villani_ops.orchestration.nodes import NodeExecutionResult, OrchestrationNode


class _Policy:
    mode = "performance"
    def select_backend(self, **kwargs):
        backend = kwargs["backends"]["b"]
        return BackendSelection(backend_name="b", backend=backend, reason="test")


class _Runner:
    name = "villani-code"


class _ParallelProbeEngine(OrchestrationEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active = 0
        self.max_seen = 0
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(2)
        self.worktrees = []

    def _execute_code_node(self, node, context):
        with self.lock:
            self.active += 1
            self.max_seen = max(self.max_seen, self.active)
        # Proves at least two tasks are running together without relying on timing only.
        if node.id in {"subtask_a_code", "subtask_b_code"}:
            self.barrier.wait(timeout=2)
        time.sleep(0.02)
        sid = node.id[len("subtask_"):-len("_code")]
        worktree = str(context.run_dir / "worktrees" / sid)
        Path(worktree).mkdir(parents=True, exist_ok=True)
        meta = {"subtask_id": sid, "status": "succeeded", "started_at": sid, "completed_at": sid, "worktree_path": worktree}
        node.result = meta
        context.graph.mark_succeeded(node.id, summary="ok")
        with self.lock:
            self.active -= 1
        self.worktrees.append(worktree)
        return NodeExecutionResult(node_id=node.id, status="succeeded", data=meta)


def test_parallel_subtask_code_nodes_observe_limit_distinct_worktrees_and_artifact(tmp_path):
    backends = {"b": Backend(name="b", provider="openai", model="m", max_parallel=2)}
    engine = _ParallelProbeEngine(backends=backends, execution_policy=_Policy(), runner_adapter=_Runner(), workspace=tmp_path)
    nodes = [OrchestrationNode(id=f"subtask_{sid}_code", kind="code", objective=sid) for sid in ["a", "b", "c", "d", "e"]]
    graph = OrchestrationGraph(run_id="r", nodes=nodes)
    context = SimpleNamespace(
        run_id="r",
        run_dir=tmp_path,
        graph=graph,
        routing_decisions={},
        task_context=SimpleNamespace(classification=None, investigation=None, plan=None, decomposition=None),
        parallel_execution={"enabled": True, "backend_limits": {"b": 2}, "subtasks_total": 5, "max_observed_concurrency": 0, "scheduled": []},
        controller_step_lock=threading.Lock(),
    )
    engine._execute_parallel_subtask_code_nodes(nodes, context)
    assert engine.max_seen == 2
    assert context.parallel_execution["max_observed_concurrency"] == 2
    assert len(set(engine.worktrees)) == 5
    assert (tmp_path / "decomposition" / "parallel_execution.json").exists()
    steps = (tmp_path / "controller_steps.jsonl").read_text()
    assert "subtask_scheduled" in steps
    assert "parallel_group_completed" in steps

class _CandidateParallelProbeEngine(OrchestrationEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active = 0
        self.max_seen = 0
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(2)
        self.worktrees = []

    def _execute_code_node(self, node, context):
        with self.lock:
            self.active += 1
            self.max_seen = max(self.max_seen, self.active)
        if node.id in {"code_attempt_001", "code_attempt_002"}:
            self.barrier.wait(timeout=2)
        time.sleep(0.02)
        aid = node.id.replace("code_", "")
        worktree = str(context.run_dir / "worktrees" / aid)
        Path(worktree).mkdir(parents=True, exist_ok=True)
        meta = {"attempt_id": aid, "status": "succeeded", "started_at": aid, "completed_at": aid, "worktree_path": worktree}
        node.result = meta
        context.graph.mark_succeeded(node.id, summary="ok")
        with self.lock:
            self.active -= 1
        self.worktrees.append(worktree)
        return NodeExecutionResult(node_id=node.id, status="succeeded", data=meta)


def test_parallel_normal_candidate_code_nodes_observe_backend_limit_and_artifact(tmp_path):
    backends = {"b": Backend(name="b", provider="openai", model="m", max_parallel=2)}
    engine = _CandidateParallelProbeEngine(backends=backends, execution_policy=_Policy(), runner_adapter=_Runner(), workspace=tmp_path)
    nodes = [OrchestrationNode(id=f"code_attempt_{i:03d}", kind="code", objective=str(i)) for i in range(1, 4)]
    graph = OrchestrationGraph(run_id="r", nodes=nodes)
    context = SimpleNamespace(
        run_id="r",
        run_dir=tmp_path,
        graph=graph,
        routing_decisions={},
        task_context=SimpleNamespace(classification=None, investigation=None, plan=None, decomposition=None),
        candidate_attempts=3,
        parallel_execution={},
        controller_step_lock=threading.Lock(),
    )
    engine._execute_parallel_candidate_code_nodes(nodes, context)
    assert engine.max_seen == 2
    assert context.parallel_execution["max_observed_parallelism"] == 2
    assert context.parallel_execution["started_attempts"] == ["code_attempt_001", "code_attempt_002", "code_attempt_003"]
    assert sorted(context.parallel_execution["completed_attempts"]) == ["code_attempt_001", "code_attempt_002", "code_attempt_003"]
    assert len(set(engine.worktrees)) == 3
    assert (tmp_path / "candidates" / "parallel_execution.json").exists()


def test_parallel_normal_candidate_code_nodes_respect_max_parallel_one(tmp_path):
    backends = {"b": Backend(name="b", provider="openai", model="m", max_parallel=1)}
    engine = _CandidateParallelProbeEngine(backends=backends, execution_policy=_Policy(), runner_adapter=_Runner(), workspace=tmp_path)
    # Avoid waiting for a second simultaneous task when the backend limit is one.
    engine.barrier = threading.Barrier(1)
    nodes = [OrchestrationNode(id=f"code_attempt_{i:03d}", kind="code", objective=str(i)) for i in range(1, 4)]
    graph = OrchestrationGraph(run_id="r", nodes=nodes)
    context = SimpleNamespace(run_id="r", run_dir=tmp_path, graph=graph, routing_decisions={}, task_context=SimpleNamespace(classification=None, investigation=None, plan=None, decomposition=None), candidate_attempts=3, parallel_execution={}, controller_step_lock=threading.Lock())
    engine._execute_parallel_candidate_code_nodes(nodes, context)
    assert engine.max_seen == 1
    assert context.parallel_execution["max_observed_parallelism"] == 1
