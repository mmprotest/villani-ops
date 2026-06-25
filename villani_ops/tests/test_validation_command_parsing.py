from types import SimpleNamespace
from pathlib import Path

from villani_ops.core.backend import Backend
from villani_ops.orchestration.engine import OrchestrationEngine

class _P: mode='performance'
class _R: name='runner'

def engine(tmp_path):
    return OrchestrationEngine(backends={'b':Backend(name='b',provider='local',model='m')}, execution_policy=_P(), runner_adapter=_R(), workspace=tmp_path)

def test_quoted_command_parses_correctly(tmp_path):
    e=engine(tmp_path)
    argv,shell=e._validation_argv({'cmd':'python -c "import sys; print(sys.argv[1])" "checkout flow"'})
    assert argv[-1] == 'checkout flow' and not shell

def test_explicit_argv_and_shell_recorded(tmp_path):
    e=engine(tmp_path)
    assert e._validation_argv({'argv':['python','-c','print(1)']}) == (['python','-c','print(1)'], False)
    assert e._validation_argv({'cmd':'echo hi', 'shell':True}) == ('echo hi', True)

def test_validation_plan_required_optional_and_artifacts(tmp_path):
    e=engine(tmp_path); idir=tmp_path/'integration'; idir.mkdir()
    ctx=SimpleNamespace(run_dir=tmp_path, integration={'worktree_path':str(tmp_path)}, task_context=SimpleNamespace(investigation={'validation_plan': {'commands':[
        {'argv':['python','-c','print("ok")'], 'required': True},
        {'cmd':'python -c "import sys; sys.exit(3)"', 'required': False},
    ]}}))
    res=e._run_validation_plan(ctx, 'validation_initial', str(tmp_path))
    assert res['passed']
    assert res['commands'][0]['argv'] == ['python','-c','print("ok")']
    assert res['commands'][0]['returncode'] == 0
    assert res['commands'][1]['returncode'] == 3 and not res['commands'][1]['required']


def test_required_command_failure_fails_aggregate(tmp_path):
    e=engine(tmp_path); (tmp_path/'integration').mkdir()
    ctx=SimpleNamespace(run_dir=tmp_path, integration={'worktree_path':str(tmp_path)}, task_context=SimpleNamespace(investigation={'validation_plan': {'commands':[{'cmd':'python -c "import sys; sys.exit(2)"'}]}}))
    assert not e._run_validation_plan(ctx, 'validation_initial', str(tmp_path))['passed']
