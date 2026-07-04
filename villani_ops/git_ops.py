from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import json, subprocess, shutil
from villani_ops.isolation.copy_git import source_is_git_repo

def run_git(repo: Path, args: list[str], check: bool=False):
    p=subprocess.run(['git',*args],cwd=repo,text=True,capture_output=True)
    if check and p.returncode!=0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p

def dirty(repo: Path)->bool:
    if not source_is_git_repo(repo): return False
    return bool(run_git(repo,['status','--porcelain']).stdout.strip())

def branch_exists(repo: Path, name: str)->bool:
    return run_git(repo,['rev-parse','--verify',name]).returncode==0

def resolve_accepted(run_dir: Path):
    d=json.loads((run_dir/'decision.json').read_text())
    if not d.get('accepted') or not d.get('winning_patch_path'):
        raise ValueError('No accepted attempt exists')
    patch=Path(d['winning_patch_path'])
    if not patch.exists():
        raise FileNotFoundError(f'Accepted patch does not exist: {patch}')
    repo=Path(json.loads((run_dir/'task.json').read_text())['repo_path'])
    return d, repo, patch

def _snapshot(repo: Path, patch: Path|None, accepted_attempt_id: str|None, branch: str|None):
    is_repo=source_is_git_repo(repo)
    return {
        'is_git_repo': is_repo,
        'current_branch': run_git(repo,['branch','--show-current']).stdout.strip() if is_repo else None,
        'current_head': run_git(repo,['rev-parse','HEAD']).stdout.strip() if is_repo else None,
        'dirty_status': run_git(repo,['status','--porcelain']).stdout if is_repo else None,
        'target_branch_exists': branch_exists(repo, branch) if branch and is_repo else False,
        'patch_path': str(patch) if patch else None,
        'accepted_attempt_id': accepted_attempt_id,
    }

def safe_apply(run_dir: Path, *, branch: str|None=None, commit: bool=False, message: str|None=None, force: bool=False, force_branch: bool=False, artifact_name: str='apply.json'):
    run_id=run_dir.name; stdout=[]; stderr=[]; commit_sha=None; mutated=False; created_branch=False; rollback_attempted=False; rollback_succeeded=False
    repo=None; patch=None; d={}
    try:
        d, repo, patch=resolve_accepted(run_dir)
        pre=_snapshot(repo, patch, d.get('winning_attempt_id'), branch)
        is_repo=source_is_git_repo(repo)
        if dirty(repo) and not force: raise ValueError('Source repo is dirty; use --force')
        if branch and not is_repo: raise ValueError('Branch integration requires a Git source repository')
        chk=run_git(repo,['apply','--check',str(patch)])
        stdout.append(chk.stdout); stderr.append(chk.stderr)
        if chk.returncode!=0: raise RuntimeError('git apply --check failed; repository was not mutated: '+(chk.stderr.strip() or chk.stdout.strip()))
        if branch:
            if branch_exists(repo, branch):
                if not force_branch: raise ValueError(f"Branch '{branch}' already exists; use --force-branch")
                run_git(repo,['branch','-D',branch],check=True)
            p=run_git(repo,['checkout','-b',branch],check=True); stdout.append(p.stdout); stderr.append(p.stderr); created_branch=True; mutated=True
        ap=run_git(repo,['apply',str(patch)],check=True); stdout.append(ap.stdout); stderr.append(ap.stderr); mutated=True
        if commit and not is_repo: raise ValueError('Commit integration requires a Git source repository')
        if commit:
            run_git(repo,['add','.'],check=True)
            cm=run_git(repo,['commit','-m',message or f'Apply Villani Ops run {run_id}'],check=True); stdout.append(cm.stdout); stderr.append(cm.stderr)
            commit_sha=run_git(repo,['rev-parse','HEAD'],check=True).stdout.strip()
        art={'attempted':True,'run_id':run_id,'pre_mutation':pre,'patch_path':str(patch),'branch':branch,'commit':commit,'commit_sha':commit_sha,'exit_code':0,'stdout':'\n'.join(stdout),'stderr':'\n'.join(stderr),'rollback_attempted':False,'rollback_succeeded':False,'recovery_instructions':[],'created_at':datetime.now(timezone.utc).isoformat()}
        (run_dir/artifact_name).write_text(json.dumps(art,indent=2)); return art
    except Exception as e:
        pre = locals().get('pre') or (_snapshot(repo, patch, d.get('winning_attempt_id'), branch) if repo else {})
        recovery=[]
        if repo and created_branch:
            rollback_attempted=True
            original=pre.get('current_branch')
            try:
                if original: run_git(repo,['checkout',original],check=True)
                if created_branch and branch and not dirty(repo): run_git(repo,['branch','-D',branch],check=False)
                rollback_succeeded=True
            except Exception as rb:
                recovery.append(f'Rollback failed: {rb}')
        if repo:
            recovery += [f'Inspect repository: {repo}', 'Run git status before continuing.', f'Patch path: {patch}']
            if branch: recovery.append(f'If needed, switch back with: git checkout {pre.get("current_branch")}')
        art={'attempted':True,'run_id':run_id,'pre_mutation':pre,'patch_path':str(patch) if patch else None,'branch':branch,'commit':commit,'commit_sha':commit_sha,'exit_code':1,'stdout':'\n'.join(stdout),'stderr':str(e),'rollback_attempted':rollback_attempted,'rollback_succeeded':rollback_succeeded,'recovery_instructions':recovery,'created_at':datetime.now(timezone.utc).isoformat()}
        (run_dir/artifact_name).write_text(json.dumps(art,indent=2)); raise

def manual_pr_commands(branch,title,body):
    return [f'git push -u origin {branch}', f'gh pr create --title {title!r} --body {body!r}']
