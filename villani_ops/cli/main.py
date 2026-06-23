from __future__ import annotations
from pathlib import Path
import typer
from rich.console import Console
from rich.table import Table
from villani_ops import VillaniOps, Task
from villani_ops.core.backend import Backend
from villani_ops.core.policy import Policy, AttemptPlan
from villani_ops.storage.files import FileStorage

app = typer.Typer(help="Villani Ops: cost-aware AI coding operations.")
backend_app = typer.Typer(help="Manage local backend configs."); app.add_typer(backend_app, name="backend")
runner_app = typer.Typer(help="Manage runner command templates."); app.add_typer(runner_app, name="runner")
policy_app = typer.Typer(help="Manage policies."); app.add_typer(policy_app, name="policy")
console=Console()

def storage(workspace: str = ".villani-ops") -> FileStorage: return FileStorage(workspace)

@app.command()
def init(workspace: str = ".villani-ops"):
    s=storage(workspace); s.init_workspace(); console.print(f"Initialized Villani Ops workspace at {s.workspace}")

@backend_app.command("add")
def backend_add(name: str, provider: str = typer.Option(...), model: str = typer.Option(...), base_url: str|None = typer.Option(None), input_cost: float = typer.Option(...), output_cost: float = typer.Option(...), workspace: str = ".villani-ops"):
    s=storage(workspace); s.init_workspace(); b=s.load_backends(); b[name]=Backend(name=name, provider=provider, base_url=base_url, model=model, input_cost_per_million=input_cost, output_cost_per_million=output_cost); s.save_backends(b); console.print(f"Added backend {name}")

@backend_app.command("list")
def backend_list(workspace: str = ".villani-ops"):
    b=storage(workspace).load_backends(); table=Table("Name","Provider","Model","Input $/M","Output $/M")
    for x in b.values(): table.add_row(x.name,x.provider,x.model,str(x.input_cost_per_million),str(x.output_cost_per_million))
    console.print(table)

@runner_app.command("set")
def runner_set(name: str, command: str = typer.Option(...), workspace: str = ".villani-ops"):
    s=storage(workspace); s.init_workspace(); cfg=s.load_config(); cfg.setdefault("runners",{}).setdefault(name,{})["command"]=command; s.save_config(cfg); console.print(f"Set runner {name}")

@runner_app.command("list")
def runner_list(workspace: str = ".villani-ops"):
    cfg=storage(workspace).load_config(); table=Table("Runner","Command")
    for name,val in (cfg.get("runners") or {}).items(): table.add_row(name, str((val or {}).get("command")))
    console.print(table)

@policy_app.command("create-default")
def policy_create_default(name: str = typer.Option(...), workspace: str = ".villani-ops"):
    s=storage(workspace); s.init_workspace(); backends=s.load_backends(); attempts=[AttemptPlan(backend=b.name, max_attempts=1, timeout_seconds=900, runner="shell") for b in backends.values()]
    p=Policy(name=name, attempts=attempts); path=s.workspace/"policies"/f"{name}.yaml"; p.save(path); console.print(f"Created policy at {path}")
    if not attempts: console.print("No backends configured; policy has no attempts. Add a backend before running.")

@app.command()
def run(repo: str = typer.Option(...), task: str = typer.Option(...), policy: str = typer.Option(...), success_criteria: str|None = typer.Option(None), workspace: str = ".villani-ops"):
    pol=Policy.load(policy); result=VillaniOps.from_workspace(workspace).run(repo=repo, task=Task(repo_path=repo, instruction=task, success_criteria=success_criteria), policy=pol)
    console.print(f"Run {result.run_id}: {'ACCEPTED' if result.decision.accepted else 'REJECTED'}")
    console.print(f"Report: {result.report_path}")

@app.command()
def report(run_id_or_latest: str, workspace: str = ".villani-ops"):
    s=storage(workspace); target=run_id_or_latest
    run_dir=s.resolve_latest_run() if target in {"latest","runs/latest"} else s.workspace/"runs"/target
    if not run_dir or not Path(run_dir).exists(): raise typer.BadParameter("Run not found")
    console.print(f"Report: {Path(run_dir)/'report.md'}")
    console.print((Path(run_dir)/"report.md").read_text())

if __name__ == "__main__": app()
