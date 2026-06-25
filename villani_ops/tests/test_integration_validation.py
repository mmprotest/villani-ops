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
