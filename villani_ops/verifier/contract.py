from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
import json, re
from typing import Any, Optional

class RequirementKind(str, Enum):
    FINAL_DELIVERABLE_EXECUTES='final_deliverable_executes'
    PERFORMANCE_REQUIREMENT='performance_requirement'
    CLEAN_ENVIRONMENT_AVAILABILITY='clean_environment_availability'
    EXTERNAL_RUNTIME_BEHAVIOR='external_runtime_behavior'
    CONSTRAINED_EDIT='constrained_edit'
    TEST_SUITE_PASSES='test_suite_passes'
    OUTPUT_FILE_CREATED='output_file_created'
    API_OR_SERVICE_BEHAVIOR='api_or_service_behavior'
    DEPENDENCY_OR_INSTALLATION='dependency_or_installation'
    DATA_CORRECTNESS='data_correctness'
    UNKNOWN='unknown'

class EvidenceStrength(str, Enum):
    OFFICIAL_ORACLE='official_oracle'  # defined for schema compatibility; not used at runtime
    CONTRACT_EQUIVALENT='contract_equivalent'
    STRONG='strong'
    WEAK='weak'
    SOURCE_INFERENCE='source_inference'
    AGENT_CLAIM='agent_claim'
    CONTRADICTORY='contradictory'
    MISSING='missing'

@dataclass
class ContractRequirement:
    id: str
    kind: str
    description: str
    required_evidence: str
    material: bool = True
    weak_evidence_not_allowed: list[str] = field(default_factory=list)
    candidate_files: list[str] = field(default_factory=list)
    candidate_commands: list[str] = field(default_factory=list)
    source: str = 'heuristic'

@dataclass
class TaskContract:
    task_prompt: str
    task_type: str
    requirements: list[ContractRequirement]
    global_forbidden_evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

@dataclass
class EvidenceItem:
    requirement_id: str
    strength: str
    source: str
    description: str
    command: str | None = None
    path: str | None = None
    exit_code: int | None = None
    reason: str = ''
    verifier_generated: bool = False

@dataclass
class VerificationDecision:
    result: int
    verdict: str
    confidence: float
    recommendedAction: str
    reason: str
    contract: dict[str, Any]
    evidence: list[dict[str, Any]]
    blockedRequirements: list[dict[str, Any]]
    downgradedFromLlmSuccess: bool = False
    riskFlags: list[str] = field(default_factory=list)
    llmResult: dict[str, Any] | None = None


def to_jsonable(x: Any) -> Any:
    if hasattr(x, '__dataclass_fields__'):
        return {k: to_jsonable(v) for k, v in asdict(x).items()}
    if isinstance(x, Enum): return x.value
    if isinstance(x, list): return [to_jsonable(v) for v in x]
    if isinstance(x, dict): return {k: to_jsonable(v) for k, v in x.items()}
    return x

FILE_RE=re.compile(r'(?<![\w/.-])([A-Za-z0-9_.-]+\.(?:py|R|r|sql|sh|js|ts|mjs|cjs|rb|pl|php|tex|txt|csv|json|html|ics|xml|stan|ipynb))(?![\w/.-])')

def _repo_files(root: Path) -> set[str]:
    if not root or not root.exists(): return set()
    ignore={'.git','node_modules','venv','.venv','__pycache__','dist','build'}
    out=set()
    for p in root.rglob('*'):
        if not (set(p.relative_to(root).parts) & ignore): out.add(str(p.relative_to(root)))
    return out

def _tests_exist(files:set[str]) -> bool:
    return any(re.search(r'(^|/)(tests?|test_.*|.*_test)\b', f) or f.endswith(('_test.py','.spec.js','.test.js')) for f in files)

def _add(reqs, kind, desc, evidence, weak, files=None, cmds=None, source='prompt'):
    reqs.append(ContractRequirement(f'R{len(reqs)+1}', kind.value if isinstance(kind,RequirementKind) else str(kind), desc, evidence, True, weak, files or [], cmds or [], source))

def build_task_contract(task_prompt: str, original_repo_root: Path, final_repo_root: Path, debug_run) -> TaskContract:
    text=task_prompt or getattr(debug_run,'objective',None) or ''
    low=text.lower(); reqs=[]; notes=[]
    files=sorted(set(FILE_RE.findall(text)))
    orig_files=_repo_files(Path(original_repo_root)) if original_repo_root else set()
    final_files=_repo_files(Path(final_repo_root)) if final_repo_root else set()
    all_files=orig_files|final_files
    if re.search(r'\b(performance|optimi[sz]e|faster|runtime|benchmark|median|latency|throughput|speed|slow|timing)\b', low):
        _add(reqs, RequirementKind.PERFORMANCE_REQUIREMENT, 'Task contains a material performance/timing requirement.', 'contract_equivalent benchmark or threshold comparison from the intended repo harness', ['EXPLAIN/query shape/correct rows only','agent self-benchmark without equivalent baseline','single wall-clock run without threshold'], files, ['benchmark','make bench','pytest'])
    exec_files=[f for f in files if Path(f).suffix.lower() in {'.py','.r','.sh','.js','.ts','.rb','.pl','.php','.sql'} or f.lower().endswith('.r')]
    if files and re.search(r'\b(run|execute|works?|script|deliverable)\b', low) and exec_files:
        _add(reqs, RequirementKind.FINAL_DELIVERABLE_EXECUTES, 'Named final deliverable(s) must execute: '+', '.join(exec_files), 'direct execution/import of the named final deliverable with exit 0 and task-relevant output', ['side-channel inline commands','commands that do not invoke the final deliverable'], exec_files, exec_files)
    if re.search(r'\b(binary|executable|command|installed?|install|path|which|available|package)\b', low):
        cmds=[]
        for m in re.findall(r'`([^`\n]+)`', text): cmds.append(m.strip())
        _add(reqs, RequirementKind.CLEAN_ENVIRONMENT_AVAILABILITY, 'Required command/binary/package availability must hold in a clean environment.', 'clean subprocess/environment availability or durable repo installation proof', ['export PATH=... && which','source/.bashrc/profile-local mutation','version checks only after shell mutation'], files, cmds)
    if re.search(r'\b(cancel|sigint|signal|async|server|http|https|ssh|deploy|serve|service|daemon|concurrent|subprocess|cli interaction|endpoint|api|request|response)\b', low):
        kind=RequirementKind.API_OR_SERVICE_BEHAVIOR if re.search(r'\b(http|https|endpoint|api|server|service|serve|deploy|request|response)\b', low) else RequirementKind.EXTERNAL_RUNTIME_BEHAVIOR
        _add(reqs, kind, 'Required external runtime/API behavior must be exercised outside source inference.', 'actual subprocess/signal/server/API probe exercising the final entrypoint', ['source-code inspection','internal-only unit tests','agent summary'], files, [])
    if re.search(r'\b(only|must not|do not|forbidden|allowlist|allowed|synonym|preserve|without changing|do not change|only edit|only replace)\b', low):
        _add(reqs, RequirementKind.CONSTRAINED_EDIT, 'Prompt imposes edit/output constraints that must be checked against the final diff.', 'deterministic final diff check against allowlist/forbidden constraints', ['output success without diff check','LaTeX/build success without constraint check'], files, [])
    if _tests_exist(orig_files) and re.search(r'\b(test|fix|pass|suite|bug|failing)\b', low):
        _add(reqs, RequirementKind.TEST_SUITE_PASSES, 'Original repository appears test-driven and task mentions tests/bug fixing.', 'relevant project test suite passes after final changes', ['generic command success','partial ad hoc tests'], [], ['pytest','npm test','go test','cargo test'])
    if re.search(r'\b(create|write|generate|output|report|artifact|model|data file|save)\b', low) and files:
        _add(reqs, RequirementKind.OUTPUT_FILE_CREATED, 'Task requires generated output/artifact file(s): '+', '.join(files), 'final repo contains required output file with task-relevant content', ['file existence without content when content matters','agent claim'], files, [])
    if re.search(r'\b(correct|rows?|data|dataset|answer|expected)\b', low):
        _add(reqs, RequirementKind.DATA_CORRECTNESS, 'Task includes data/correctness expectations.', 'task-equivalent correctness check against expected data/behavior', ['row count only when performance is material','agent claim'], files, [])
    if not reqs:
        _add(reqs, RequirementKind.UNKNOWN, 'Could not confidently classify task contract; require strong or equivalent evidence before success.', 'contract_equivalent_or_strong', ['agent claims','generic command success','source plausibility'], files, [])
        notes.append('Unknown task type: conservative adjudication rejects weak evidence.')
    kinds=[r.kind for r in reqs]
    task_type='+'.join(dict.fromkeys(kinds))
    forbidden=['official_oracle/runtime hidden evaluator outputs/result.json','agent summary as proof','generic exit 0 as proof','source plausibility as runtime proof']
    return TaskContract(text, task_type, reqs, forbidden, notes)


def adjudicate_contract(contract: TaskContract, evidence: list[EvidenceItem], llm_result: Optional[dict[str,Any]]) -> VerificationDecision:
    good={EvidenceStrength.CONTRACT_EQUIVALENT.value, EvidenceStrength.STRONG.value}
    bad={EvidenceStrength.MISSING.value, EvidenceStrength.AGENT_CLAIM.value, EvidenceStrength.SOURCE_INFERENCE.value, EvidenceStrength.WEAK.value}
    blocked=[]; risk=[]
    for req in contract.requirements:
        if not req.material: continue
        ev=[e for e in evidence if e.requirement_id==req.id]
        strengths=[e.strength for e in ev] or [EvidenceStrength.MISSING.value]
        if EvidenceStrength.CONTRADICTORY.value in strengths:
            blocked.append({'id':req.id,'description':req.description,'requiredEvidence':req.required_evidence,'bestAvailableEvidence':'contradictory','reason':'Contradictory evidence blocks success.'}); continue
        if not any(s in good for s in strengths):
            best=next((s for s in strengths if s not in [EvidenceStrength.MISSING.value]), EvidenceStrength.MISSING.value)
            blocked.append({'id':req.id,'description':req.description,'requiredEvidence':req.required_evidence,'bestAvailableEvidence':best,'reason':'Material requirement lacks contract-equivalent or strong evidence.'})
    llm_success=bool(llm_result and llm_result.get('result')==1)
    if blocked:
        result=0; verdict='failure'; action='reject' if any(b['bestAvailableEvidence']=='contradictory' for b in blocked) else 'inspect_manually'
        reason='Material contract requirements were not satisfied: '+ '; '.join(f"{b['id']} {b['bestAvailableEvidence']}" for b in blocked[:5])
        conf=.87
    else:
        if llm_result and llm_result.get('result')==0:
            result=0; verdict='failure'; action=llm_result.get('recommendedAction') or 'inspect_manually'; reason='LLM rejected despite contract-satisfied evidence; conservative policy keeps failure.'; conf=float(llm_result.get('confidence') or .65)
        else:
            result=1; verdict='success'; action='accept'; reason='All material contract requirements have strong or contract-equivalent evidence.'; conf=.82
    if action=='inspect_manually' and result==1: action='accept'
    return VerificationDecision(result, verdict, conf, action, reason, to_jsonable(contract), [to_jsonable(e) for e in evidence], blocked, llm_success and result==0, risk, {'result': llm_result.get('result'), 'reason': llm_result.get('reason')} if isinstance(llm_result,dict) else None)
