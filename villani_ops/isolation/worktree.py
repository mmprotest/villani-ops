from __future__ import annotations
from pathlib import Path
import subprocess, json

def _run(args, cwd, log):
    p=subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    log.append({"cmd":args,"cwd":str(cwd),"returncode":p.returncode,"stdout":p.stdout,"stderr":p.stderr})
    if p.returncode!=0: raise RuntimeError(f"Command failed: {' '.join(args)}\n{p.stderr}")
    return p.stdout.strip()

class GitWorktreeIsolation:
    def create(self, source_repo: str|Path, run_id: str, attempt_id: str, workspace: str|Path) -> dict:
        repo=Path(source_repo).resolve(); log=[]
        top=_run(['git','rev-parse','--show-toplevel'], repo, log)
        if Path(top).resolve()!=repo: repo=Path(top).resolve()
        base=_run(['git','rev-parse','HEAD'], repo, log)
        _run(['git','status'], repo, log)
        wt=Path(workspace)/'worktrees'/run_id/attempt_id; wt.parent.mkdir(parents=True, exist_ok=True)
        branch=f"villani-ops/{run_id}/{attempt_id}"
        _run(['git','worktree','add','-b',branch,str(wt),'HEAD'], repo, log)
        return {"repo_path":str(repo),"worktree_path":str(wt.resolve()),"branch_name":branch,"base_commit":base,"commands":log}

def capture_worktree(repo_path: str|Path, out_dir: str|Path) -> dict:
    repo=Path(repo_path); out=Path(out_dir); log=[]
    status=_run(['git','status','--porcelain'], repo, log)
    diff=subprocess.run(['git','diff','--binary','HEAD'], cwd=repo, text=True, capture_output=True)
    names=subprocess.run(['git','diff','--name-only','HEAD'], cwd=repo, text=True, capture_output=True)
    log += [{"cmd":['git','diff','--binary','HEAD'],"returncode":diff.returncode,"stdout":"<patch>","stderr":diff.stderr},{"cmd":['git','diff','--name-only','HEAD'],"returncode":names.returncode,"stdout":names.stdout,"stderr":names.stderr}]
    patch=out/'diff.patch'; changed=out/'changed_files.json'; git_status=out/'git_status.txt'; cmds=out/'git_commands.json'
    patch.write_text(diff.stdout); files=[x for x in names.stdout.splitlines() if x]; changed.write_text(json.dumps(files, indent=2)); git_status.write_text(status); cmds.write_text(json.dumps(log, indent=2))
    return {"patch_path":str(patch),"changed_files":files,"changed_files_path":str(changed),"git_status":status,"git_status_path":str(git_status),"git_commands_path":str(cmds)}
