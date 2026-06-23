import json, os, subprocess, stat
from pathlib import Path
from typer.testing import CliRunner

from villani_ops.cli.main import app
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMClient
from villani_ops.runners.villani_code import VillaniCodeRunner
from villani_ops.runners.base import RunnerContext
from villani_ops.policy_engine.engine import PolicyEngine
from villani_ops.core.task import TaskClassification
from villani_ops.llm.client import LLMCallResult

runner=CliRunner()

def init_git(path: Path):
    path.mkdir(); subprocess.run(['git','init'],cwd=path,check=True,capture_output=True); subprocess.run(['git','config','user.email','a@b.c'],cwd=path,check=True); subprocess.run(['git','config','user.name','A'],cwd=path,check=True)
    (path/'hello.txt').write_text('hello\n'); subprocess.run(['git','add','.'],cwd=path,check=True); subprocess.run(['git','commit','-m','init'],cwd=path,check=True,capture_output=True)

def make_run(ws: Path, repo: Path, patch_text: str, accepted=True):
    rd=ws/'runs'/'r1'; rd.mkdir(parents=True, exist_ok=True); (rd/'task.json').write_text(json.dumps({'repo_path':str(repo)})); patch=rd/'diff.patch'; patch.write_text(patch_text)
    (rd/'decision.json').write_text(json.dumps({'run_id':'r1','accepted':accepted,'winning_patch_path':str(patch),'winning_branch':None}))
    return rd

def test_backend_direct_key_stored_masked_and_conflict_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app,['init']).exit_code==0
    res=runner.invoke(app,['backend','add','local-qwen','--provider','openai-compatible','--base-url','http://x/v1','--model','m','--api-key','dummy','--roles','coding,classification,review,policy'])
    assert res.exit_code==0
    raw=(tmp_path/'.villani-ops'/'backends.yaml').read_text(); assert 'api_key: dummy' in raw
    assert 'dummy' not in runner.invoke(app,['backend','list']).output
    assert '***REDACTED***' in runner.invoke(app,['backend','show','local-qwen']).output
    bad=runner.invoke(app,['backend','add','bad','--provider','local','--model','m','--api-key','x','--api-key-env','X'])
    assert bad.exit_code!=0 and 'Choose only one' in bad.output

def test_llm_client_uses_direct_key(monkeypatch):
    seen={}
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'choices':[{'message':{'content':'{"ok": true}'}}], 'usage':{'prompt_tokens':1,'completion_tokens':2}}
    def fake_post(url, json, headers, timeout): seen.update(headers=headers); return Resp()
    monkeypatch.setattr('httpx.post', fake_post)
    b=Backend(name='b',provider='openai-compatible',base_url='http://x/v1',model='m',api_key='secret')
    LLMClient().complete_json(b,'s','u','S')
    assert seen['headers']['Authorization']=='Bearer secret'

def test_villani_code_receives_key_and_saves_redacted_command(tmp_path, monkeypatch):
    exe=tmp_path/'villani-code'; exe.write_text('#!/usr/bin/env python\nimport sys, pathlib\npathlib.Path("args.txt").write_text("\\n".join(sys.argv))\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv('PATH', str(tmp_path)+os.pathsep+os.environ['PATH'])
    repo=tmp_path/'repo'; repo.mkdir(); run=tmp_path/'run'; run.mkdir()
    b=Backend(name='b',provider='openai-compatible',base_url='http://x/v1',model='m',api_key='secret')
    res=VillaniCodeRunner().run(RunnerContext(attempt_id='a1',repo_path=str(repo),task_instruction='do',backend=b,run_dir=str(run),timeout_seconds=5))
    assert res.exit_code==0 and '--api-key\nsecret' in (repo/'args.txt').read_text()
    assert 'secret' not in (run/'villani_code_command.json').read_text()

class FakeClient:
    def __init__(self, attempts): self.attempts=attempts
    def complete_json(self,*a,**k): return LLMCallResult(parsed_json={'profile':'balanced','attempts':self.attempts},raw_text='{}',backend_name='p',model='m')

def _b(name, roles=('coding',), cap=1, cost=1, enabled=True):
    return Backend(name=name,provider='local',model=name,roles=list(roles),capability_score=cap,input_cost_per_million=cost,output_cost_per_million=cost,enabled=enabled)

def test_policy_guardrails_balanced_and_invented_repaired():
    backs={'cheap':_b('cheap',cap=10,cost=0),'strong':_b('strong',cap=90,cost=5),'policy':_b('policy',roles=('policy',))}
    cls=TaskClassification(difficulty='easy',category='bugfix',risk='low')
    strat,_=PolicyEngine(FakeClient([{'backend':'invented'},{'backend':'strong'}])).generate(cls,backs,'balanced')
    assert strat.attempts[0].backend=='cheap' and strat.warnings

def test_apply_guards_check_and_success_and_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); repo=tmp_path/'repo'; init_git(repo); ws=tmp_path/'.villani-ops'
    bad='diff --git a/missing.txt b/missing.txt\nindex 1111111..2222222 100644\n--- a/missing.txt\n+++ b/missing.txt\n@@ -1 +1 @@\n-x\n+y\n'
    rd=make_run(ws,repo,bad)
    res=runner.invoke(app,['apply','r1','--workspace',str(ws)])
    assert res.exit_code!=0 and (repo/'hello.txt').read_text()=='hello\n'
    assert json.loads((rd/'apply.json').read_text())['exit_code']==1
    good='diff --git a/hello.txt b/hello.txt\nindex ce01362..2e09960 100644\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+changed\n'
    rd=make_run(ws,repo,good)
    res=runner.invoke(app,['apply','r1','--commit','--workspace',str(ws)])
    assert res.exit_code==0 and (repo/'hello.txt').read_text()=='changed\n'
    art=json.loads((rd/'apply.json').read_text()); assert art['exit_code']==0 and art['commit_sha']

def test_branch_existing_refuses_and_force_branch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); repo=tmp_path/'repo'; init_git(repo); ws=tmp_path/'.villani-ops'
    subprocess.run(['git','checkout','-b','feature'],cwd=repo,check=True,capture_output=True); subprocess.run(['git','checkout','master'],cwd=repo,check=True,capture_output=True)
    patch='diff --git a/hello.txt b/hello.txt\nindex ce01362..2e09960 100644\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+branch\n'
    make_run(ws,repo,patch)
    assert runner.invoke(app,['branch','r1','--name','feature','--workspace',str(ws)]).exit_code!=0
    res=runner.invoke(app,['branch','r1','--name','feature','--force-branch','--workspace',str(ws)])
    assert res.exit_code==0 and subprocess.run(['git','branch','--show-current'],cwd=repo,text=True,capture_output=True).stdout.strip()=='feature'

def test_pr_missing_gh_saves_manual_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); repo=tmp_path/'repo'; init_git(repo); ws=tmp_path/'.villani-ops'
    patch='diff --git a/hello.txt b/hello.txt\nindex ce01362..2e09960 100644\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+pr\n'
    rd=make_run(ws,repo,patch)
    monkeypatch.setattr('shutil.which', lambda name: None if name=='gh' else '/usr/bin/'+name)
    res=runner.invoke(app,['pr','r1','--title','T','--body','B','--workspace',str(ws)])
    assert res.exit_code!=0, res.output
    art=json.loads((rd/'pr.json').read_text()); assert art['gh_available'] is False and art['manual_commands']

def test_readme_primary_example_uses_direct_key():
    readme=Path('README.md').read_text()
    assert '--api-key dummy' in readme
    assert '--api-key-env VILLANI_API_KEY' not in readme
