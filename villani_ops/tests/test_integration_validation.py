import json
import sys
from pathlib import Path

from villani_ops.orchestration.engine import OrchestrationEngine
from villani_ops.performance.report import write_performance_report
from villani_ops.core.decision import Decision
from villani_ops.core.task import Task


def test_validation_command_uses_sys_executable_not_bare_python(tmp_path):
    engine=OrchestrationEngine.__new__(OrchestrationEngine)
    cmd=engine._validation_command(tmp_path)
    assert cmd == [sys.executable, '-m', 'pytest', '-q']
    assert cmd[0] != 'python'


def test_run_cmd_records_python_executable_and_environment_failure(tmp_path):
    engine=OrchestrationEngine.__new__(OrchestrationEngine)
    script=tmp_path/'fakepython.py'
    script.write_text("import sys; sys.stderr.write('No module named pytest'); sys.exit(1)")
    res=engine._run_cmd([sys.executable, str(script), '-m', 'pytest'], tmp_path)
    assert res['python_executable'] == sys.executable
    assert res['exit_code'] == 1
    assert 'environment_failure' in (res['stderr'] or '')


def test_windows_venv_python_path_preserved_by_validation_artifact(tmp_path):
    val={'command':['C:\\repo\\.venv\\Scripts\\python.exe','-m','pytest','-q'], 'python_executable':'C:\\repo\\.venv\\Scripts\\python.exe', 'exit_code':0, 'passed':True}
    p=tmp_path/'validation_initial.json'; p.write_text(json.dumps(val))
    loaded=json.loads(p.read_text())
    assert loaded['python_executable'].endswith('.venv\\Scripts\\python.exe')
    assert loaded['command'][0] == loaded['python_executable']


def test_report_shows_initial_and_post_repair_validation_separately(tmp_path):
    (tmp_path/'controller_steps.jsonl').write_text('{}\n')
    dec=Decision(run_id='r', accepted=True, decomposition_executed=True, subtask_count=1, subtasks_accepted=['a'], integration_validation={'passed':True,'exit_code':0,'command':[sys.executable,'-m','pytest','-q']}, integration_validation_initial={'passed':False,'exit_code':1,'command':[sys.executable,'-m','pytest','-q']}, integration_validation_after_repair={'passed':True,'exit_code':0,'command':[sys.executable,'-m','pytest','-q']}, integration_repair_used=True)
    text=write_performance_report(tmp_path, Task(repo_path=str(tmp_path), objective='Fix'), None, [], None, dec, 0).read_text()
    assert 'Initial validation: passed=false exit_code=1' in text
    assert 'Repair used: true' in text
    assert 'Post-repair validation: passed=true exit_code=0' in text


def test_repair_validation_context_records_full_required_and_optional_failures(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from villani_ops.orchestration.engine import OrchestrationEngine
    from villani_ops.core.backend import Backend
    from villani_ops.core.task import Task
    from villani_ops.execution_policies.base import BackendSelection
    from villani_ops.orchestration.graph import OrchestrationGraph
    from villani_ops.orchestration.nodes import OrchestrationNode
    from villani_ops.runners.base import RunnerResult
    import json

    class Policy:
        mode='performance'
        def select_backend(self, **kw):
            b=kw['backends']['b']; return BackendSelection(backend_name='b', backend=b, reason='x')
    class Runner:
        name='villani-code'
        def run_task(self, **kw): return RunnerResult(exit_code=0)
    e=OrchestrationEngine(backends={'b':Backend(name='b',provider='openai',model='m')}, execution_policy=Policy(), runner_adapter=Runner(), workspace=tmp_path/'ws')
    idir=tmp_path/'run'/'integration'; idir.mkdir(parents=True)
    (idir/'a.out').write_text('out1'); (idir/'a.err').write_text('err1')
    (idir/'b.out').write_text('out2'); (idir/'b.err').write_text('err2')
    validation={'passed':False,'fallback':False,'source':'investigation','commands':[
        {'cmd':'npm test -- --grep "checkout flow"','argv':['npm','test','--','--grep','checkout flow'],'shell':False,'required':True,'reason':'required one','returncode':1,'passed':False,'stdout_path':str(idir/'a.out'),'stderr_path':str(idir/'a.err')},
        {'cmd':'python -m pytest -q','argv':['python','-m','pytest','-q'],'shell':False,'required':True,'reason':'required two','returncode':2,'passed':False,'stdout_path':str(idir/'b.out'),'stderr_path':str(idir/'b.err')},
        {'cmd':'echo optional','argv':['echo','optional'],'shell':False,'required':False,'reason':'optional','returncode':3,'passed':False,'stdout':'optout','stderr':'opterr'},
    ]}
    (idir/'dummy.patch').write_text('')
    monkeypatch.setattr('villani_ops.orchestration.engine.capture_worktree', lambda worktree, out: {'patch_path': str(idir/'dummy.patch'), 'changed_files': []})
    ctx=SimpleNamespace(timeout_seconds=None, run_dir=tmp_path/'run', integration={'validation':validation,'validation_initial':validation,'worktree_path':str(tmp_path),'apply_results':[],'combined_patch_path':None}, graph=OrchestrationGraph(run_id='r',nodes=[OrchestrationNode(id='integration_repair',kind='integration_repair', objective='repair')]), task=Task(repo_path=str(tmp_path), objective='x'), success_criteria='', accepted_subtasks=[], controller_step_lock=None, routing_decisions={}, task_context=SimpleNamespace(), run_id='r', mode='performance', runner='villani-code', costs={'coding':0,'review':0,'classification':0,'investigation':0,'selection':0}, input_tokens=0, output_tokens=0, warnings=[])
    node=ctx.graph.get('integration_repair'); node.assigned_backend='b'; node.assigned_model='m'
    e._run_validation_plan = lambda context, phase, worktree_path: {'passed': True, 'commands': [], 'fallback': False, 'source': 'test'}
    e._execute_integration_repair_node(node, ctx)
    data=json.loads((idir/'repair_validation_context.json').read_text())
    assert data['aggregate_status'] == 'failed'
    assert len(data['required_failed_commands']) == 2
    assert len(data['optional_failed_commands']) == 1
    assert data['required_failed_commands'][0]['argv'] == ['npm','test','--','--grep','checkout flow']
    prompt=(idir/'repair_prompt.txt').read_text()
    assert 'required one' in prompt and 'required two' in prompt and 'optional' in prompt
