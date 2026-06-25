import json
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

class _E2EProbeEngine(OrchestrationEngine):
    def __init__(self,*a,outcomes=None,**kw):
        super().__init__(*a,**kw); self.events=[]; self.active=0; self.max_seen=0; self.lock=threading.Lock(); self.outcomes=outcomes or {}
    def _event(self,node,action):
        with self.lock: self.events.append((node.id if node else None,action,time.time()))
    def _execute_classify_node(self,node,context):
        self._event(node,'start'); context.classification=None; context.task_context.classification={}; return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',data={}),{})
    def _execute_investigate_node(self,node,context):
        self._event(node,'start'); context.investigation=None; context.task_context.investigation={}; return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',data={}),{})
    def _execute_plan_node(self,node,context):
        self._event(node,'start'); from villani_ops.orchestration.planner import PlanResult; p=PlanResult(summary='p',should_decompose=False,candidate_attempts=context.candidate_attempts); context.plan=p; context.task_context.plan=p.model_dump(mode='json'); return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',data=context.task_context.plan),context.task_context.plan)
    def _execute_decompose_node(self,node,context):
        self._event(node,'start'); return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='skipped',data={'intentional_skip':True}),{'intentional_skip':True})
    def _execute_code_node(self,node,context):
        self._event(node,'start')
        with self.lock:
            self.active+=1; self.max_seen=max(self.max_seen,self.active)
        time.sleep(0.03)
        aid=node.id.replace('code_',''); adir=context.run_dir/'attempts'/aid; adir.mkdir(parents=True,exist_ok=True)
        wt=context.run_dir/'worktrees'/aid; wt.mkdir(parents=True,exist_ok=True)
        patch=adir/'patch.diff'; status=self.outcomes.get(aid,'accept')
        if status != 'code_fail': patch.write_text('diff --git a/f b/f\n')
        meta={'attempt_id':aid,'backend_name':node.assigned_backend,'model':'m','status':'succeeded','exit_code':0,'started_at':_now(),'completed_at':_now(),'worktree_path':str(wt),'patch_path':str(patch) if patch.exists() else None,'changed_files':['f'] if patch.exists() else [],'input_tokens':10,'output_tokens':5,'token_accounting_status':'reported'}
        if status == 'code_fail': meta.update({'status':'failed','exit_code':1,'changed_files':[],'patch_path':None})
        node.result=meta; self._add_usage(context,'coding',10,5,0.25)
        with self.lock: self.active-=1
        self._event(node,'end')
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='failed' if status=='code_fail' else 'succeeded',data=meta,error='code failed' if status=='code_fail' else None),meta)
    def _execute_review_node(self,node,context):
        self._event(node,'start'); aid=node.id.replace('review_',''); cn=context.graph.get(f'code_{aid}'); meta=dict(cn.result or {}); eligible=self.outcomes.get(aid,'accept')=='accept' and meta.get('exit_code')==0 and meta.get('patch_path')
        meta.update({'review':{'decision':'pass' if eligible else 'fail','recommended_action':'accept' if eligible else 'fail','summary':'r','score':0.9 if eligible else 0.1},'acceptance_eligible':bool(eligible),'acceptance_blockers':[] if eligible else ['rejected'],'candidate_summary':{'attempt_id':aid,'acceptance_eligible':bool(eligible)}})
        context.attempts.append(meta); self._add_usage(context,'review',1,2,0.05)
        if context.parallel_execution and context.parallel_execution.get('enabled'):
            for row in context.parallel_execution.get('results',[]):
                if row.get('node_id')==f'code_{aid}': row['review_status']='accepted' if eligible else 'rejected'
            write_json_utf8(context.run_dir/'candidates'/'parallel_execution.json',context.parallel_execution)
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',data=meta),meta)
    def _execute_select_node(self,node,context):
        self._event(node,'start'); from villani_ops.performance.models import SelectionResult
        eligible=[a for a in context.attempts if a.get('acceptance_eligible')]; sel=eligible[-1]['attempt_id'] if eligible else None
        context.selection=SelectionResult(decision='select' if sel else 'reject_all',selected_attempt_id=sel,summary='s',confidence=1.0); context.winner=eligible[-1] if eligible else None
        return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',data=context.selection.model_dump(mode='json')),context.selection.model_dump(mode='json'))
    def _execute_verify_node(self,node,context):
        self._event(node,'start'); data={'accepted':bool(context.winner),'winner':context.winner.get('attempt_id') if context.winner else None}; return self._finish(context,node,NodeExecutionResult(node_id=node.id,status='succeeded',data=data),data)
from villani_ops.orchestration.engine import _now
from villani_ops.orchestration.artifacts import write_json_utf8
from villani_ops.core.task import Task

def _run_e2e(tmp_path, attempts, max_parallel, outcomes=None):
    repo=tmp_path/'repo'; git_repo_simple(repo)
    backends={'b': Backend(name='b',provider='openai',model='m',max_parallel=max_parallel,roles=['coding','review','selection','investigation','classification','policy'])}
    e=_E2EProbeEngine(backends=backends,execution_policy=_Policy(),runner_adapter=_Runner(),workspace=tmp_path/'ws',outcomes=outcomes)
    res=e.run(repo=repo,task=Task(repo_path=str(repo),objective='x'),candidate_attempts=attempts,classify=True)
    return e,res,Path(res.run_dir)

def git_repo_simple(path):
    import subprocess
    path.mkdir(); subprocess.run(['git','init'],cwd=path,check=True,capture_output=True); subprocess.run(['git','config','user.email','a@b.c'],cwd=path,check=True); subprocess.run(['git','config','user.name','A'],cwd=path,check=True); (path/'f').write_text('x\n'); subprocess.run(['git','add','.'],cwd=path,check=True); subprocess.run(['git','commit','-m','init'],cwd=path,check=True,capture_output=True)

def _event_time(engine,node,action):
    return next(t for n,a,t in engine.events if n==node and a==action)

def test_e2e_parallel_candidate_scheduler_max_two(tmp_path):
    e,res,rd=_run_e2e(tmp_path,3,2)
    assert e.max_seen == 2
    assert {n for n,a,t in e.events if n and n.startswith('code_attempt') and a=='start'} == {'code_attempt_001','code_attempt_002','code_attempt_003'}
    assert len({a['worktree_path'] for a in res.attempts}) == 3
    assert len({a['patch_path'] for a in res.attempts}) == 3
    for i in range(1,4):
        assert _event_time(e,f'review_attempt_{i:03d}','start') > _event_time(e,f'code_attempt_{i:03d}','end')
    select_t=_event_time(e,'select','start')
    for i in range(1,4): assert select_t > _event_time(e,f'review_attempt_{i:03d}','start')
    assert res.decision.accepted
    pe=json.loads((rd/'candidates'/'parallel_execution.json').read_text())
    assert pe['enabled'] is True and pe['candidate_attempts']==3 and pe['max_observed_parallelism']==2
    assert set(pe['started_attempts']) == {f'code_attempt_{i:03d}' for i in range(1,4)}
    assert set(pe['completed_attempts']) == set(pe['started_attempts'])
    assert len(pe['results']) == 3

def test_e2e_parallel_candidate_scheduler_max_one(tmp_path):
    e,res,rd=_run_e2e(tmp_path,3,1)
    assert e.max_seen == 1 and len(res.attempts)==3 and res.decision.accepted
    assert json.loads((rd/'candidates'/'parallel_execution.json').read_text())['max_observed_parallelism'] == 1

def test_e2e_single_candidate_does_not_write_enabled_parallel_artifact(tmp_path):
    e,res,rd=_run_e2e(tmp_path,1,2)
    assert len(res.attempts)==1 and res.decision.accepted
    assert not (rd/'candidates'/'parallel_execution.json').exists()

def test_e2e_mixed_candidate_outcomes_selects_accepted_candidate_after_all_reviews(tmp_path):
    e,res,rd=_run_e2e(tmp_path,3,2,{'attempt_001':'code_fail','attempt_002':'reject','attempt_003':'accept'})
    assert len(res.attempts)==3
    assert res.decision.winning_attempt_id == 'attempt_003'
    select_t=_event_time(e,'select','start')
    for i in range(1,4): assert select_t > _event_time(e,f'review_attempt_{i:03d}','start')
    assert json.loads((rd/'candidates'/'parallel_execution.json').read_text())['results'][2]['review_status'] == 'accepted'

def test_parallel_candidate_shared_state_totals_and_json_artifacts(tmp_path):
    e,res,rd=_run_e2e(tmp_path,3,2)
    assert res.decision.total_input_tokens == 33
    assert res.decision.total_output_tokens == 21
    graph=json.loads((rd/'orchestration_graph.json').read_text())
    assert all(n['status'] in {'succeeded','failed','skipped'} for n in graph['nodes'] if n['id'].startswith('code_attempt_'))
    pe=json.loads((rd/'candidates'/'parallel_execution.json').read_text())
    assert len(pe['started_attempts']) == len(pe['completed_attempts']) == len(pe['results']) == 3
