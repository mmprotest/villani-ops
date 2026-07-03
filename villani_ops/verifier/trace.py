from __future__ import annotations
import hashlib, json, os, re, sys, traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEVELS={"minimal","standard","full"}
MINIMAL={"manifest.json","verification_result.json","errors.jsonl"}
STANDARD=MINIMAL|{"input.json","source_artifacts.json","verifier_packet.json","requirements.json","evidence_by_category.json","tool_calls.jsonl","tool_observations.jsonl","llm_messages.jsonl","llm_raw_responses.jsonl","llm_final_verdict_raw.json","llm_final_verdict_parsed.json","calibration.json","verifier_transcript.md"}
FULL=STANDARD|{"timeline.jsonl","validation_windows.json","failure_classification.json"}
SECRET_PATTERNS=[
    (re.compile(r'(?i)(authorization\s*[:=]\s*bearer\s+)[^\s"\']+'), r'\1<redacted>'),
    (re.compile(r'(?i)(api[_-]?key\s*[:=]\s*)[^\s"\']+'), r'\1<redacted>'),
    (re.compile(r'(?i)(OPENAI_API_KEY|VILLANI_OPS_VERIFIER_API_KEY)(\s*=\s*)[^\s]+'), r'\1\2<redacted>'),
    (re.compile(r'(?i)(--api-key(?:=|\s+))[^\s]+'), r'\1<redacted>'),
    (re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._\-]+'), r'\1<redacted>'),
]

def utc_now(): return datetime.now(timezone.utc).isoformat()
def compact_ts(): return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
def redact(obj: Any) -> Any:
    if isinstance(obj,str):
        s=obj
        for pat,repl in SECRET_PATTERNS: s=pat.sub(repl,s)
        return s
    if isinstance(obj,list): return [redact(x) for x in obj]
    if isinstance(obj,dict):
        out={}
        for k,v in obj.items():
            if str(k).lower() in {'apikey','api_key','authorization','headers'}: out[k]='<redacted>'
            else: out[k]=redact(v)
        return out
    return obj

def _safe_name(s: str) -> str:
    s=re.sub(r'[^A-Za-z0-9_.-]+','_',s or '').strip('._')
    return s[:80] or 'debug-run'

class VerifierTraceWriter:
    def __init__(self, workspace: Path, debug_dir: Path, trace_dir: Path|None, trace_enabled: bool, trace_level: str):
        self.workspace=Path(workspace); self.debug_dir=Path(debug_dir); self._explicit=trace_dir is not None
        self._trace_dir=Path(trace_dir) if trace_dir is not None else None; self.trace_enabled=trace_enabled
        self.trace_level=trace_level if trace_level in LEVELS else 'full'; self.trace_id=None; self.created_at=utc_now(); self.manifest={}; self._started=False; self._failed=False
    @property
    def trace_dir(self): return self._trace_dir if self.trace_enabled and not self._failed else None
    def _allowed(self,name):
        if self.trace_level=='minimal': return name in MINIMAL
        if self.trace_level=='standard': return name in STANDARD
        return name in FULL
    def _resolve_dir(self, manifest):
        if self._trace_dir is not None: return self._trace_dir
        run_id=None
        try:
            meta=self.debug_dir/'session_meta.json'
            if meta.exists(): run_id=(json.loads(meta.read_text(encoding='utf-8')).get('run_id'))
        except Exception: pass
        base=_safe_name(run_id or self.debug_dir.name or hashlib.sha256(str(self.debug_dir.resolve()).encode()).hexdigest()[:8])
        root=self.workspace/'verifier-runs'; root.mkdir(parents=True,exist_ok=True)
        p=root/(compact_ts()+'_'+base); i=2
        while p.exists(): p=root/(compact_ts()+'_'+base+f'_{i}'); i+=1
        return p
    def start(self, manifest: dict) -> None:
        if not self.trace_enabled: return
        try:
            self._trace_dir=self._resolve_dir(manifest)
            if self._explicit and self._trace_dir.exists(): raise FileExistsError(f'trace directory already exists: {self._trace_dir}')
            self._trace_dir.mkdir(parents=True,exist_ok=False)
            self.trace_id=self._trace_dir.name; self.manifest=redact({**manifest,'traceId':self.trace_id,'createdAt':self.created_at,'completedAt':None,'traceDir':str(self._trace_dir),'status':'running'})
            self._started=True; self.write_json('manifest.json',self.manifest,force=True)
            (self._trace_dir/'errors.jsonl').touch()
        except Exception:
            self._failed=True
            if self._explicit: raise
    def write_json(self,name:str,payload:Any,force=False)->None:
        if not self.trace_dir or (not force and not self._allowed(name)): return
        try: (self.trace_dir/name).write_text(json.dumps(redact(payload),indent=2,ensure_ascii=False,default=str),encoding='utf-8')
        except Exception as e: self.record_error('trace_write',e,{'artifact':name})
    def append_jsonl(self,name:str,payload:Any)->None:
        if not self.trace_dir or not self._allowed(name): return
        try:
            with (self.trace_dir/name).open('a',encoding='utf-8') as f: f.write(json.dumps(redact(payload),ensure_ascii=False,default=str)+'\n'); f.flush()
        except Exception as e:
            if name!='errors.jsonl': self.record_error('trace_write',e,{'artifact':name})
    def write_text(self,name:str,text:str)->None:
        if not self.trace_dir or not self._allowed(name): return
        try: (self.trace_dir/name).write_text(redact(text),encoding='utf-8')
        except Exception as e: self.record_error('trace_write',e,{'artifact':name})
    def record_error(self,stage:str,error:BaseException|str,extra:dict|None=None)->None:
        if not self.trace_dir: return
        tb=traceback.format_exc() if isinstance(error,BaseException) else None
        self.append_jsonl('errors.jsonl',{'createdAt':utc_now(),'stage':stage,'errorType':type(error).__name__ if isinstance(error,BaseException) else 'Error','message':str(error),'traceback':tb,'extra':extra or {}})
    def finish(self, final_result: dict)->None:
        if not self.trace_dir: return
        completed=utc_now(); self.write_json('verification_result.json',final_result,force=True)
        self.manifest.update({'completedAt':completed,'status':'error' if final_result.get('verdict')=='error' else 'completed'})
        self.write_json('manifest.json',self.manifest,force=True)
        idx={'createdAt':self.created_at,'completedAt':completed,'traceId':self.trace_id,'traceDir':str(self.trace_dir),'debugDir':str(self.debug_dir),'repoDir':final_result.get('repoDir'),'result':final_result.get('result'),'verdict':final_result.get('verdict'),'confidence':final_result.get('confidence'),'recommendedAction':final_result.get('recommendedAction'),'backend':(final_result.get('verifier') or {}).get('backend'),'model':(final_result.get('verifier') or {}).get('model'),'mode':(final_result.get('verifier') or {}).get('mode'),'toolCallCount':final_result.get('toolCallCount',len(final_result.get('toolsUsed') or [])),'error':final_result.get('reason') if final_result.get('verdict')=='error' else None}
        try:
            root=self.trace_dir.parent; root.mkdir(parents=True,exist_ok=True)
            with (root/'index.jsonl').open('a',encoding='utf-8') as f: f.write(json.dumps(redact(idx),ensure_ascii=False,default=str)+'\n')
        except Exception as e: self.record_error('trace_write',e,{'artifact':'index.jsonl'})

def source_artifacts(debug_dir: Path, run=None) -> dict:
    core=['session_meta.json','commands.jsonl','tool_calls.jsonl','patches.jsonl','model_responses.jsonl','summary.json','final_summary.json','validations.jsonl']
    files=[]
    for p in sorted(Path(debug_dir).glob('*')):
        if not p.is_file(): continue
        try:
            data=p.read_bytes(); txt=data.decode('utf-8','ignore')
            files.append({'relativePath':p.name,'exists':True,'sizeBytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'lineCount':txt.count('\n')+(1 if txt else 0)})
        except Exception as e: files.append({'relativePath':p.name,'exists':True,'sizeBytes':None,'sha256':None,'lineCount':None,'error':str(e)})
    return {'schemaVersion':'villani-ops-verifier-source-artifacts-v1','debugDir':str(debug_dir),'files':files,'coreArtifacts':{n:(Path(debug_dir)/n).exists() for n in core},'missingArtifacts':[n for n in core if not (Path(debug_dir)/n).exists()],'parseWarnings':getattr(run,'parseWarnings',[]) if run else []}

def timeline_rows(run, packet):
    from .timeline import build_timeline
    cats=packet.get('evidence',{}) if packet else {}
    cat_by_order={}
    for cat,items in cats.items():
        if isinstance(items,list):
            for it in items:
                if isinstance(it,dict) and it.get('order') is not None: cat_by_order.setdefault(it.get('order'),cat)
    rows=[]
    for ev in build_timeline(run):
        cat=cat_by_order.get(ev.order)
        rows.append({'order':ev.order,'kind':ev.kind,'source':ev.source+'.jsonl' if not ev.source.endswith('.jsonl') else ev.source,'commandIndex':ev.command_index,'toolCallIndex':ev.tool_call_index,'toolCallId':ev.tool_call_id,'turnIndex':ev.turn_index,'timestamp':ev.timestamp,'status':ev.status,'summary':ev.text[:500],'isValidationCandidate':cat in {'finalEndToEndValidation','testValidation','serviceValidation'},'isFailure':cat=='activeFailures','isRecovered':cat=='recoveredFailures','category':cat})
    return rows

def failure_classification(packet):
    cats=packet.get('evidence',{}) if packet else {}; out=[]
    for key,cls in [('activeFailures','active'),('recoveredFailures','recovered')]:
        for i,x in enumerate(cats.get(key,[]) or [],1): out.append({'id':f'fail-{len(out)+1:04d}','source':x.get('source'),'timelineOrder':x.get('order'),'text':x.get('text'),'classification':cls,'reason':'Classified by deterministic validation-window recovery rules.'})
    post=[x for x in cats.get('recoveredFailures',[]) or [] if x.get('kind')=='post_validation_risk']
    return {'schemaVersion':'villani-ops-verifier-failure-classification-v1','activeFailures':[x for x in out if x['classification']=='active'],'recoveredFailures':[x for x in out if x['classification']=='recovered'],'postValidationRisks':post,'classificationRules':['Failures before selected strong validation window are recovered; failures after validation may be post-validation risks when cleanup/inspection related.'],'counts':{'activeFailureCount':len(cats.get('activeFailures',[]) or []),'recoveredFailureCount':len(cats.get('recoveredFailures',[]) or []),'postValidationRiskCount':len(post)}}

def validation_windows(run, selected):
    from .deterministic import _signal_score, is_service_validation_command, is_test_validation_command, _strong_final_signal
    candidates=[]; current=[]
    for c in run.commands:
        sc,sigs=_signal_score(c)
        if sc>0 or (current and (is_service_validation_command(c) or is_test_validation_command(c) or _strong_final_signal(c))): current.append(c)
        else:
            if current: candidates.append(current); current=[]
    if current: candidates.append(current)
    rows=[]
    for cluster in candidates:
        score=0; sig=[]
        for c in cluster:
            sc,s=_signal_score(c); score+=sc; sig+=s
        if score>0: rows.append({'startOrder':min(getattr(c,'_timeline_order',c.index) for c in cluster),'endOrder':max(getattr(c,'_timeline_order',c.index) for c in cluster),'score':score,'reason':'validation signal cluster','signals':sig[:20]})
    return {'schemaVersion':'villani-ops-verifier-validation-windows-v1','selected':selected,'candidates':rows}

def transcript(result, packet=None, calibration=None, trace_dir=None):
    v=result.get('verifier') or {}; lines=['# Villani Ops Verifier Trace','','## Summary',f"- Result: {result.get('result')}",f"- Verdict: {result.get('verdict')}",f"- Confidence: {result.get('confidence')}",f"- Recommended action: {result.get('recommendedAction')}",f"- Debug dir: {result.get('debugDir')}",f"- Backend: {v.get('backend')}",f"- Model: {v.get('model')}",f"- Trace dir: {trace_dir or result.get('traceDir')}",'','## Objective','',str((packet or {}).get('objective') or ''),'','## Extracted Requirements']
    for r in result.get('requirementResults') or []: lines.append(f"- {r.get('id')}: {r.get('status')} — {r.get('requirement')}")
    lines += ['','## Selected Validation Window','',json.dumps((result.get('deterministicChecks') or {}).get('finalValidationWindow'),indent=2,default=str),'','## Top Success Evidence']
    for e in (result.get('successEvidence') or [])[:10]: lines.append(f"- {e.get('text') if isinstance(e,dict) else e}")
    lines += ['','## Failure Classification','','### Active Failures']
    for e in result.get('failureEvidence') or []: lines.append(f"- {e.get('text') if isinstance(e,dict) else e}")
    lines += ['','### Recovered Failures']
    for e in result.get('recoveredFailures') or []: lines.append(f"- {e.get('text') if isinstance(e,dict) else e}")
    lines += ['','### Post-Validation Risks','','See failure_classification.json.','','## LLM Tool Loop','','See llm_messages.jsonl, tool_calls.jsonl, and tool_observations.jsonl.','','### Final LLM Verdict','',json.dumps(result.get('llmRawVerdict'),indent=2,default=str),'','## Calibration','',json.dumps(calibration or {'changes':[],'rulesApplied':[]},indent=2,default=str),'','## Final Result','',json.dumps(result,indent=2,default=str)]
    return '\n'.join(lines)+'\n'
