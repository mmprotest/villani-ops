from __future__ import annotations
from pathlib import Path
import re, shlex
from .contract import TaskContract, ContractRequirement, EvidenceItem, EvidenceStrength, RequirementKind
from .tools import compare_file, list_original_repo, list_final_repo

BENCH_RE=re.compile(r'\b(benchmark|bench|hyperfine|pytest-benchmark|timeit|median|throughput|latency|ops/sec|speedup|baseline|threshold)\b', re.I)
BAD_ENV_RE=re.compile(r'export\s+PATH=|\bsource\b|\.\s+~/.bashrc|/etc/profile(?:\.d)?', re.I)
EXTERNAL_RE=re.compile(r'\b(SIGINT|kill\s+-INT|subprocess|Popen|curl\s|wget\s|requests\.|http|server|daemon|concurrent|asyncio|cancel)\b', re.I)

def _cmds(debug_run): return [c for c in getattr(debug_run,'commands',[]) if getattr(c,'command',None)]
def _txt(c): return ((c.command or '')+'\n'+(c.stdout or '')+'\n'+(c.stderr or ''))
def _ok(c): return getattr(c,'exitCode',None)==0

def _mentions_file(cmd, path):
    b=Path(path).name.lower(); s=(cmd or '').lower(); return b in s or str(path).lower() in s

def _command_evidence(req, debug_run):
    out=[]
    for c in _cmds(debug_run):
        cmd=c.command or ''; blob=_txt(c)
        strength=None; reason=''
        low=cmd.lower()
        if req.kind==RequirementKind.PERFORMANCE_REQUIREMENT.value:
            if re.search(r'\bexplain\b|query plan|row count|correct rows|looks optimized', blob, re.I):
                strength=EvidenceStrength.WEAK.value; reason='EXPLAIN/correctness/query-shape evidence is insufficient for performance.'
            elif _ok(c) and BENCH_RE.search(blob) and re.search(r'baseline|threshold|faster|speedup|median|latency|throughput|ops/sec', blob, re.I):
                strength=EvidenceStrength.CONTRACT_EQUIVALENT.value; reason='Benchmark-like output includes comparison/threshold metric.'
            elif _ok(c) and BENCH_RE.search(cmd):
                strength=EvidenceStrength.STRONG.value; reason='Repo benchmark harness appears to have run.'
        elif req.kind==RequirementKind.FINAL_DELIVERABLE_EXECUTES.value:
            if req.candidate_files and any(_mentions_file(cmd, f) for f in req.candidate_files):
                if re.search(r'\b(Rscript|python\d*|node|bash|sh|sqlite3)\b', cmd, re.I) and _ok(c) and not re.search(r'\b(Rscript|python\d*|node)\s+-e\b|<<', cmd, re.I):
                    strength=EvidenceStrength.CONTRACT_EQUIVALENT.value; reason='Command directly invoked named final deliverable.'
                elif _ok(c):
                    strength=EvidenceStrength.WEAK.value; reason='Named file mentioned but command appears inline/side-channel.'
            elif re.search(r'\b(Rscript|python\d*|node)\s+-e\b|<<', cmd, re.I) and _ok(c):
                strength=EvidenceStrength.WEAK.value; reason='Inline script did not directly invoke required final deliverable.'
        elif req.kind==RequirementKind.CLEAN_ENVIRONMENT_AVAILABILITY.value:
            if BAD_ENV_RE.search(cmd):
                strength=EvidenceStrength.WEAK.value; reason='Shell-local environment mutation cannot prove clean availability.'
            elif _ok(c) and re.search(r'\benv\s+-i\b|clean env|clean subprocess', cmd+' '+blob, re.I):
                strength=EvidenceStrength.CONTRACT_EQUIVALENT.value; reason='Clean environment/subprocess availability was exercised.'
            elif _ok(c) and re.search(r'\b(which|command -v|--version)\b', cmd) and not BAD_ENV_RE.search(cmd):
                strength=EvidenceStrength.STRONG.value; reason='Availability command ran without detected shell-local mutation.'
        elif req.kind in {RequirementKind.EXTERNAL_RUNTIME_BEHAVIOR.value, RequirementKind.API_OR_SERVICE_BEHAVIOR.value}:
            if _ok(c) and re.search(r'\b(grep|cat|sed|awk)\b', cmd, re.I):
                strength=EvidenceStrength.WEAK.value; reason='Source/text inspection is insufficient for external behavior.'
            elif _ok(c) and (EXTERNAL_RE.search(cmd+' '+blob) or re.search(r'git clone|git push|serves correct content|clone exit: 0|push exit: 0|PASS:', cmd+' '+blob, re.I)) and not re.search(r'\bpytest\b.*(helper|unit|internal)|source inspection', cmd+' '+blob, re.I):
                strength=EvidenceStrength.CONTRACT_EQUIVALENT.value; reason='External runtime/API behavior appears exercised.'
            elif _ok(c) and re.search(r'pytest|unit|internal|grep|cat ', cmd, re.I):
                strength=EvidenceStrength.WEAK.value; reason='Internal/source-only evidence is insufficient for external behavior.'
        elif req.kind==RequirementKind.TEST_SUITE_PASSES.value:
            if _ok(c) and re.search(r'\b(pytest|npm test|go test|cargo test|mvn test|gradle test)\b', cmd, re.I):
                strength=EvidenceStrength.STRONG.value; reason='Project test suite command passed.'
        elif req.kind==RequirementKind.DATA_CORRECTNESS.value:
            if _ok(c) and re.search(r'expected|correct|all tests passed|PASS', blob, re.I):
                strength=EvidenceStrength.STRONG.value; reason='Correctness-oriented validation passed.'
        if strength:
            out.append(EvidenceItem(req.id,strength,'agent_command',reason,cmd,None,getattr(c,'exitCode',None),reason,False))
    return out

def _allowlist_words(root:Path, files):
    words=set()
    for f in files:
        p=root/f
        if p.exists() and p.is_file():
            words.update(w.lower() for w in re.findall(r'[A-Za-z][A-Za-z-]*', p.read_text(errors='replace')))
    return words

def _static_evidence(req, original_repo_root, final_repo_root):
    out=[]; orig=Path(original_repo_root); final=Path(final_repo_root)
    if req.kind==RequirementKind.OUTPUT_FILE_CREATED.value:
        for f in req.candidate_files:
            if (final/f).exists(): out.append(EvidenceItem(req.id,EvidenceStrength.STRONG.value,'final_repo',f'Required output file exists in final repo: {f}',path=f))
    if req.kind==RequirementKind.CONSTRAINED_EDIT.value:
        all_files=set(list_original_repo(orig))|set(list_final_repo(final)); changed=[]
        for f in sorted(all_files):
            try:
                cmp=compare_file(orig, final, f)
                if cmp.unified_diff: changed.append((f,cmp))
            except Exception: pass
        prompt_files=[Path(f).name for f in req.candidate_files]
        allow_candidates=[f for f,_ in changed if 'synonym' in Path(f).name.lower()] + [f for f in list_final_repo(final) if 'synonym' in Path(f).name.lower()]
        if re.search(r'synonym|allowlist|allowed', req.description, re.I) or allow_candidates:
            words=_allowlist_words(final, allow_candidates)
            if not words:
                out.append(EvidenceItem(req.id,EvidenceStrength.WEAK.value,'diff','Constraint mentioned allowlist/synonyms but no readable allowlist was found.',reason='missing allowlist'))
            else:
                additions=' '.join(' '.join(c.added_lines) for _,c in changed if Path(_).name not in [Path(a).name for a in allow_candidates])
                changed_words=[w.lower() for w in re.findall(r'[A-Za-z][A-Za-z-]*', additions)]
                suspect=[w for w in changed_words if w not in words and len(w)>3]
                if suspect:
                    out.append(EvidenceItem(req.id,EvidenceStrength.CONTRADICTORY.value,'diff',f'Changed words not present in allowlist: {suspect[:8]}',reason='unchecked/invalid constrained edit'))
                else:
                    out.append(EvidenceItem(req.id,EvidenceStrength.CONTRACT_EQUIVALENT.value,'diff','Final diff additions are covered by simple allowlist words.',reason='allowlist diff check passed'))
        elif changed:
            out.append(EvidenceItem(req.id,EvidenceStrength.WEAK.value,'diff','Changed files exist, but generic constrained edit could not be deterministically verified.',reason='unchecked constrained edit'))
    return out

def _tool_evidence(req, debug_run):
    out=[]
    for t in getattr(debug_run,'toolCalls',[]) or []:
        name=(getattr(t,'toolName',None) or '').lower(); args=getattr(t,'args',None)
        if isinstance(args,str):
            import json
            try: args=json.loads(args)
            except Exception: args={}
        args=args if isinstance(args,dict) else {}
        path=args.get('path') or args.get('file_path') or args.get('filename')
        status=(getattr(t,'status',None) or '').lower(); ok=status not in {'failed','error','failure'} and not getattr(t,'error',None)
        if req.kind=='output_file_created' and ok and path and any(Path(path).name==Path(f).name for f in req.candidate_files):
            content=str(args.get('content') or args.get('text') or '')
            strength=EvidenceStrength.STRONG.value if content.strip() else EvidenceStrength.WEAK.value
            out.append(EvidenceItem(req.id,strength,'tool_call',f'Write-like tool created required output {path}.',path=path,reason='debug write artifact'))
    return out

def build_contract_evidence(contract: TaskContract, original_repo_root: Path, final_repo_root: Path, debug_run) -> list[EvidenceItem]:
    evidence=[]
    for req in contract.requirements:
        evidence.extend(_command_evidence(req, debug_run))
        evidence.extend(_static_evidence(req, original_repo_root, final_repo_root))
        evidence.extend(_tool_evidence(req, debug_run))
        if not any(e.requirement_id==req.id for e in evidence):
            evidence.append(EvidenceItem(req.id,EvidenceStrength.MISSING.value,'derived','No contract-equivalent evidence found.',reason='missing'))
    if getattr(debug_run,'modelResponses',None):
        for req in contract.requirements:
            evidence.append(EvidenceItem(req.id,EvidenceStrength.AGENT_CLAIM.value,'model_response','Agent/model summary exists but is not proof.',reason='agent claim'))
    return evidence
