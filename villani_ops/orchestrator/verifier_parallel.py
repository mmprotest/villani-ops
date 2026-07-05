from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import json, secrets, subprocess, sys, time, shutil
from villani_ops.isolation.copy_git import create_git_baselined_copy, capture_candidate_patch
from villani_ops.runners import runner_for_name
from villani_ops.storage.files import FileStorage
from villani_ops.core.task import Task
from villani_ops.core.backend import Backend
from villani_ops.git_ops import safe_apply
from .selection import select_winner, POLICY



def _is_verifier_debug_dir(path: Path) -> bool:
    return path.is_dir() and (path / "session_meta.json").exists()

def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0

def _debug_dir_score(path: Path) -> tuple[int, float, str]:
    score = 0
    if (path / "session_meta.json").exists():
        score += 10
    if (path / "final_summary.json").exists():
        score += 5
    if (path / "commands.jsonl").exists():
        score += 2
    if (path / "tool_calls.jsonl").exists():
        score += 2
    mtimes = [_safe_mtime(path / name) for name in ("final_summary.json", "session_meta.json", "summary.json") if (path / name).exists()]
    if not mtimes:
        mtimes.append(_safe_mtime(path))
    return (score, max(mtimes), path.name)

def resolve_verifier_debug_dir(debug_root: Path | None, resolved_trace_dir: Path | None = None) -> Path | None:
    if resolved_trace_dir and _is_verifier_debug_dir(resolved_trace_dir):
        return resolved_trace_dir
    candidates: list[Path] = []
    if debug_root and _is_verifier_debug_dir(debug_root):
        candidates.append(debug_root)
    if debug_root and debug_root.exists():
        children = [child for child in debug_root.iterdir() if child.is_dir()]
        candidates.extend(child for child in children if _is_verifier_debug_dir(child))
        for child in children:
            candidates.extend(grandchild for grandchild in child.iterdir() if grandchild.is_dir() and _is_verifier_debug_dir(grandchild))
    if not candidates:
        return None
    return max(set(candidates), key=_debug_dir_score)

def _debug_resolution(debug_root: Path | None, resolved_trace_dir: Path | None) -> tuple[Path | None, str, str]:
    resolved = resolve_verifier_debug_dir(debug_root, resolved_trace_dir)
    if resolved is None:
        root = debug_root or resolved_trace_dir
        return None, "missing", f"No verifier-compatible Villani Code debug trace found. Expected session_meta.json under {root} or one of its child trace directories."
    if resolved_trace_dir and resolved == resolved_trace_dir:
        return resolved, "resolved", "selected runner resolved_trace_dir containing session_meta.json"
    if debug_root and resolved == debug_root:
        return resolved, "resolved", "selected debug root containing session_meta.json"
    return resolved, "resolved", "selected nested trace directory containing session_meta.json"

def now(): return datetime.now(timezone.utc).isoformat()
def write_json(p:Path,o): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(o,indent=2,default=str),encoding='utf-8')
def append_jsonl(p:Path,o): p.parent.mkdir(parents=True,exist_ok=True); p.open('a',encoding='utf-8').write(json.dumps(o,default=str)+'\n')

@dataclass
class VerifierParallelConfig:
    repo: Path; task: str; candidates:int=5; parallelism:int|None=None; seed:int|None=None; workspace:Path=Path('.villani-ops'); agent:str='villani-code'; backend:str|None=None; verifier_backend:str|None=None; candidate_timeout_seconds:int|None=None; verifier_timeout_seconds:int=180; verifier_max_tool_calls:int=12; on_all_fail:str='fail'; keep_worktrees:bool=False; out:Path|None=None

@dataclass
class CandidateResult:
    candidate_id:str; worktree_path:Path; run_status:str='pending'; debug_root:Path|None=None; debug_dir:Path|None=None; resolved_trace_dir:Path|None=None; debug_resolution_status:str|None=None; debug_resolution_reason:str|None=None; verifier_result:dict|None=None; verifier_trace_dir:Path|None=None; error:str|None=None; artifacts_dir:Path|None=None; started_at:str|None=None; completed_at:str|None=None; exit_code:int|None=None; stdout_path:Path|None=None; stderr_path:Path|None=None; duration_seconds:float|None=None; changed_files:list[str]|None=None; patch_path:Path|None=None; patch_status:str|None=None

class VerifierParallelOrchestrator:
    def __init__(self, config:VerifierParallelConfig, runner=None, verifier=None, integrator=None):
        self.config=config; self.runner=runner; self.verifier=verifier; self.integrator=integrator
    def _backend_obj(self):
        s=FileStorage(self.config.workspace); s.init_workspace(); backs=s.load_backends()
        if self.config.backend:
            if self.config.backend not in backs: raise ValueError(f'missing backend: {self.config.backend}')
            return backs[self.config.backend]
        elig=[b for b in backs.values() if b.enabled]
        if not elig: raise ValueError('missing backend')
        return sorted(elig,key=lambda b:b.capability_score,reverse=True)[0]
    def _run_candidate(self, cid, odir, backend:Backend):
        cdir=odir/'candidates'/cid; rdir=cdir/'run'; rdir.mkdir(parents=True,exist_ok=True); cr=CandidateResult(cid, cdir/'worktree', 'running', artifacts_dir=rdir, started_at=now())
        start=time.time()
        try:
            copied=create_git_baselined_copy(self.config.repo, cdir)
            cr.worktree_path=copied.worktree_path; cr.patch_path=copied.patch_path
        except Exception as e:
            cr.error=f'candidate isolation setup failed: {e}'; cr.run_status='failed'; cr.patch_status='failed'; (rdir/'stderr.txt').write_text(cr.error,encoding='utf-8'); cr.completed_at=now(); cr.duration_seconds=time.time()-start; return cr
        try:
            run=self.runner or runner_for_name(self.config.agent or 'villani-code')
            res=run.run_task(repo_path=cr.worktree_path, task=self.config.task, success_criteria=None, backend_name=backend.name, backend_config=backend, timeout_seconds=self.config.candidate_timeout_seconds or backend.timeout_seconds or 1200, context={'attempt_id':cid}, artifacts_dir=rdir)
            (rdir/'stdout.txt').write_text(res.stdout or '',encoding='utf-8'); (rdir/'stderr.txt').write_text(res.stderr or '',encoding='utf-8')
            cr.exit_code=res.exit_code; cr.stdout_path=rdir/'stdout.txt'; cr.stderr_path=rdir/'stderr.txt'
            cr.debug_root=Path(res.debug_artifact_dir) if res.debug_artifact_dir else None
            cr.resolved_trace_dir=Path(res.resolved_trace_dir) if res.resolved_trace_dir else None
            cr.debug_dir, cr.debug_resolution_status, cr.debug_resolution_reason = _debug_resolution(cr.debug_root, cr.resolved_trace_dir)
            cr.run_status='completed' if res.exit_code==0 else 'failed'
        except Exception as e:
            cr.error=f'agent runner failed: {e}'; cr.run_status='failed'; (rdir/'stderr.txt').write_text(cr.error,encoding='utf-8')
        try:
            cap=capture_candidate_patch(cr.worktree_path, cr.patch_path or (cdir/'diff.patch'))
            cr.changed_files=cap.changed_files; cr.patch_path=Path(cap.patch_path) if cap.patch_path else (cdir/'diff.patch')
            cr.patch_status='captured' if cap.patch_path else ('failed' if cap.failure_reason else 'empty')
            if cap.failure_reason: cr.error=(cr.error+'; ' if cr.error else '')+f'patch capture failed: {cap.failure_reason}'
        except Exception as e:
            cr.patch_status='failed'; cr.error=(cr.error+'; ' if cr.error else '')+f'patch capture failed: {e}'
        cr.completed_at=now(); cr.duration_seconds=time.time()-start
        return cr
    def _run_verifier(self, cr:CandidateResult, odir:Path):
        vdir=odir/'candidates'/cr.candidate_id/'verifier'; vdir.mkdir(parents=True,exist_ok=True); out=vdir/'verifier-result.json'
        if not cr.debug_dir or not _is_verifier_debug_dir(Path(cr.debug_dir)):
            cr.debug_dir, cr.debug_resolution_status, cr.debug_resolution_reason = _debug_resolution(cr.debug_root, cr.resolved_trace_dir or cr.debug_dir)
        if not cr.debug_dir or not _is_verifier_debug_dir(Path(cr.debug_dir)):
            root = cr.debug_root or cr.resolved_trace_dir or cr.debug_dir
            reason = f'No verifier-compatible Villani Code debug trace found. Expected session_meta.json under {root} or one of its child trace directories.'
            cr.debug_resolution_status='missing'; cr.debug_resolution_reason=reason
            cr.verifier_result={'result':None,'verdict':'error','confidence':0.0,'recommendedAction':'inspect_manually','reason':reason,'traceDir':None}; write_json(out, cr.verifier_result); return cr
        try:
            if self.verifier:
                res=self.verifier(debug_dir=Path(cr.debug_dir), repo_dir=cr.worktree_path, workspace=self.config.workspace, backend=self.config.verifier_backend, out=out, trace_dir=vdir/'trace')
                if isinstance(res,dict): write_json(out,res)
            else:
                cmd=[sys.executable,'-m','villani_ops.cli.main','verifier','--debug-dir',str(cr.debug_dir),'--repo-dir',str(cr.worktree_path),'--workspace',str(self.config.workspace),'--json','--out',str(out),'--verifier-timeout-seconds',str(self.config.verifier_timeout_seconds),'--max-verifier-tool-calls',str(self.config.verifier_max_tool_calls),'--trace-dir',str(vdir/'trace')]
                if self.config.verifier_backend: cmd += ['--backend', self.config.verifier_backend]
                p=subprocess.run(cmd,text=True,capture_output=True,timeout=self.config.verifier_timeout_seconds+30); (vdir/'stdout.txt').write_text(p.stdout); (vdir/'stderr.txt').write_text(p.stderr)
                try: res=json.loads(p.stdout or out.read_text())
                except Exception: res={'result':None,'verdict':'error','confidence':0.0,'recommendedAction':'inspect_manually','reason':'unparseable verifier output','traceDir':None,'stdoutPath':str(vdir/'stdout.txt'),'stderrPath':str(vdir/'stderr.txt')}
        except Exception as e:
            res={'result':None,'verdict':'error','confidence':0.0,'recommendedAction':'inspect_manually','reason':f'verifier subprocess failed: {e}','traceDir':None}
            write_json(out,res)
        cr.verifier_result=res if isinstance(res,dict) else {'result':None,'verdict':'error'}; cr.verifier_trace_dir=Path(cr.verifier_result.get('traceDir')) if cr.verifier_result.get('traceDir') else None; return cr
    def _record_candidate(self, cr, p):
        v=cr.verifier_result or {}; verifier_trace=str(cr.verifier_trace_dir) if cr.verifier_trace_dir else v.get('traceDir')
        return {'candidateId':cr.candidate_id,'worktreePath':str(cr.worktree_path),'status':'verified' if v else cr.run_status,'agent':self.config.agent,'backend':self.config.backend,'startedAt':cr.started_at,'completedAt':cr.completed_at,'debugRoot':str(cr.debug_root) if cr.debug_root else None,'debugDir':str(cr.debug_dir) if cr.debug_dir else None,'candidateDebugDir':str(cr.debug_dir) if cr.debug_dir else None,'resolvedTraceDir':str(cr.resolved_trace_dir) if cr.resolved_trace_dir else None,'debugResolutionStatus':cr.debug_resolution_status,'debugResolutionReason':cr.debug_resolution_reason,'patchPath':str(cr.patch_path) if cr.patch_path else None,'patchStatus':cr.patch_status,'verifierResultPath':str(p/'candidates'/cr.candidate_id/'verifier'/'verifier-result.json'),'verifierTraceDir':verifier_trace,'traceDir':verifier_trace,'result':v.get('result'),'verdict':v.get('verdict'),'confidence':v.get('confidence'),'recommendedAction':v.get('recommendedAction'),'error':cr.error or (v.get('reason') if v.get('verdict')=='error' else None)}
    def _integrate(self, odir, winner):
        rec={'schemaVersion':'villani-ops-verifier-parallel-integration-v1','winnerCandidateId':winner.candidate_id if winner else None,'sourceWorktree':str(winner.worktree_path) if winner else None,'targetRepo':str(self.config.repo),'patchPath':str(winner.patch_path) if winner and winner.patch_path else None,'status':'skipped','changedFiles':[],'error':None}
        if not winner: write_json(odir/'integration.json',rec); return rec
        write_json(odir/'task.json', Task(repo_path=str(self.config.repo), objective=self.config.task).model_dump(mode='json'))
        write_json(odir/'decision.json', {'accepted':True,'winning_patch_path':str(winner.patch_path),'winning_attempt_id':winner.candidate_id})
        try:
            art=self.integrator(odir,winner) if self.integrator else safe_apply(odir, artifact_name='integration-apply.json')
            rec.update({'status':'integrated','changedFiles':winner.changed_files or [],'apply':art})
        except Exception as e: rec.update({'status':'failed','error':str(e)})
        write_json(odir/'integration.json',rec); return rec
    def _materialization_record(self, odir, source, selection, integ, winner):
        v=(winner.verifier_result or {}) if winner else {}
        fallback=bool(selection.get('fallback') or selection.get('fallbackWinner'))
        status='selected' if winner and (integ.get('status') in {'integrated','skipped'} or fallback or selection.get('winnerResult')==1) else ('no_winner' if not winner else 'integration_failed')
        rec={'schemaVersion':'villani-ops-materializable-selection-v1','source':source,'orchestrationId':odir.name,'orchestrationDir':str(odir),'winnerCandidateId':winner.candidate_id if winner else None,'winnerResult':selection.get('winnerResult'),'winnerVerdict':v.get('verdict'),'winnerConfidence':v.get('confidence'),'targetRepo':str(self.config.repo),'patchPath':str(winner.patch_path) if winner and winner.patch_path else None,'selectionPath':str(odir/'selection.json'),'integrationPath':str(odir/'integration.json'),'createdAt':now(),'status':status,'fallbackWinner':fallback,'selectionPolicy':selection.get('selectionPolicy'),'materializationPath':str(odir/'materialization.json'),'candidateDebugDir':str(winner.debug_dir) if winner and winner.debug_dir else None,'verifierTraceDir':str(winner.verifier_trace_dir) if winner and winner.verifier_trace_dir else (v.get('traceDir') if v else None)}
        write_json(odir/'materialization.json', rec)
        if winner:
            sel2=dict(selection); sel2.update({'winnerPatchPath':rec['patchPath'],'materializationPath':rec['materializationPath'],'candidateDebugDir':rec['candidateDebugDir'],'verifierTraceDir':rec['verifierTraceDir'],'traceDir':rec['verifierTraceDir']})
            write_json(odir/'selection.json', sel2)
            write_json(odir/'task.json', Task(repo_path=str(self.config.repo), objective=self.config.task).model_dump(mode='json'))
            write_json(odir/'decision.json', {'accepted':True,'winning_patch_path':rec['patchPath'],'winning_attempt_id':winner.candidate_id})
        return rec
    def run(self):
        cfg=self.config; cfg.repo=Path(cfg.repo).resolve(); cfg.workspace=Path(cfg.workspace).resolve(); cfg.parallelism=cfg.parallelism or cfg.candidates; cfg.seed=cfg.seed if cfg.seed is not None else secrets.randbelow(2**31)
        if cfg.candidates<1 or cfg.parallelism<1 or cfg.parallelism>cfg.candidates: raise ValueError('invalid candidates/parallelism')
        oid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3); odir=cfg.workspace/'orchestrations'/oid; odir.mkdir(parents=True)
        backend=self._backend_obj(); cfg.backend=cfg.backend or backend.name
        candidates=[]
        with ThreadPoolExecutor(max_workers=cfg.parallelism) as ex:
            futs=[ex.submit(self._run_candidate, f'candidate-{i:03d}', odir, backend) for i in range(1,cfg.candidates+1)]
            for f in as_completed(futs): candidates.append(self._run_verifier(f.result(), odir))
        candidates=sorted(candidates,key=lambda c:c.candidate_id)
        for cr in candidates:
            append_jsonl(odir/'candidate-runs.jsonl', {'candidateId':cr.candidate_id,'status':cr.run_status,'exitCode':cr.exit_code,'durationSeconds':cr.duration_seconds,'stdoutPath':str(cr.stdout_path) if cr.stdout_path else None,'stderrPath':str(cr.stderr_path) if cr.stderr_path else None,'debugRoot':str(cr.debug_root) if cr.debug_root else None,'debugDir':str(cr.debug_dir) if cr.debug_dir else None,'resolvedTraceDir':str(cr.resolved_trace_dir) if cr.resolved_trace_dir else None})
            v=cr.verifier_result or {}; append_jsonl(odir/'verifier-results.jsonl', {'candidateId':cr.candidate_id,'result':v.get('result'),'verdict':v.get('verdict'),'confidence':v.get('confidence'),'recommendedAction':v.get('recommendedAction'),'debugDir':str(cr.debug_dir) if cr.debug_dir else None,'candidateDebugDir':str(cr.debug_dir) if cr.debug_dir else None,'verifierTraceDir':v.get('traceDir'),'traceDir':v.get('traceDir'),'verifierResultPath':str(odir/'candidates'/cr.candidate_id/'verifier'/'verifier-result.json')})
            append_jsonl(odir/'candidates.jsonl', self._record_candidate(cr,odir))
        sel=select_winner(candidates,cfg.seed,cfg.on_all_fail); write_json(odir/'selection.json',sel.to_dict())
        winner=next((c for c in candidates if c.candidate_id==sel.winnerCandidateId),None); integ=self._integrate(odir,winner)
        status='completed' if integ['status'] in {'integrated','skipped'} and (winner or cfg.on_all_fail=='fail') else 'failed'
        if sel.winnerCandidateId is None and cfg.on_all_fail=='fail': status='failed'
        mat=self._materialization_record(odir, 'verifier-parallel', sel.to_dict(), integ, winner)
        orch={'schemaVersion':'villani-ops-verifier-parallel-orchestration-v1','orchestrationId':oid,'mode':'verifier-parallel','createdAt':oid[:16],'completedAt':now(),'status':status,'repo':str(cfg.repo),'workspace':str(cfg.workspace),'taskPreview':cfg.task[:120],'candidates':cfg.candidates,'parallelism':cfg.parallelism,'seed':cfg.seed,'agent':cfg.agent,'backend':cfg.backend,'verifierBackend':cfg.verifier_backend,'onAllFail':cfg.on_all_fail,'winnerCandidateId':sel.winnerCandidateId,'selectionPolicy':POLICY,'materializationPath':str(odir/'materialization.json'),'winnerPatchPath':mat.get('patchPath')}
        write_json(odir/'orchestration.json',orch); self._transcript(odir,candidates,sel,integ)
        if not cfg.keep_worktrees and integ['status']=='integrated':
            for cr in candidates:
                if cr.worktree_path and cr.worktree_path.exists(): shutil.rmtree(cr.worktree_path, ignore_errors=True)
        out={'schemaVersion':'villani-ops-verifier-parallel-output-v1','orchestrationId':oid,'status':status,'winnerCandidateId':sel.winnerCandidateId,'winnerResult':sel.winnerResult,'selectionPath':str(odir/'selection.json'),'integrationPath':str(odir/'integration.json'),'materializationPath':str(odir/'materialization.json'),'winnerPatchPath':mat.get('patchPath'),'candidateDebugDir':mat.get('candidateDebugDir'),'verifierTraceDir':mat.get('verifierTraceDir'),'orchestrationDir':str(odir),'candidates':sel.allCandidates}
        if cfg.out: write_json(cfg.out,out)
        return out
    def _transcript(self, odir,cands,sel,integ):
        rows='\n'.join(f"| {c.candidate_id} | {c.run_status} | {c.debug_root or ''} | {c.debug_dir or ''} | {(c.verifier_result or {}).get('result')} | {(c.verifier_result or {}).get('confidence')} | {(c.verifier_result or {}).get('traceDir')} |" for c in cands)
        (odir/'transcript.md').write_text(f"# Verifier Parallel Orchestration\n\n## Summary\n- Repo: {self.config.repo}\n- Candidates: {len(cands)}\n- Winner: {sel.winnerCandidateId}\n- Selection policy: {POLICY}\n- Seed: {self.config.seed}\n\n## Task\n\n{self.config.task}\n\n## Candidate Results\n\n| Candidate | Run Status | Debug Root | Debug Dir | Verifier Result | Confidence | Trace |\n|---|---|---|---|---:|---:|---|\n{rows}\n\n## Selection\n\n{sel.reason}\n\n## Integration\n\n{integ.get('status')} {integ.get('error') or ''}\n",encoding='utf-8')
