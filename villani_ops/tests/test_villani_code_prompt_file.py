import json
from pathlib import Path
from villani_ops.core.backend import Backend

from villani_ops.runners.villani_code import VillaniCodeRunner
from villani_ops.runners.base import RunnerContext


def test_long_villani_code_prompt_uses_task_file_not_argv(tmp_path, monkeypatch):
    monkeypatch.setenv('VILLANI_CODE_INLINE_PROMPT_LIMIT','10')
    ctx=RunnerContext(attempt_id='a', repo_path=str(tmp_path), task_instruction='x'*100, success_criteria='ok', backend=Backend(name='b',provider='local',model='m',api_key='dummy',command_name='echo',max_tokens=100), timeout_seconds=10, run_dir=str(tmp_path/'run'))
    res=VillaniCodeRunner().run(ctx)
    assert res.exit_code == 0
    cmd=json.loads((tmp_path/'run'/'villani_code_command.json').read_text())
    assert '--task-file' in cmd
    assert 'x'*100 not in cmd
    prompt_path=Path(cmd[cmd.index('--task-file')+1])
    assert prompt_path.exists()
    assert 'x'*100 in prompt_path.read_text()
