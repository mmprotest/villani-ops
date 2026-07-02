import json
import os
import stat
from pathlib import Path

from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerContext
from villani_ops.runners.villani_code import VillaniCodeRunner, provider_for_villani_code_cli


def _install_fake_villani_code(tmp_path: Path, monkeypatch):
    exe = tmp_path / 'villani-code'
    exe.write_text(
        '#!/usr/bin/env python\n'
        'import pathlib, sys\n'
        'pathlib.Path("args.txt").write_text("\\n".join(sys.argv))\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])


def _run_runner(tmp_path: Path, monkeypatch, provider: str):
    _install_fake_villani_code(tmp_path, monkeypatch)
    repo = tmp_path / 'repo'
    repo.mkdir()
    run = tmp_path / f'run-{provider.replace(" ", "-")}'
    run.mkdir()
    backend = Backend(
        name='b',
        provider=provider,
        base_url='http://127.0.0.1:1234/v1',
        model='villanis/models/qwen3.6-35b-a3b-ud-iq4_xs.gguf',
        api_key='secret',
    )
    result = VillaniCodeRunner().run(
        RunnerContext(
            attempt_id='a1',
            repo_path=str(repo),
            task_instruction='do',
            backend=backend,
            run_dir=str(run),
            timeout_seconds=5,
        )
    )
    assert result.exit_code == 0
    return backend, repo, run


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_provider_mapping_function_for_villani_code_cli():
    assert provider_for_villani_code_cli('openai-compatible') == 'openai'
    assert provider_for_villani_code_cli('openai_compatible') == 'openai'
    assert provider_for_villani_code_cli('openai compatible') == 'openai'
    assert provider_for_villani_code_cli('openai') == 'openai'
    assert provider_for_villani_code_cli('anthropic') == 'anthropic'
    assert provider_for_villani_code_cli('custom-provider') == 'custom-provider'


def test_openai_compatible_maps_to_openai_for_villani_code_cli(tmp_path, monkeypatch):
    backend, repo, run = _run_runner(tmp_path, monkeypatch, 'openai-compatible')

    args = (repo / 'args.txt').read_text().splitlines()
    command_artifact = json.loads((run / 'villani_code_command.json').read_text())

    assert _value_after(args, '--provider') == 'openai'
    assert _value_after(command_artifact, '--provider') == 'openai'
    assert _value_after(args, '--provider') != 'openai-compatible'
    assert _value_after(command_artifact, '--provider') != 'openai-compatible'
    assert backend.provider == 'openai-compatible'


def test_openai_remains_openai_for_villani_code_cli(tmp_path, monkeypatch):
    _backend, repo, run = _run_runner(tmp_path, monkeypatch, 'openai')

    args = (repo / 'args.txt').read_text().splitlines()
    command_artifact = json.loads((run / 'villani_code_command.json').read_text())

    assert _value_after(args, '--provider') == 'openai'
    assert _value_after(command_artifact, '--provider') == 'openai'


def test_anthropic_remains_anthropic_for_villani_code_cli(tmp_path, monkeypatch):
    _backend, repo, run = _run_runner(tmp_path, monkeypatch, 'anthropic')

    args = (repo / 'args.txt').read_text().splitlines()
    command_artifact = json.loads((run / 'villani_code_command.json').read_text())

    assert _value_after(args, '--provider') == 'anthropic'
    assert _value_after(command_artifact, '--provider') == 'anthropic'


def test_debug_flags_and_api_key_redaction_remain_present(tmp_path, monkeypatch):
    _backend, repo, run = _run_runner(tmp_path, monkeypatch, 'openai-compatible')

    args = (repo / 'args.txt').read_text().splitlines()
    command_artifact = json.loads((run / 'villani_code_command.json').read_text())

    assert _value_after(args, '--debug') == 'trace'
    assert _value_after(args, '--debug-dir') == str(run / 'villani_code_debug')
    assert _value_after(command_artifact, '--debug') == 'trace'
    assert _value_after(command_artifact, '--debug-dir') == str(run / 'villani_code_debug')
    assert _value_after(args, '--api-key') == 'secret'
    assert 'secret' not in (run / 'villani_code_command.json').read_text()
    assert _value_after(command_artifact, '--api-key') == '***REDACTED***'


def test_villani_code_runner_timeout_kills_child_process_group(tmp_path):
    import os, stat, time, subprocess
    from villani_ops.runners.villani_code import VillaniCodeRunner
    from villani_ops.runners.base import RunnerContext
    from villani_ops.core.backend import Backend
    marker=tmp_path/'child_alive.txt'
    exe=tmp_path/'villani-code'
    exe.write_text(f'''#!/usr/bin/env python3
import subprocess, time, pathlib
subprocess.Popen(["/bin/sh","-c","while true; do echo alive > {marker}; sleep 1; done"])
time.sleep(30)
''')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    old=os.environ.get('PATH'); os.environ['PATH']=str(tmp_path)+os.pathsep+os.environ.get('PATH','')
    try:
        b=Backend(name='b',provider='local',model='m',api_key='dummy',metadata={'allow_dummy_api_key':True})
        res=VillaniCodeRunner().run(RunnerContext(attempt_id='a',repo_path=str(tmp_path),task_instruction='x',backend=b,timeout_seconds=1,run_dir=str(tmp_path/'run')))
        assert res.exit_code==124 and 'timed out' in res.stderr.lower()
        time.sleep(1.5)
        ps=subprocess.run(['pgrep','-f',str(marker)],text=True,capture_output=True)
        live=[]
        for pid in ps.stdout.split():
            stat=subprocess.run(['ps','-o','stat=','-p',pid],text=True,capture_output=True).stdout.strip()
            if stat and 'Z' not in stat: live.append((pid, stat))
        assert not live, live
    finally:
        if old is None: os.environ.pop('PATH',None)
        else: os.environ['PATH']=old
