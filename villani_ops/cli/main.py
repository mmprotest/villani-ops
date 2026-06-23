from __future__ import annotations
from pathlib import Path
import typer, json, subprocess, shutil, os
from rich.console import Console
from rich.table import Table
from villani_ops import VillaniOps
from villani_ops.core.task import Task
from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.policy_engine.defaults import DEFAULT_PROFILES

app=typer.Typer(help='Villani Ops: cost-aware AI coding operations.')
backend_app=typer.Typer(); task_app=typer.Typer(); policy_app=typer.Typer(); runner_app=typer.Typer()
app.add_typer(backend_app,name='backend'); app.add_typer(task_app,name='task'); app.add_typer(policy_app,name='policy'); app.add_typer(runner_app,name='runner')
console=Console()
def storage(workspace='.villani-ops'): return FileStorage(workspace)

@app.command()
def init(workspace: str='.villani-ops'):
    storage(workspace).init_workspace(); console.print(f'Initialized Villani Ops workspace at {workspace}')

@backend_app.command('add')
def backend_add(name: str, provider: str=typer.Option(...), base_url: str|None=None, model: str=typer.Option(...), api_key: str|None=None, api_key_env: str|None=None, input_cost: float=0.0, output_cost: float=0.0, roles: str='coding', capability_score: int=0, max_tokens: int|None=None, timeout_seconds: int|None=None, workspace: str='.villani-ops'):
    s=storage(workspace); s.init_workspace(); b=s.load_backends(); b[name]=Backend(name=name,provider=provider,base_url=base_url,model=model,api_key=api_key,api_key_env=api_key_env,input_cost_per_million=input_cost,output_cost_per_million=output_cost,roles=[r.strip() for r in roles.split(',') if r.strip()],capability_score=capability_score,max_tokens=max_tokens,timeout_seconds=timeout_seconds); s.save_backends(b); console.print(f'Added backend {name}')

@backend_app.command('list')
def backend_list(workspace: str='.villani-ops'):
    table=Table('Name','Provider','Model','Roles','Capability','Costs $/M','State','Base URL','API key')
    for b in storage(workspace).load_backends().values(): table.add_row(b.name,b.provider,b.model,','.join(b.roles),str(b.capability_score),f'{b.input_cost_per_million}/{b.output_cost_per_million}','enabled' if b.enabled else 'disabled',b.base_url or '', 'configured' if b.api_key_configured() else 'missing')
    console.print(table)
@backend_app.command('show')
def backend_show(name: str, workspace: str='.villani-ops'):
    console.print_json(json.dumps(storage(workspace).load_backends()[name].redacted_dict()))
@backend_app.command('disable')
def backend_disable(name: str, workspace: str='.villani-ops'):
    s=storage(workspace); b=s.load_backends(); b[name].enabled=False; s.save_backends(b); console.print(f'Disabled {name}')
@backend_app.command('enable')
def backend_enable(name: str, workspace: str='.villani-ops'):
    s=storage(workspace); b=s.load_backends(); b[name].enabled=True; s.save_backends(b); console.print(f'Enabled {name}')


@runner_app.command('set')
def runner_set(name: str, command: str=typer.Option(...), workspace: str='.villani-ops'):
    s=storage(workspace); s.init_workspace(); cfg=s.load_config(); cfg.setdefault('runners',{}).setdefault(name,{})['command']=command; s.save_config(cfg); console.print(f'Set runner {name}')

@runner_app.command('list')
def runner_list(workspace: str='.villani-ops'):
    table=Table('Runner','Command')
    for n,v in (storage(workspace).load_config().get('runners') or {}).items(): table.add_row(n, str((v or {}).get('command')))
    console.print(table)

@task_app.command('create')
def task_create(repo: str=typer.Option(...), objective: str=typer.Option(...), success_criteria: str|None=None, workspace: str='.villani-ops'):
    s=storage(workspace); s.init_workspace(); t=Task(repo_path=str(Path(repo).resolve()), objective=objective, success_criteria=success_criteria); d=s.workspace/'tasks'/t.task_id; d.mkdir(parents=True); (d/'task.json').write_text(t.model_dump_json(indent=2)); console.print(t.task_id)

@policy_app.command('list-defaults')
def policy_list_defaults():
    for k,v in DEFAULT_PROFILES.items(): console.print(f'{k}: {v["summary"]}')
@policy_app.command('show')
def policy_show(name: str): console.print_json(json.dumps(DEFAULT_PROFILES[name]))
@policy_app.command('create-default')
def policy_create_default(name: str=typer.Option('balanced'), workspace: str='.villani-ops'):
    from villani_ops.core.policy import Policy, AttemptPlan
    s=storage(workspace); s.init_workspace(); backends=s.load_backends(); attempts=[AttemptPlan(backend=b.name, max_attempts=1, timeout_seconds=900, runner='shell') for b in backends.values()]
    pol=Policy(name=name, attempts=attempts); path=s.workspace/'policies'/f'{name}.yaml'; pol.save(path); console.print(f'Created policy at {path}')

@app.command()
def run(repo: str|None=None, task: str|None=typer.Option(None,'--task'), task_id: str|None=None, policy: str='balanced', success_criteria: str|None=None, isolation: str='worktree', workspace: str='.villani-ops'):
    s=storage(workspace)
    if task_id:
        data=json.loads((s.workspace/'tasks'/task_id/'task.json').read_text()); t=Task.model_validate(data); repo=t.repo_path
    else:
        if not repo or not task: raise typer.BadParameter('Provide --task-id or both --repo and --task')
        t=Task(repo_path=str(Path(repo).resolve()), objective=task, success_criteria=success_criteria)

    if policy.endswith('.yaml') or '/' in policy:
        result=_legacy_run(repo, t, policy, workspace)
    else:
        result=VillaniOps.from_workspace(workspace).run(repo=repo, task=t, policy=policy, isolation=isolation)
    d=result.decision; console.print(f"Result: {'ACCEPTED' if d.accepted else 'REJECTED' if policy.endswith('.yaml') or '/' in policy else 'FAILED'}"); console.print(f'Task: {t.objective}'); c=d.classification or {}; console.print(f"Classification: {c.get('difficulty')} {c.get('category')} {c.get('risk')}"); console.print(f"Strategy: {policy}, {len((d.execution_strategy or {}).get('attempts',[]))} planned attempts"); console.print(f"Winner: {d.winning_attempt_id or 'none'}"); console.print(f"Review: {d.reviewer_decision}, score {d.reviewer_score}"); console.print(f"Cost: total ${d.total_cost:.6f}"); console.print('Evidence:'); [console.print(f'  - {e}') for e in d.reviewer_evidence]; console.print(f"Apply:\n  villani-ops apply {d.run_id}"); console.print(f'Report: {result.report_path}')


def _legacy_run(repo, t, policy_path, workspace):
    from datetime import datetime, timezone
    import secrets, time
    from types import SimpleNamespace
    from villani_ops.core.policy import Policy
    from villani_ops.core.decision import select_attempt
    from villani_ops.core.attempt import Attempt
    from villani_ops.runners.shell import ShellRunner
    from villani_ops.runners.base import RunnerContext
    from villani_ops.isolation.copy import CopyIsolation
    from villani_ops.storage.files import capture_diff
    from villani_ops.validation.base import DiffReviewValidator
    from villani_ops.reports.markdown import write_markdown_report
    s=storage(workspace); s.init_workspace(); repo=Path(repo).resolve(); pol=Policy.load(policy_path); run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3); run_dir=s.create_run_dir(run_id); s.save_task(run_dir,t); backends=s.load_backends(); cfg=s.load_config(); attempts=[]; warnings=[]
    for plan in pol.attempts:
        b=backends[plan.backend]
        aid=f'attempt_{len(attempts)+1:03d}'; adir=run_dir/'attempts'/aid; adir.mkdir(parents=True,exist_ok=True); copy=CopyIsolation().create(repo, adir/'repo')
        a=Attempt(attempt_id=aid,run_id=run_id,backend_name=b.name,runner_name=plan.runner,status='running',isolated_repo_path=str(copy)); (adir/'task.md').write_text(t.objective or '')
        res=ShellRunner().run(RunnerContext(attempt_id=aid,repo_path=str(copy),task_instruction=t.objective or '',success_criteria=t.success_criteria,backend=b,timeout_seconds=plan.timeout_seconds,run_dir=str(adir),command=(cfg.get('runners',{}).get('shell',{}) or {}).get('command')))
        (adir/'stdout.log').write_text(res.stdout); (adir/'stderr.log').write_text(res.stderr); a.stdout_path=str(adir/'stdout.log'); a.stderr_path=str(adir/'stderr.log')
        diff=capture_diff(repo,copy,adir/'diff.patch'); a.diff_path=str(diff)
        if res.exit_code!=0: a.status='failed'; a.error=res.stderr.strip() or f'Shell runner command is not configured.'; warnings.append(a.error)
        a.validation=DiffReviewValidator().validate(diff);
        if res.exit_code==0 and a.validation.passed: a.status='validated'
        s.save_validation(adir,a.validation); s.save_attempt(adir,a); attempts.append(a)
    d=select_attempt(run_id,attempts,pol.selection,warnings); s.save_decision(run_dir,d)
    # minimal legacy report text expected by tests
    report=run_dir/'report.md'; report.write_text(('ACCEPTED' if d.accepted else 'REJECTED')+'\n'+'\n'.join(warnings)+'\n'+ '\n'.join([a.diff_path or '' for a in attempts]))
    return SimpleNamespace(run_id=run_id,run_dir=str(run_dir),decision=d,report_path=str(report),attempts=attempts)

@app.command()
def apply(run_id: str, branch: str|None=None, commit: bool=False, force: bool=False, workspace: str='.villani-ops'):
    d=json.loads((storage(workspace).workspace/'runs'/run_id/'decision.json').read_text())
    if not d.get('accepted'): raise typer.BadParameter('No accepted attempt exists')
    repo=Path(json.loads((storage(workspace).workspace/'runs'/run_id/'task.json').read_text())['repo_path'])
    if subprocess.run(['git','status','--porcelain'],cwd=repo,text=True,capture_output=True).stdout.strip() and not force: raise typer.BadParameter('Source repo is dirty; use --force')
    if branch: subprocess.run(['git','checkout','-b',branch],cwd=repo,check=True)
    subprocess.run(['git','apply',d['winning_patch_path']],cwd=repo,check=True)
    if commit: subprocess.run(['git','add','.'],cwd=repo,check=True); subprocess.run(['git','commit','-m',f'Apply Villani Ops run {run_id}'],cwd=repo,check=True)
    console.print('Applied patch')

@app.command()
def branch(run_id: str, name: str=typer.Option(...), commit: bool=False, workspace: str='.villani-ops'):
    apply(run_id, branch=name, commit=commit, workspace=workspace)

@app.command()
def pr(run_id: str, title: str, body: str='', workspace: str='.villani-ops'):
    if not shutil.which('gh'):
        console.print(f'gh not available. Run manually:\nvillani-ops branch {run_id} --name villani-ops/{run_id} --commit\ngit push -u origin villani-ops/{run_id}\ngh pr create --title {title!r} --body {body!r}'); return
    console.print('Use branch command then gh pr create; automatic GitHub integration is optional.')

@app.command()
def report(run_id_or_latest: str, workspace: str='.villani-ops'):
    s=storage(workspace); run_dir=s.resolve_latest_run() if run_id_or_latest in {'latest','runs/latest'} else s.workspace/'runs'/run_id_or_latest; console.print((Path(run_dir)/'report.md').read_text())
if __name__=='__main__': app()
