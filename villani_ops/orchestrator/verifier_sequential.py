from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import secrets, shutil
from datetime import datetime, timezone
from typing import Any

from .verifier_parallel import (
    VerifierParallelConfig,
    VerifierParallelOrchestrator,
    CandidateResult,
    now,
    write_json,
    append_jsonl,
)
from .selection import select_winner

POLICY = 'binary_verifier_first_success'

@dataclass
class VerifierSequentialConfig(VerifierParallelConfig):
    parallelism: int | None = 1

class VerifierSequentialOrchestrator(VerifierParallelOrchestrator):
    """Sequential verifier-selected orchestrator.

    Reuses verifier-parallel candidate isolation, runner invocation, debug trace
    resolution, verifier invocation, patch capture, recording, and integration
    helpers; only the run loop and mode-specific artifacts differ.
    """

    def __init__(self, config: VerifierSequentialConfig, runner=None, verifier=None, integrator=None):
        super().__init__(config, runner=runner, verifier=verifier, integrator=integrator)

    def _candidate_run_row(self, cr: CandidateResult) -> dict[str, Any]:
        return {'candidateId':cr.candidate_id,'status':cr.run_status,'exitCode':cr.exit_code,'durationSeconds':cr.duration_seconds,'stdoutPath':str(cr.stdout_path) if cr.stdout_path else None,'stderrPath':str(cr.stderr_path) if cr.stderr_path else None,'debugRoot':str(cr.debug_root) if cr.debug_root else None,'debugDir':str(cr.debug_dir) if cr.debug_dir else None,'resolvedTraceDir':str(cr.resolved_trace_dir) if cr.resolved_trace_dir else None}

    def _verifier_row(self, cr: CandidateResult, odir: Path) -> dict[str, Any]:
        v=cr.verifier_result or {}
        return {'candidateId':cr.candidate_id,'result':v.get('result'),'verdict':v.get('verdict'),'confidence':v.get('confidence'),'recommendedAction':v.get('recommendedAction'),'debugDir':str(cr.debug_dir) if cr.debug_dir else None,'traceDir':v.get('traceDir'),'verifierResultPath':str(odir/'candidates'/cr.candidate_id/'verifier'/'verifier-result.json')}

    def _skipped_record(self, cid: str, winner_id: str | None) -> dict[str, Any]:
        return {'candidateId':cid,'status':'skipped','skipReason':f'Earlier candidate {winner_id} verified as correct.' if winner_id else 'Not attempted.','result':None,'verdict':None}

    def _summary(self, cr: CandidateResult, status: str | None = None) -> dict[str, Any]:
        v=cr.verifier_result or {}
        return {'candidateId':cr.candidate_id,'status':status or ('selected' if v.get('result') == 1 else 'verified'),'result':v.get('result'),'verdict':v.get('verdict'),'confidence':v.get('confidence'),'traceDir':str(cr.debug_dir) if cr.debug_dir else v.get('traceDir')}

    def _first_success_selection(self, candidates: list[CandidateResult], skipped: list[str], seed: int, on_all_fail: str, winner: CandidateResult | None) -> dict[str, Any]:
        attempted=[c.candidate_id for c in candidates]
        if winner:
            allc=[]
            for c in candidates:
                allc.append(self._summary(c, 'selected' if c.candidate_id == winner.candidate_id else 'verified'))
            allc.extend({'candidateId':cid,'status':'skipped'} for cid in skipped)
            return {'schemaVersion':'villani-ops-verifier-sequential-selection-v1','selectionPolicy':POLICY,'seed':seed,'onAllFail':on_all_fail,'winnerCandidateId':winner.candidate_id,'winnerResult':1,'stoppedEarly':bool(skipped),'stopReason':f'{winner.candidate_id} verifier result = 1','attemptedCandidates':attempted,'skippedCandidates':skipped,'allCandidates':allc,'reason':'Selected first candidate with verifier result = 1.'}
        sel=select_winner(candidates, seed, on_all_fail).to_dict()
        sel.update({'schemaVersion':'villani-ops-verifier-sequential-selection-v1','selectionPolicy':POLICY,'stoppedEarly':False,'stopReason':None,'attemptedCandidates':attempted,'skippedCandidates':skipped})
        if sel.get('fallback'):
            sel['reason']='Sequential all-fail fallback: '+sel.get('reason','')
        return sel

    def run(self):
        cfg=self.config; cfg.repo=Path(cfg.repo).resolve(); cfg.workspace=Path(cfg.workspace).resolve(); cfg.parallelism=1; cfg.seed=cfg.seed if cfg.seed is not None else secrets.randbelow(2**31)
        if cfg.candidates < 1: raise ValueError('invalid candidates')
        oid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3); odir=cfg.workspace/'orchestrations'/oid; odir.mkdir(parents=True)
        backend=self._backend_obj(); cfg.backend=cfg.backend or backend.name
        candidates: list[CandidateResult]=[]; skipped: list[str]=[]; winner: CandidateResult | None=None
        for i in range(1, cfg.candidates+1):
            cid=f'candidate-{i:03d}'
            cr=self._run_candidate(cid, odir, backend)
            cr=self._run_verifier(cr, odir)
            candidates.append(cr)
            append_jsonl(odir/'candidate-runs.jsonl', self._candidate_run_row(cr))
            append_jsonl(odir/'verifier-results.jsonl', self._verifier_row(cr, odir))
            rec=self._record_candidate(cr, odir)
            if (cr.verifier_result or {}).get('result') == 1:
                rec['status']='selected'; winner=cr
            append_jsonl(odir/'candidates.jsonl', rec)
            if winner:
                skipped=[f'candidate-{j:03d}' for j in range(i+1, cfg.candidates+1)]
                for sid in skipped:
                    append_jsonl(odir/'candidates.jsonl', self._skipped_record(sid, winner.candidate_id))
                break
        selection=self._first_success_selection(candidates, skipped, cfg.seed, cfg.on_all_fail, winner)
        if not winner and selection.get('winnerCandidateId'):
            winner=next((c for c in candidates if c.candidate_id==selection.get('winnerCandidateId')), None)
        write_json(odir/'selection.json', selection)
        integ=self._integrate(odir, winner)
        if isinstance(integ, dict):
            integ['schemaVersion']='villani-ops-verifier-sequential-integration-v1'; write_json(odir/'integration.json', integ)
        status='completed' if integ.get('status') in {'integrated','skipped'} and (winner or cfg.on_all_fail=='fail') else 'failed'
        if selection.get('winnerCandidateId') is None and cfg.on_all_fail=='fail': status='failed'
        orch={'schemaVersion':'villani-ops-verifier-sequential-orchestration-v1','orchestrationId':oid,'mode':'verifier-sequential','createdAt':oid[:16],'completedAt':now(),'status':status,'repo':str(cfg.repo),'workspace':str(cfg.workspace),'taskPreview':cfg.task[:120],'candidates':cfg.candidates,'attemptedCandidates':len(candidates),'skippedCandidates':len(skipped),'parallelism':1,'seed':cfg.seed,'agent':cfg.agent,'backend':cfg.backend,'verifierBackend':cfg.verifier_backend,'onAllFail':cfg.on_all_fail,'winnerCandidateId':selection.get('winnerCandidateId'),'selectionPolicy':POLICY}
        write_json(odir/'orchestration.json', orch); self._transcript(odir, candidates, skipped, selection, integ)
        if not cfg.keep_worktrees and integ.get('status')=='integrated':
            for cr in candidates:
                if cr.worktree_path and cr.worktree_path.exists(): shutil.rmtree(cr.worktree_path, ignore_errors=True)
        out={'schemaVersion':'villani-ops-verifier-sequential-output-v1','orchestrationId':oid,'status':status,'winnerCandidateId':selection.get('winnerCandidateId'),'winnerResult':selection.get('winnerResult'),'stoppedEarly':selection.get('stoppedEarly',False),'attemptedCandidates':len(candidates),'skippedCandidates':len(skipped),'selectionPath':str(odir/'selection.json'),'integrationPath':str(odir/'integration.json'),'orchestrationDir':str(odir),'candidates':selection.get('allCandidates',[])}
        if cfg.out: write_json(cfg.out, out)
        return out

    def _transcript(self, odir: Path, cands: list[CandidateResult], skipped: list[str], sel: dict[str, Any], integ: dict[str, Any]):
        rows='\n'.join(f"| {c.candidate_id} | {c.run_status} | {(c.verifier_result or {}).get('result')} | {(c.verifier_result or {}).get('confidence')} | {(c.verifier_result or {}).get('traceDir')} |" for c in cands)
        skipped_lines='\n'.join(f"- {cid}: skipped" for cid in skipped) or 'None'
        (odir/'transcript.md').write_text(f"# Verifier Sequential Orchestration\n\n## Summary\n- Repo: {self.config.repo}\n- Candidates allowed: {self.config.candidates}\n- Candidates attempted: {len(cands)}\n- Candidates skipped: {len(skipped)}\n- Winner: {sel.get('winnerCandidateId')}\n- Stop reason: {sel.get('stopReason')}\n\n## Task\n\n{self.config.task}\n\n## Attempted Candidates\n\n| Candidate | Run Status | Verifier Result | Confidence | Trace |\n|---|---|---:|---:|---|\n{rows}\n\n## Skipped Candidates\n\n{skipped_lines}\n\n## Selection\n\n{sel.get('reason')}\n\n## Integration\n\n{integ.get('status')} {integ.get('error') or ''}\n", encoding='utf-8')
