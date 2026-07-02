from __future__ import annotations
from pathlib import Path
import typer, json, subprocess, shutil, os, sys
from rich.console import Console
from rich.table import Table
from villani_ops import VillaniOps, CostPolicyVillaniOps
from villani_ops.core.task import Task
from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.policy_engine.defaults import DEFAULT_PROFILES
from villani_ops.controller.progress import RunProgressReporter
from villani_ops.agentic.progress import AgenticProgressReporter
from villani_ops.core.policy import DEFAULT_TIMEOUT_SECONDS

app=typer.Typer(help='Villani Ops: CLI-only multi-agent performance orchestrator for coding tasks.')
backend_app=typer.Typer(); task_app=typer.Typer(); policy_app=typer.Typer(); runner_app=typer.Typer(); viewer_app=typer.Typer(help='Local run viewer commands')
app.add_typer(backend_app,name='backend'); app.add_typer(task_app,name='task'); app.add_typer(policy_app,name='policy'); app.add_typer(runner_app,name='runner'); app.add_typer(viewer_app,name='viewer')
console=Console()


def _fmt_tokens(n):
    if n is None:
        return 'unavailable'
    if n >= 1_000_000:
        return f'{n/1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n/1_000:.1f}k'
    return str(n)

def _print_usage_summary(result, verbose=False):
    summary = getattr(getattr(result, 'state', None), 'usage_summary', None) or {}
    if not summary:
        p = Path(getattr(result, 'run_dir', '')) / 'cost_summary.json'
        if p.exists():
            try:
                summary = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                summary = {}
    calls = summary.get('calls_count', 0)
    unavailable = summary.get('unavailable_calls_count', 0)
    if not calls and not summary:
        return
    if not calls or unavailable == calls:
        console.print('Tokens: unavailable')
        console.print('Cost: unavailable')
    else:
        console.print(f"Tokens: {_fmt_tokens(summary.get('input_tokens',0))} input / {_fmt_tokens(summary.get('output_tokens',0))} output / {_fmt_tokens(summary.get('total_tokens',0))} total")
        label = 'partial, ' if unavailable else ''
        console.print(f"Cost: {label}${summary.get('total_cost',0.0):.4f} total (${summary.get('input_cost',0.0):.4f} input, ${summary.get('output_cost',0.0):.4f} output)")
    console.print(f"Usage: {calls} calls tracked, {unavailable} calls unavailable")
    if verbose:
        for title, key in [('Usage by role','by_role'), ('Usage by backend','by_backend')]:
            buckets = summary.get(key) or {}
            if buckets:
                console.print(title + ':')
                for name, b in buckets.items():
                    console.print(f"  {name}: {_fmt_tokens(b.get('total_tokens',0))} tokens, ${b.get('total_cost',0.0):.4f}")

def storage(workspace='.villani-ops'): return FileStorage(workspace)

@app.command()
def init(workspace: str='.villani-ops'):
    storage(workspace).init_workspace(); console.print(f'Initialized Villani Ops workspace at {workspace}')

@backend_app.command('add')
def backend_add(name: str, provider: str=typer.Option(...), base_url: str|None=None, model: str=typer.Option(...), api_key: str|None=None, api_key_env: str|None=None, input_cost: float=0.0, output_cost: float=0.0, roles: str='coding', capability_score: int=0, max_parallel: int=typer.Option(1, '--max-parallel', help='Maximum concurrent calls/tasks allowed for this backend.'), max_tokens: int|None=None, timeout_seconds: int|None=None, workspace: str='.villani-ops'):
    if api_key and api_key_env:
        raise typer.BadParameter('Choose only one of --api-key or --api-key-env')
    if max_parallel < 1 or max_parallel > 32:
        raise typer.BadParameter('--max-parallel must be between 1 and 32')
    if provider=='local' and not api_key and not api_key_env:
        api_key='dummy'
    s=storage(workspace); s.init_workspace(); b=s.load_backends(); b[name]=Backend(name=name,provider=provider,base_url=base_url,model=model,api_key=api_key,api_key_env=api_key_env,input_cost_per_million=input_cost,output_cost_per_million=output_cost,roles=[r.strip() for r in roles.split(',') if r.strip()],capability_score=capability_score,max_parallel=max_parallel,max_tokens=max_tokens,timeout_seconds=timeout_seconds); s.save_backends(b); console.print(f'Added backend {name}')

@backend_app.command('list')
def backend_list(workspace: str='.villani-ops'):
    table=Table('Name','Provider','Model','Roles','Capability','max_parallel','Costs $/M','State','Base URL','API key')
    loaded=list(storage(workspace).load_backends().values())
    for b in loaded: table.add_row(b.name,b.provider,b.model,','.join(b.roles),str(b.capability_score),str(b.max_parallel),f'{b.input_cost_per_million}/{b.output_cost_per_million}','enabled' if b.enabled else 'disabled',b.base_url or '', 'configured' if b.api_key_configured() else 'missing')
    console.print(table)
    for b in loaded: console.print(f'{b.name} capability={b.capability_score} max_parallel={b.max_parallel}')
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
def task_create(repo: str=typer.Option(...), objective: str=typer.Option(...), success_criteria: str|None=None, classify: bool=False, workspace: str='.villani-ops'):
    s=storage(workspace); s.init_workspace(); t=Task(repo_path=str(Path(repo).resolve()), objective=objective, success_criteria=success_criteria); d=s.workspace/'tasks'/t.task_id; d.mkdir(parents=True);
    if classify:
        from villani_ops.classification import TaskClassifier
        backends=s.load_backends()
        try:
            cls, call=TaskClassifier().classify(t, backends, d/'classification.json')
        except ValueError as e:
            raise typer.BadParameter(str(e))
        t.classification=cls
        (d/'classification.raw.txt').write_text(str(call.raw_text))
    (d/'task.json').write_text(t.model_dump_json(indent=2)); console.print(t.task_id)

@policy_app.command('list-defaults')
def policy_list_defaults():
    for k,v in DEFAULT_PROFILES.items(): console.print(f'{k}: {v["summary"]}')
@policy_app.command('show')
def policy_show(name: str): console.print_json(json.dumps(DEFAULT_PROFILES[name]))
@policy_app.command('create-default')
def policy_create_default(name: str=typer.Option('balanced'), workspace: str='.villani-ops'):
    from villani_ops.core.policy import Policy, AttemptPlan
    s=storage(workspace); s.init_workspace(); backends=s.load_backends(); attempts=[AttemptPlan(backend=b.name, max_attempts=1, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, runner='shell') for b in backends.values()]
    pol=Policy(name=name, attempts=attempts); path=s.workspace/'policies'/f'{name}.yaml'; pol.save(path); console.print(f'Created policy at {path}')

@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def run(ctx: typer.Context, repo: str|None=None, task: str|None=typer.Option(None,'--task'), task_id: str|None=None, success_criteria: str|None=None, mode: str=typer.Option('performance', '--mode', help='Execution mode: performance, cheap, balanced, or quality'), runner: str=typer.Option('villani-code', '--runner'), candidate_attempts: int=typer.Option(3, '--candidate-attempts', min=1, max=8), timeout_seconds: int|None=typer.Option(None, '--timeout-seconds', help=f'Maximum run timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).'), classify: bool=typer.Option(True, '--classify/--no-classify'), non_interactive: bool=False, quiet: bool=typer.Option(False, '--quiet'), verbose: bool=typer.Option(False, '--verbose'), orchestrator: str=typer.Option('adaptive', '--orchestrator', help='Orchestrator architecture: adaptive (default; agentic single-task constrained), agentic (decomposition-capable), or graph (explicit legacy). adaptive: Agentic orchestration constrained to the single-task execution path. The orchestrator investigates, plans, attempts, validates, reviews, observes, and retries within the candidate-attempt budget, but cannot decompose the task.'), ui: bool=typer.Option(False, '--ui', help='Start local run viewer'), no_ui: bool=typer.Option(False, '--no-ui', help='Disable local run viewer'), ui_port: int=typer.Option(8765, '--ui-port'), open_ui: bool=typer.Option(False, '--open-ui'), tournament_budget_policy: str=typer.Option('off', '--tournament-budget-policy', help='Tournament budget policy: off, guarded, or planned'), workspace: str='.villani-ops'):
    forbidden = {
        '--policy': '--policy has been replaced by --mode. Use --mode performance|cheap|balanced|quality. Cost policies moved to villani-ops cost-run.',
        '--backend': 'Backend assignment is controlled by the execution policy. Configure backends, then use --mode. Performance orchestration always uses the most capable enabled backend in performance mode.',
        '--human-approval': 'Human approval is not supported in the primary orchestration path. Human approval is not supported in performance orchestration.',
    }
    for arg in ctx.args:
        for opt, msg in forbidden.items():
            if arg == opt or arg.startswith(opt + '='):
                console.print(msg, soft_wrap=True)
                raise typer.Exit(2)
        if arg.startswith('-'):
            console.print(f'Unknown option: {arg}')
            raise typer.Exit(2)
    if ctx.args:
        console.print(f'Unknown argument(s): {" ".join(ctx.args)}')
        raise typer.Exit(2)
    s=storage(workspace)
    viewer_server=None
    viewer_enabled=bool(ui and not no_ui)
    if viewer_enabled:
        from villani_ops.viewer.server import ViewerServer
        class ViewerStorage(FileStorage):
            def create_run_dir(self, run_id):
                nonlocal viewer_server
                p=super().create_run_dir(run_id)
                try:
                    viewer_server=ViewerServer(self.workspace/'runs', port=ui_port).start()
                    url=viewer_server.url(run_id)
                    console.print(f'Live dashboard: {url}')
                    if open_ui:
                        import webbrowser; webbrowser.open(url)
                except Exception as e:
                    console.print(f'Warning: viewer server failed to start: {e}')
                return p
        s=ViewerStorage(workspace)
    if task_id:
        data=json.loads((s.workspace/'tasks'/task_id/'task.json').read_text()); t=Task.model_validate(data); repo=t.repo_path
    else:
        if not repo or not task: raise typer.BadParameter('Provide --task-id or both --repo and --task')
        t=Task(repo_path=str(Path(repo).resolve()), objective=task, success_criteria=success_criteria)
    if mode not in {'performance','cheap','balanced','quality'}:
        raise typer.BadParameter('Invalid mode. Choose one of: performance, cheap, balanced, quality')
    if orchestrator not in {'graph','agentic','adaptive'}:
        raise typer.BadParameter('Invalid orchestrator. Choose one of: adaptive, agentic, graph')
    if tournament_budget_policy not in {'off','guarded','planned'}:
        raise typer.BadParameter('Invalid tournament budget policy. Choose one of: off, guarded, planned')
    if runner != 'villani-code':
        raise typer.BadParameter(f"Runner '{runner}' is registered but not implemented yet." if runner in {"claude-code","pi","aider","codex"} else f"Unsupported runner '{runner}'. Supported runner: villani-code.")
    if orchestrator in {'agentic','adaptive'}:
        from villani_ops.agentic import OpsRunner, OpsRunRequest
        
        s.init_workspace(); backends=s.load_backends()
        result=OpsRunner(s, backends=backends, progress_reporter=AgenticProgressReporter(not quiet, verbose=verbose, console=console)).run(OpsRunRequest(repo_path=str(Path(repo).resolve()), task=t.objective, success_criteria=t.success_criteria, mode=mode, runner=runner, candidate_attempts=candidate_attempts, timeout_seconds=timeout_seconds, workspace=workspace, orchestrator=orchestrator, backends=backends, tournament_budget_policy=tournament_budget_policy))
    else:
        result=VillaniOps(s, progress_reporter=RunProgressReporter(not quiet, verbose=verbose)).run(repo=repo, task=t, candidate_attempts=candidate_attempts, timeout_seconds=timeout_seconds, classify=classify, non_interactive=(non_interactive or not sys.stdin.isatty()), mode=mode, runner=runner)
    d=result.decision
    failure_kind = getattr(getattr(result, 'state', None), 'failure_kind', None)
    if failure_kind:
        msg = getattr(result.state, 'failure_message', None) or getattr(d, 'failure_reason', None) or getattr(d, 'reason', 'Run failed')
        console.print('Villani Ops run failed')
        console.print(f'Reason: {msg}')
        console.print(f'Run directory: {result.run_dir}')
        console.print('Next step: Start the backend server or update the backend configuration.')
        raise typer.Exit(1)
    console.print(f"Result: {'ACCEPTED' if d.accepted else 'FAILED'}")
    console.print(f'Mode: {d.mode}')
    console.print(f'Task: {t.objective}')
    console.print(f"Runner: {runner}")
    if d.mode == 'performance' and d.performance_backend_name and d.performance_backend_model:
        console.print(f"Performance backend: {d.performance_backend_name}/{d.performance_backend_model}")
    elif d.mode in {'cheap', 'balanced', 'quality'}:
        graph_path = d.orchestration_graph_path or 'orchestration_graph.json'
        assignments = [(node, backend) for node, backend in (d.node_backend_assignments or {}).items() if backend]
        if assignments:
            console.print('Backend assignments:')
            for node, backend in assignments[:8]:
                console.print(f"- {node}: {backend}")
            if len(assignments) > 8:
                console.print(f"- ... {len(assignments) - 8} more; see {graph_path}")
            else:
                console.print(f"Backend assignments graph: {graph_path}")
        else:
            console.print(f"Backend assignments: see {graph_path}")
    if getattr(d, 'decomposition_executed', False):
        console.print(f"Subtasks requested/completed: {d.subtask_count}/{len(d.subtasks_executed or [])}")
        console.print(f"Attempts per subtask: {d.attempts_per_subtask}")
        console.print(f"Subtask attempts completed: {d.subtask_attempts_completed}")
        console.print(f"Accepted subtasks: {len(d.subtasks_accepted or [])}/{d.subtask_count}")
    else:
        console.print(f"Candidate attempts requested/completed: {d.candidate_attempts_requested}/{d.candidate_attempts_completed}")
    console.print(f"Winner: {d.winning_attempt_id or 'none'}")
    console.print(f"Controller reason: {d.reason}")
    _print_usage_summary(result, verbose=verbose)
    console.print(f"Run directory: {result.run_dir}")
    if viewer_enabled:
        try:
            from villani_ops.viewer.builder import write_offline_viewer
            viewer_path=write_offline_viewer(Path(result.run_dir))
            console.print(f'Run viewer saved: {viewer_path}')
            if viewer_server:
                console.print('Live dashboard remains available for 30 seconds for final refresh.')
                import time; time.sleep(30); viewer_server.stop()
        except Exception as e:
            console.print(f'Warning: offline viewer could not be written: {e}')


@viewer_app.command('list')
def viewer_list(workspace: str='.villani-ops'):
    runs=(storage(workspace).workspace/'runs')
    table=Table('run_id','status','task','started','result','cost', width=140)
    for rd in sorted([p for p in runs.iterdir() if p.is_dir()], key=lambda p:p.stat().st_mtime, reverse=True)[:25] if runs.exists() else []:
        try:
            from villani_ops.viewer.adapter import build_viewer_snapshot
            snap=build_viewer_snapshot(rd); r=snap.get('run',{}); u=snap.get('usage',{})
            res=r.get('result') or {}; result=(res.get('decision') if isinstance(res,dict) else '') or ''
            table.add_row(rd.name, str(r.get('status','')), str(r.get('task',''))[:50], str(r.get('started_at','')), str(result), f"${u.get('total_cost',0) or 0:.4f}")
        except Exception: table.add_row(rd.name,'unknown','','','','')
    console.print(table)

@viewer_app.command('serve')
def viewer_serve(port: int=typer.Option(8765,'--port'), workspace: str='.villani-ops'):
    from villani_ops.viewer.server import serve_forever
    serve_forever(storage(workspace).workspace/'runs', port=port)

@viewer_app.command('open')
def viewer_open(run_id: str, port: int=typer.Option(8765,'--port'), open_browser: bool=typer.Option(False,'--open'), workspace: str='.villani-ops'):
    from villani_ops.viewer.server import ViewerServer
    srv=ViewerServer(storage(workspace).workspace/'runs', port=port).start(); url=srv.url(run_id); console.print(url)
    if open_browser:
        import webbrowser; webbrowser.open(url)
    try:
        import time
        while True: time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()


@app.command('cost-run')
def cost_run(repo: str|None=None, task: str|None=typer.Option(None,'--task'), task_id: str|None=None, policy: str='balanced', max_attempts: int|None=typer.Option(None, '--max-attempts', min=1, max=10), success_criteria: str|None=None, isolation: str='worktree', workspace: str='.villani-ops', legacy_yaml_policy: bool=False, human_approval: bool=typer.Option(False, '--human-approval/--no-human-approval'), non_interactive: bool=False):
    s=storage(workspace)
    if task_id:
        data=json.loads((s.workspace/'tasks'/task_id/'task.json').read_text()); t=Task.model_validate(data); repo=t.repo_path
    else:
        if not repo or not task: raise typer.BadParameter('Provide --task-id or both --repo and --task')
        t=Task(repo_path=str(Path(repo).resolve()), objective=task, success_criteria=success_criteria)
    is_yaml = policy.endswith(('.yaml','.yml')) or Path(policy).suffix in {'.yaml','.yml'}
    if is_yaml:
        if not legacy_yaml_policy:
            raise typer.BadParameter('YAML policy files use legacy smoke-test mode. Re-run with --legacy-yaml-policy if you intentionally want that path. It does not provide LLM task validation.')
        result=_legacy_run(repo, t, policy, workspace)
    else:
        result=CostPolicyVillaniOps(storage(workspace), progress_reporter=RunProgressReporter(True)).run(repo=repo, task=t, policy=policy, isolation=isolation, human_approval=human_approval, non_interactive=(non_interactive or not sys.stdin.isatty()), max_attempts=max_attempts)
    d=result.decision; strat=d.execution_strategy or {}; console.print(f"Result: {'ACCEPTED' if d.accepted else 'REJECTED' if (policy.endswith('.yaml') or '/' in policy) else 'FAILED'}"); console.print(f"Final state: {d.final_state}"); console.print(f"Final action: {d.final_action}"); console.print(f'Task: {t.objective}'); c=d.classification or {}; console.print(f"Classification: {c.get('difficulty')} {c.get('category')} {c.get('risk')}"); console.print(f"Policy: {policy}"); console.print(f"Max attempts: {strat.get('max_attempts') or len(strat.get('attempts',[]))}"); console.print(f"Planned attempts: {len(strat.get('attempts',[]))}"); console.print(f"Attempts used: {d.attempts_used}"); console.print(f"Retries used: {d.retries_used}"); console.print(f"Escalations used: {d.escalations_used}"); console.print(f"Human reviews requested/skipped: {d.human_reviews_requested}/{d.human_reviews_skipped}"); console.print(f"Winner: {d.winning_attempt_id or 'none'}"); console.print(f"Review: {d.reviewer_decision}, score {d.reviewer_score}"); console.print(f"Human override used: {d.human_override_used}"); console.print(f"Controller reason: {d.reason}"); console.print(f"Cost: total ${d.total_cost:.6f}"); console.print('Evidence:'); [console.print(f'  - {e}') for e in d.reviewer_evidence]
    if d.accepted:
        console.print(f"Apply:\n  villani-ops apply {d.run_id}"); console.print(f"Branch:\n  villani-ops branch {d.run_id} --name villani-ops/{d.run_id}"); console.print(f"PR:\n  villani-ops pr {d.run_id} --title \"{(t.objective or 'Villani Ops changes')[:60]}\"")
    else:
        console.print(f"Failure reason: {d.failure_reason}")
        console.print(f"Best failed attempt: {(d.attempts[-1] or {}).get('attempt_id') if d.attempts else 'none'}")
    console.print(f'Report: {result.report_path}')



def _run_dir_for(s, run_id: str) -> Path:
    return s.resolve_latest_run() if run_id in {'latest','runs/latest'} else s.workspace/'runs'/run_id

def _redact(text: str) -> str:
    import re
    return re.sub(r'(gh[pousr]_[A-Za-z0-9_]+)', '***REDACTED***', text or '')

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
    report=run_dir/'report.md'; report.write_text('LEGACY MODE: This run used diff_review smoke validation, not LLM task validation.\n' + ('ACCEPTED' if d.accepted else 'REJECTED')+'\ndiff_smoke_check\n'+'\n'.join(warnings)+'\n'+ '\n'.join([a.diff_path or '' for a in attempts]))
    return SimpleNamespace(run_id=run_id,run_dir=str(run_dir),decision=d,report_path=str(report),attempts=attempts)

@app.command()
def apply(run_id: str, branch: str|None=None, commit: bool=False, message: str|None=None, force: bool=False, force_branch: bool=False, workspace: str='.villani-ops'):
    from villani_ops.git_ops import safe_apply
    s=storage(workspace); run_dir=_run_dir_for(s, run_id)
    try:
        safe_apply(run_dir, branch=branch, commit=commit, message=message, force=force, force_branch=force_branch, artifact_name='apply.json')
    except Exception as e:
        raise typer.BadParameter(str(e))
    console.print('Applied patch')

@app.command()
def branch(run_id: str, name: str=typer.Option(...), commit: bool=False, force: bool=False, force_branch: bool=False, workspace: str='.villani-ops'):
    from villani_ops.git_ops import safe_apply
    s=storage(workspace); run_dir=_run_dir_for(s, run_id)
    try:
        safe_apply(run_dir, branch=name, commit=commit, force=force, force_branch=force_branch, artifact_name='branch.json')
    except Exception as e:
        raise typer.BadParameter(str(e))
    console.print(f'Created branch {name}')

@app.command()
def pr(run_id: str, title: str=typer.Option(...), body: str=typer.Option(''), branch: str|None=None, no_push: bool=False, prepare_branch: bool=typer.Option(False, '--prepare-branch'), force: bool=False, force_branch: bool=False, workspace: str='.villani-ops'):
    from datetime import datetime, timezone
    from villani_ops.git_ops import safe_apply, resolve_accepted, manual_pr_commands
    s=storage(workspace); run_dir=_run_dir_for(s, run_id); run_id=run_dir.name
    try:
        d, repo, patch=resolve_accepted(run_dir)
    except Exception as e:
        raise typer.BadParameter(str(e))
    branch_name=branch or f'villani-ops-pr/{run_id}'
    manual=manual_pr_commands(branch_name,title,body)
    recovery=['Run git status before continuing.', f'Use manual commands for branch {branch_name} if needed.']
    gh=shutil.which('gh')
    if not gh and not no_push and not prepare_branch:
        art={'attempted':False,'gh_available':False,'branch':branch_name,'title':title,'body':body,'push_skipped':False,'exit_code':127,'stdout':'','stderr':'gh CLI is unavailable; no mutation performed without --prepare-branch','url':None,'manual_commands':manual,'recovery_instructions':recovery,'created_at':datetime.now(timezone.utc).isoformat()}
        (run_dir/'pr.json').write_text(json.dumps(art, indent=2)); console.print('gh not available; no mutation performed. Re-run with --prepare-branch or run manually:\n'+'\n'.join(manual)); raise typer.Exit(1)
    try:
        safe_apply(run_dir, branch=branch_name, commit=True, message=title, force=force, force_branch=force_branch, artifact_name='pr_apply.json')
    except Exception as e:
        art={'attempted':True,'gh_available':bool(gh),'branch':branch_name,'title':title,'body':body,'push_skipped':no_push,'exit_code':1,'stdout':'','stderr':str(e),'url':None,'manual_commands':manual,'recovery_instructions':recovery,'created_at':datetime.now(timezone.utc).isoformat()}
        (run_dir/'pr.json').write_text(json.dumps(art, indent=2)); raise typer.BadParameter(str(e))
    if no_push or (prepare_branch and not gh):
        art={'attempted':False,'gh_available':bool(gh),'branch':branch_name,'title':title,'body':body,'push_skipped':True,'commit_sha':subprocess.run(['git','rev-parse','HEAD'],cwd=repo,text=True,capture_output=True).stdout.strip(),'exit_code':0,'stdout':'','stderr':'','url':None,'manual_commands':manual,'recovery_instructions':recovery,'created_at':datetime.now(timezone.utc).isoformat()}
        (run_dir/'pr.json').write_text(json.dumps(art, indent=2)); console.print('\n'.join(manual)); return
    push=subprocess.run(['git','push','-u','origin',branch_name],cwd=repo,text=True,capture_output=True)
    if push.returncode!=0:
        art={'attempted':True,'gh_available':True,'branch':branch_name,'title':title,'body':body,'push_skipped':False,'commit_sha':subprocess.run(['git','rev-parse','HEAD'],cwd=repo,text=True,capture_output=True).stdout.strip(),'exit_code':push.returncode,'stdout':push.stdout,'stderr':push.stderr,'url':None,'manual_commands':manual,'recovery_instructions':recovery,'created_at':datetime.now(timezone.utc).isoformat()}
        (run_dir/'pr.json').write_text(json.dumps(art, indent=2)); raise typer.Exit(push.returncode)
    proc=subprocess.run([gh,'pr','create','--title',title,'--body',body],cwd=repo,text=True,capture_output=True)
    out=_redact(proc.stdout); err=_redact(proc.stderr); url=next((l for l in out.splitlines() if l.startswith('http')), None)
    art={'attempted':True,'gh_available':True,'branch':branch_name,'title':title,'body':body,'push_skipped':False,'commit_sha':subprocess.run(['git','rev-parse','HEAD'],cwd=repo,text=True,capture_output=True).stdout.strip(),'exit_code':proc.returncode,'stdout':out,'stderr':err,'url':url,'manual_commands':manual,'recovery_instructions':recovery,'created_at':datetime.now(timezone.utc).isoformat()}
    (run_dir/'pr.json').write_text(json.dumps(art, indent=2))
    if proc.returncode!=0: raise typer.Exit(proc.returncode)
    console.print(out or 'PR created')

@app.command()
def report(run_id_or_latest: str, workspace: str='.villani-ops'):
    s=storage(workspace); run_dir=s.resolve_latest_run() if run_id_or_latest in {'latest','runs/latest'} else s.workspace/'runs'/run_id_or_latest; console.print((Path(run_dir)/'report.md').read_text())

@app.command()
def compare(repo: str=typer.Option(...), tasks: str=typer.Option(...), policies: list[str]=typer.Option(['cheap','balanced','quality']), max_attempts: int|None=typer.Option(None, '--max-attempts', min=1, max=10), out: str='.villani-ops/reports/comparison.md', workspace: str='.villani-ops', non_interactive: bool=True, resume: bool=False, max_tasks: int|None=None, repeat: int=1):
    import csv, statistics
    s=storage(workspace); s.init_workspace(); out_path=Path(out).expanduser().resolve(); out_path.parent.mkdir(parents=True, exist_ok=True)
    task_rows=[json.loads(line) for line in Path(tasks).read_text().splitlines() if line.strip()]
    if max_tasks is not None: task_rows=task_rows[:max_tasks]
    json_path=out_path.with_suffix('.json'); csv_path=out_path.with_suffix('.csv')
    results=[]
    done=set()
    if resume and json_path.exists():
        results=json.loads(json_path.read_text()); done={(r.get('policy'),r.get('task_id'),r.get('trial_index')) for r in results}
    before=subprocess.run(['git','rev-parse','HEAD'],cwd=Path(repo),text=True,capture_output=True).stdout.strip()
    for trial in range(repeat):
        for td in task_rows:
            for pol in policies:
                key=(pol,td.get('id'),trial)
                if key in done: continue
                t=Task(task_id=td.get('id') or None, repo_path=str(Path(repo).resolve()), objective=td.get('objective'), success_criteria=td.get('success_criteria'))
                start_t=__import__('time').time(); d=None; err=''
                try:
                    rr=CostPolicyVillaniOps.from_workspace(s.workspace).run(repo=repo, task=t, policy=pol, non_interactive=non_interactive, max_attempts=max_attempts)
                    d=rr.decision
                except Exception as e:
                    err=str(e)
                winner=getattr(d,'winning_attempt_id',None) if d else None
                cls=(getattr(d,'classification',{}) or {}) if d else {}
                win=next((a for a in (getattr(d,'attempts',[]) if d else []) if a.get('attempt_id')==winner), {})
                strat=(getattr(d,'execution_strategy',{}) or {}) if d else {}; planned=strat.get('attempts') or []
                results.append({'comparison_id':out_path.stem,'policy':pol,'max_attempts':strat.get('max_attempts') or max_attempts,'planned_attempt_count':len(planned),'planned_backends':','.join(a.get('backend','') for a in planned),'estimated_policy_cost':sum(float(a.get('estimated_attempt_cost') or 0) for a in planned),'actual_total_cost':getattr(d,'total_cost',0) if d else 0,'actual_coding_cost':getattr(d,'coding_cost',0) if d else 0,'winner_backend': next((a.get('backend_name') for a in (getattr(d,'attempts',[]) if d else []) if a.get('attempt_id')==winner), None),'winning_backend': next((a.get('backend_name') for a in (getattr(d,'attempts',[]) if d else []) if a.get('attempt_id')==winner), None),'task_id':td.get('id'),'run_id':getattr(d,'run_id','') if d else '','trial_index':trial,'accepted':getattr(d,'accepted',False) if d else False,'final_action':getattr(d,'final_action','fail') if d else 'fail','total_cost':getattr(d,'total_cost',0) if d else 0,'classification_cost':getattr(d,'classification_cost',0) if d else 0,'policy_cost':getattr(d,'policy_cost',0) if d else 0,'coding_cost':getattr(d,'coding_cost',0) if d else 0,'review_cost':getattr(d,'review_cost',0) if d else 0,'total_input_tokens':getattr(d,'total_input_tokens',0) if d else 0,'total_output_tokens':getattr(d,'total_output_tokens',0) if d else 0,'coding_input_tokens':getattr(d,'total_coding_input_tokens',0) if d else 0,'coding_output_tokens':getattr(d,'total_coding_output_tokens',0) if d else 0,'token_accounting_statuses':json.dumps(getattr(d,'token_accounting_statuses',{}) if d else {}, sort_keys=True),'winning_attempt_input_tokens':win.get('input_tokens'),'winning_attempt_output_tokens':win.get('output_tokens'),'winning_attempt_coding_cost':win.get('coding_cost'),'winning_attempt_duration_ms':win.get('duration_ms'),'winning_attempt_token_accounting_status':win.get('token_accounting_status'),'attempts_used':getattr(d,'attempts_used',getattr(d,'total_attempts',0)) if d else 0,'retries_used':getattr(d,'retries_used',0) if d else 0,'escalations_used':getattr(d,'escalations_used',0) if d else 0,'strategy_summary':strat.get('strategy_summary','') if d else '','winning_model': next((a.get('model') for a in (getattr(d,'attempts',[]) if d else []) if a.get('attempt_id')==winner), None),'difficulty':cls.get('difficulty'),'category':cls.get('category'),'risk':cls.get('risk'),'reviewer_score':getattr(d,'reviewer_score',None) if d else None,'wall_time_seconds':__import__('time').time()-start_t,'report_path':(str(Path(s.workspace)/'runs'/getattr(d,'run_id','')/'report.md') if d else ''),'failure_reason':err or ('' if getattr(d,'accepted',False) else (getattr(d,'failure_reason','') or getattr(d,'reason','')))})
    after=subprocess.run(['git','rev-parse','HEAD'],cwd=Path(repo),text=True,capture_output=True).stdout.strip()
    if before and after and before!=after: raise typer.BadParameter('compare mutated source repo HEAD')
    json_path.write_text(json.dumps(results, indent=2))
    fields=['comparison_id','task_id','trial_index','policy','max_attempts','planned_attempt_count','planned_backends','estimated_policy_cost','actual_total_cost','actual_coding_cost','run_id','accepted','final_action','failure_reason','difficulty','category','risk','strategy_summary','attempts_used','retries_used','escalations_used','winner_backend','winning_backend','winning_model','reviewer_score','total_cost','classification_cost','policy_cost','coding_cost','review_cost','total_input_tokens','total_output_tokens','coding_input_tokens','coding_output_tokens','token_accounting_statuses','winning_attempt_input_tokens','winning_attempt_output_tokens','winning_attempt_coding_cost','winning_attempt_duration_ms','winning_attempt_token_accounting_status','wall_time_seconds','report_path']
    with csv_path.open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(results)
    lines=['# Villani Ops Comparison','','| Policy | Tasks run | Accepted solves | Solve rate | Total cost | Cost per accepted solve | Tokens per accepted solve | Attempts per accepted solve | Retries per accepted solve | Escalations per accepted solve | Median wall time | Average reviewer score |','| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for pol in policies:
        rs=[r for r in results if r['policy']==pol]; n=len(rs); acc=[r for r in rs if r['accepted']]
        total=sum(r['total_cost'] for r in rs); toks=sum(r['total_input_tokens']+r['total_output_tokens'] for r in rs); attempts=sum(r['attempts_used'] for r in rs); retries=sum(r.get('retries_used',0) for r in rs); escalations=sum(r.get('escalations_used',0) for r in rs)
        cpa='N/A' if not acc else f"${total/len(acc):.6f}"; tpa='N/A' if not acc else f"{toks/len(acc):.1f}"; apa='N/A' if not acc else f"{attempts/len(acc):.1f}"; rpa='N/A' if not acc else f"{retries/len(acc):.1f}"; epa='N/A' if not acc else f"{escalations/len(acc):.1f}"
        med=statistics.median([r['wall_time_seconds'] for r in rs]) if rs else 0; scores=[r['reviewer_score'] for r in rs if r['reviewer_score'] is not None]; avg=sum(scores)/len(scores) if scores else 0
        lines.append(f"| {pol} | {n} | {len(acc)} | {(len(acc)/n*100 if n else 0):.1f}% | ${total:.6f} | {cpa} | {tpa} | {apa} | {rpa} | {epa} | {med:.2f}s | {avg:.2f} |")
    from collections import Counter
    fail=Counter(r.get('failure_reason') or 'none' for r in results if not r.get('accepted'))
    cat=Counter((r.get('category') or 'unknown') for r in results)
    diff=Counter((r.get('difficulty') or 'unknown') for r in results)
    lines += ['', '## Failure reason breakdown'] + [f'- {k}: {v}' for k,v in fail.items()]
    lines += ['', '## Category/difficulty breakdown'] + [f'- category {k}: {v}' for k,v in cat.items()] + [f'- difficulty {k}: {v}' for k,v in diff.items()]
    def pol_stats(name):
        rs=[r for r in results if r['policy']==name]; acc=[r for r in rs if r['accepted']]; total=sum(r['total_cost'] for r in rs); return len(acc), len(rs), (total/len(acc) if acc else None)
    cheap=pol_stats('cheap'); bal=pol_stats('balanced'); qual=pol_stats('quality')
    bal_cost='N/A' if bal[2] is None else '${:.6f}'.format(bal[2])
    qual_cost='N/A' if qual[2] is None else '${:.6f}'.format(qual[2])
    lines += ['', '## Thesis Signal', f'- Cheapest policy solve rate: {cheap[0]}/{cheap[1]}', f'- Quality policy solve rate: {qual[0]}/{qual[1]}', f'- Balanced policy cost per accepted solve: {bal_cost}', f'- Quality policy cost per accepted solve: {qual_cost}', f'Balanced solved {bal[0]}/{bal[1]} at {bal_cost} per accepted solve versus Quality solving {qual[0]}/{qual[1]} at {qual_cost} per accepted solve.']
    out_path.write_text('\n'.join(lines)+'\n')
    console.print(f'Wrote {out_path}, {csv_path}, {json_path}')


if __name__=='__main__': app()
