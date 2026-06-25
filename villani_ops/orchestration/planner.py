from __future__ import annotations
import json
from pathlib import Path
from typing import Literal, Any
from pydantic import BaseModel, Field
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMClient, LLMCallResult
from .graph import OrchestrationGraph
from .nodes import OrchestrationNode
from .artifacts import write_text_utf8, write_json_utf8

class PlanResult(BaseModel):
    summary: str
    strategy: Literal['single_task','parallel_candidates','decompose_then_execute'] = 'parallel_candidates'
    should_decompose: bool = False
    decomposition_reason: str | None = None
    candidate_attempts: int
    risks: list[str] = Field(default_factory=list)
    expected_difficulty: Literal['easy','medium','hard','unknown'] = 'unknown'
    confidence: float = 0.0
    planner_normalized: bool = False
    planner_normalization_notes: list[str] = Field(default_factory=list)
    planner_fallback_used: bool = False
    planner_fallback_reason: str | None = None
    decomposition_normalized: bool = False
    decomposition_normalization_notes: list[str] = Field(default_factory=list)
    decomposition_fallback_used: bool = False
    decomposition_fallback_reason: str | None = None
    fallback_used: bool = False
    planner_repaired: bool = False
    planner_repair_notes: list[str] = Field(default_factory=list)

class Subtask(BaseModel):
    id: str
    title: str
    objective: str
    success_criteria: str | None = None
    relevant_files: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    expected_difficulty: Literal['easy','medium','hard','unknown'] = 'unknown'
    risk: Literal['low','medium','high','unknown'] = 'unknown'
    confidence: float = 0.0
    required_role: str = 'coding'
    assigned_backend: str | None = None
    can_run_parallel: bool | None = None
    parallel_group: str | None = None
    parallel_safety_reason: str | None = None
    max_attempts: int | None = None
    timeout_seconds: int | None = None
    validation_commands: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    merge_contract: str | None = None
    depends_on_acceptance: bool = True

class DecompositionResult(BaseModel):
    should_use_decomposition: bool
    reason: str
    subtasks: list[Subtask] = Field(default_factory=list)
    merge_strategy: str | None = None
    confidence: float = 0.0
    advisory_only: bool = True
    planner_normalized: bool = False
    planner_normalization_notes: list[str] = Field(default_factory=list)
    planner_fallback_used: bool = False
    planner_fallback_reason: str | None = None
    decomposition_normalized: bool = False
    decomposition_normalization_notes: list[str] = Field(default_factory=list)
    decomposition_fallback_used: bool = False
    decomposition_fallback_reason: str | None = None
    fallback_used: bool = False


def _as_list(value: Any) -> list[str]:
    if value is None: return []
    if isinstance(value, list): return [str(x) for x in value if str(x).strip()]
    if isinstance(value, str): return [value.strip()] if value.strip() else []
    return [str(value)]

def normalize_plan_payload(payload: dict[str, Any], *, requested_candidate_attempts: int, task: str | None = None, success_criteria: str | None = None, classification: dict[str, Any] | None = None, investigation: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    data=dict(payload or {}); notes=[]; out: dict[str, Any]={}
    def nonempty(v: Any) -> bool: return isinstance(v, str) and bool(v.strip())
    def join_items(items: Any) -> str:
        vals=[]
        for x in items if isinstance(items, list) else []:
            if isinstance(x, dict):
                s=x.get('title') or x.get('objective') or x.get('summary') or x.get('name')
                if s and x.get('objective') and s != x.get('objective'): s=f"{s}: {x.get('objective')}"
            else: s=x
            if str(s or '').strip(): vals.append(str(s).strip())
        return '; '.join(vals)
    def nested_files(src: dict[str, Any] | None, key: str) -> list[str]:
        v=(src or {}).get(key)
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []
    files=[]
    rs=data.get('resulting_state')
    if isinstance(rs, dict): files=nested_files(rs, 'files')
    files = files or nested_files(classification, 'likely_files') or nested_files(investigation, 'relevant_files')
    subtasks=data.get('subtasks') if isinstance(data.get('subtasks'), list) and data.get('subtasks') else []

    if nonempty(data.get('summary')): out['summary']=data['summary'].strip()
    else:
        for key in ('plan','steps','implementation_plan','execution_plan','task_plan'):
            if isinstance(data.get(key), list):
                s=join_items(data[key])
                if s: out['summary']=s; notes.append(f"Mapped {key} list to summary"); break
            elif nonempty(data.get(key)):
                out['summary']=data[key].strip(); notes.append(f"Mapped {key} to summary"); break
        if 'summary' not in out:
            for key in ('analysis','thought','reasoning','approach','strategy_summary'):
                if nonempty(data.get(key)): out['summary']=data[key].strip(); notes.append(f"Mapped {key} to summary"); break
        if 'summary' not in out and files:
            out['summary']='Planner identified relevant files: ' + ', '.join(files[:8]) + '.'
            notes.append('Synthesized summary from relevant files')
        if 'summary' not in out and subtasks:
            s=join_items(subtasks)
            if s: out['summary']=s; notes.append('Synthesized summary from subtasks')
        if 'summary' not in out: return data, notes

    strategy_aliases={'parallel':'parallel_candidates','parallel_candidates':'parallel_candidates','multi_candidate':'parallel_candidates','multiple_candidates':'parallel_candidates','independent_candidates':'parallel_candidates','single':'single_task','single_task':'single_task','one_shot':'single_task','direct':'single_task','decompose':'decompose_then_execute','decomposition':'decompose_then_execute','decompose_then_execute':'decompose_then_execute','subtasks':'decompose_then_execute'}
    raw_strategy=next((data[k] for k in ('strategy','execution_strategy','strategy_name','mode','approach_type') if data.get(k) is not None), None)
    explicit_valid=False
    if raw_strategy is not None:
        mapped=strategy_aliases.get(str(raw_strategy).strip().lower())
        if mapped: out['strategy']=mapped; explicit_valid=True; notes.append('Mapped strategy alias')
    if 'strategy' not in out:
        out['strategy']='decompose_then_execute' if subtasks else 'parallel_candidates'; notes.append(f"Defaulted strategy to {out['strategy']}")

    def parse_bool(v: Any) -> bool | None:
        if isinstance(v, bool): return v
        if isinstance(v, (int,float)): return bool(v)
        if isinstance(v, str):
            s=v.strip().lower()
            if s in {'true','yes','needed','required','need','requires','use'}: return True
            if s in {'false','no','not needed','none','unneeded'}: return False
        return None
    raw_dec=next((data[k] for k in ('should_decompose','decompose','requires_decomposition','needs_decomposition','use_decomposition','decomposition') if k in data), None)
    dec=parse_bool(raw_dec)
    out['should_decompose']=bool(dec) if dec is not None else False
    if dec is not None: notes.append('Mapped decomposition flag to should_decompose')
    if out['strategy']=='decompose_then_execute' or subtasks or len(files) >= 4:
        out['should_decompose']=True
        if len(files) >= 4: out['strategy']='decompose_then_execute'; notes.append('Mapped resulting_state.files to decomposition signal')
    if subtasks and not explicit_valid: out['strategy']='decompose_then_execute'

    reason=next((data[k] for k in ('decomposition_reason','decomposition','reason','rationale','why_decompose') if nonempty(data.get(k))), None)
    out['decomposition_reason']=str(reason).strip() if reason else None
    if reason: notes.append('Mapped decomposition reason')
    if not out['decomposition_reason'] and subtasks: out['decomposition_reason']='Task contains multiple separable subtasks.'; notes.append('Synthesized decomposition reason from subtasks')
    if not out['decomposition_reason'] and len(files) >= 4: out['decomposition_reason']='Planner identified multiple relevant files across the task.'; notes.append('Synthesized decomposition reason from relevant files')

    raw_attempts=next((data[k] for k in ('candidate_attempts','num_candidates','candidate_count','candidates','attempts','num_attempts','parallel_attempts') if k in data), None)
    try: attempts=int(raw_attempts) if raw_attempts is not None and not isinstance(raw_attempts, list) else int(requested_candidate_attempts)
    except Exception: attempts=int(requested_candidate_attempts); notes.append('Defaulted invalid candidate_attempts to requested value')
    if raw_attempts is None: notes.append('Defaulted candidate_attempts to requested value')
    out['candidate_attempts']=max(1,min(8,attempts))
    if out['candidate_attempts'] != attempts: notes.append('Clamped candidate_attempts to 1..8')

    diff_alias={'simple':'easy','low':'easy','moderate':'medium','normal':'medium','complex':'hard','high':'hard','difficult':'hard','easy':'easy','medium':'medium','hard':'hard','unknown':'unknown'}
    raw_diff=next((data[k] for k in ('expected_difficulty','difficulty','complexity','task_difficulty') if data.get(k) is not None), None)
    out['expected_difficulty']=diff_alias.get(str(raw_diff).strip().lower(), 'unknown')
    if raw_diff is not None and out['expected_difficulty']=='unknown' and str(raw_diff).strip().lower()!='unknown': notes.append('Defaulted invalid expected_difficulty to unknown')

    raw_risks=next((data[k] for k in ('risks','risk_factors','warnings','concerns','caveats','failure_modes') if k in data), [])
    risks=_as_list(raw_risks)
    coord='Multi-part task may require coordinated changes across subsystems.'
    if out['should_decompose'] and not risks: risks.append(coord); notes.append('Added decomposition coordination risk')
    out['risks']=risks

    raw_conf=next((data[k] for k in ('confidence','confidence_score','certainty') if data.get(k) is not None), 0.0)
    try:
        s=str(raw_conf).strip(); conf=float(s[:-1])/100 if s.endswith('%') else float(raw_conf)
        if conf > 1: conf=conf/100
        out['confidence']=max(0.0,min(1.0,conf))
    except Exception: out['confidence']=0.0
    return out, notes


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, Any]:
    for k in keys:
        if k in data and data[k] is not None:
            return k, data[k]
    return None, None

def _parse_boolish(value: Any) -> bool | None:
    if isinstance(value, bool): return value
    if isinstance(value, (int, float)): return bool(value)
    if isinstance(value, str):
        s=value.strip().lower()
        if s in {'true','yes','needed','required','need','requires','use','needed/required'}: return True
        if s in {'false','no','not needed','not required','none','unneeded','unnecessary'}: return False
    return None

def _parse_confidence(value: Any) -> float:
    try:
        if value is None: return 0.0
        s=str(value).strip()
        n=float(s[:-1])/100 if s.endswith('%') else float(s)
        if n > 1 and n <= 100: n=n/100
        return max(0.0, min(1.0, n))
    except Exception:
        return 0.0

def _slug(value: Any) -> str:
    import re
    s=str(value or '').strip().lower()
    s=re.sub(r'[^a-z0-9]+', '_', s)
    s=re.sub(r'_+', '_', s).strip('_')
    return s

def _short_text(value: Any) -> str:
    s=str(value or '').strip()
    if not s: return ''
    first=s.split('.', 1)[0].strip()
    if first and len(first) <= 80: return first + ('.' if s.startswith(first + '.') else '')
    return s[:80].strip()

def _string_or_none(value: Any) -> str | None:
    if isinstance(value, list):
        vals=[str(x).strip() for x in value if str(x).strip()]
        return '; '.join(vals) if vals else None
    if value is None: return None
    s=str(value).strip()
    return s or None

def normalize_decomposition_payload(payload: dict[str, Any], *, task: str | None = None, success_criteria: str | None = None, plan: dict[str, Any] | None = None, classification: dict[str, Any] | None = None, investigation: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    data=dict(payload or {}); notes: list[str]=[]
    raw_items=None
    for key in ('subtasks','tasks','work_items','steps','components','modules','decomposition'):
        if key not in data or data[key] is None: continue
        value=data[key]
        if key == 'decomposition':
            if isinstance(value, list):
                raw_items=value; notes.append('Mapped top-level decomposition list to subtasks'); break
            if isinstance(value, dict):
                nested_key, nested=_first_present(value, ('subtasks','tasks','steps','work_items','components','modules'))
                if isinstance(nested, list):
                    raw_items=nested; notes.append(f'Mapped decomposition.{nested_key} to subtasks'); break
                notes.append('Preserved decomposition object as metadata')
                continue
            continue
        if isinstance(value, list):
            raw_items=value; break
    if raw_items is None or not isinstance(raw_items, list): raw_items=[]
    subtasks=[]; used_ids=set()
    diff_map={'easy':'easy','medium':'medium','hard':'hard','unknown':'unknown','simple':'easy','low':'easy','moderate':'medium','normal':'medium','complex':'hard','high':'hard','difficult':'hard'}
    risk_map={'low':'low','medium':'medium','high':'high','unknown':'unknown','simple':'low','safe':'low','moderate':'medium','normal':'medium','complex':'high','dangerous':'high'}
    def unique_id(base: str, idx: int) -> str:
        base=_slug(base) or f'subtask_{idx:03d}'
        sid=base; n=2
        while sid in used_ids:
            sid=f'{base}_{n}'; n+=1
        used_ids.add(sid); return sid
    for idx,item in enumerate(raw_items, 1):
        if item is None or item == '': continue
        if isinstance(item, str):
            title=_short_text(item) or f'subtask {idx:03d}'; objective=str(item).strip() or title
            subtasks.append({'id':unique_id(f'subtask_{idx:03d}', idx),'title':title,'objective':objective,'success_criteria':None,'relevant_files':[],'dependencies':[],'expected_difficulty':'unknown','risk':'unknown','confidence':0.0})
            notes.append('Converted string subtask to title/objective')
            continue
        if not isinstance(item, dict): continue
        src=dict(item)
        id_key, raw_id=_first_present(src, ('id','name','key','slug','task_id'))
        title_key, title=_first_present(src, ('title','name','summary','label'))
        obj_key, objective=_first_present(src, ('objective','description','details','task','instruction','goal','fix','action','work'))
        if not title:
            title=_short_text(objective) or f'subtask {idx:03d}'
            if obj_key == 'description': notes.append('Mapped subtask description to title/objective')
        if not objective: objective=title
        if isinstance(raw_id, (int, float)) and not isinstance(raw_id, bool):
            sid=unique_id(title if title_key else f'subtask_{idx:03d}', idx); notes.append(f'Normalized numeric subtask id to {sid}')
        elif raw_id is not None:
            sid=unique_id(str(raw_id), idx)
        else:
            sid=unique_id(f'subtask_{idx:03d}', idx); notes.append(f'Generated deterministic subtask id {sid}')
        sc_key, sc=_first_present(src, ('success_criteria','acceptance_criteria','validation','done_when'))
        files_key, files=_first_present(src, ('relevant_files','files','file_paths','paths','modules','affected_files','relevant_file_paths'))
        deps_key, deps=_first_present(src, ('dependencies','depends_on','prerequisites','blocked_by'))
        if files_key and files_key != 'relevant_files': notes.append(f'Mapped subtask {files_key} to relevant_files')
        _, diff=_first_present(src, ('expected_difficulty','difficulty','complexity'))
        _, risk=_first_present(src, ('risk','risk_level','impact'))
        _, conf=_first_present(src, ('confidence','confidence_score','certainty'))
        subtasks.append({'id':sid,'title':str(title).strip(),'objective':str(objective).strip(),'success_criteria':_string_or_none(sc),'relevant_files':_as_list(files),'dependencies':_as_list(deps),'expected_difficulty':diff_map.get(str(diff).strip().lower(), 'unknown'),'risk':risk_map.get(str(risk).strip().lower(), 'unknown'),'confidence':_parse_confidence(conf)})
    flag_key, flag=_first_present(data, ('should_use_decomposition','should_decompose','use_decomposition','decompose','requires_decomposition','needs_decomposition'))
    parsed=_parse_boolish(flag)
    if parsed is None:
        if subtasks: parsed=True; notes.append('Defaulted should_use_decomposition to true because subtasks were present')
        elif isinstance(plan, dict) and plan.get('should_decompose'): parsed=True; notes.append('Defaulted should_use_decomposition to true because plan requested decomposition')
        else: parsed=False
    reason_key, reason=_first_present(data, ('reason','rationale','decomposition_reason','why_decompose','summary','analysis'))
    if reason is None and isinstance(data.get('decomposition'), str):
        reason=data.get('decomposition'); notes.append('Mapped string decomposition to decomposition reason')
    reason=str(reason).strip() if reason is not None and str(reason).strip() else ''
    if not reason and subtasks:
        reason='Task was decomposed into separable subtasks.'; notes.append('Synthesized decomposition reason from subtasks')
    if not reason and isinstance(plan, dict) and plan.get('decomposition_reason'):
        reason=str(plan.get('decomposition_reason'))
    merge_key, merge=_first_present(data, ('merge_strategy','integration_strategy','combine_strategy','validation_strategy'))
    merge=str(merge).strip() if merge is not None and str(merge).strip() else None
    if not merge and subtasks:
        merge='Apply coordinated changes in one candidate patch and validate full test suite.'; notes.append('Defaulted merge_strategy because subtasks were present')
    _, conf=_first_present(data, ('confidence','confidence_score','certainty'))
    return {'should_use_decomposition': bool(parsed), 'reason': reason if (reason or not parsed) else 'Task was decomposed into separable subtasks.', 'subtasks': subtasks, 'merge_strategy': merge, 'confidence': _parse_confidence(conf), 'advisory_only': True}, notes


def repair_plan_against_context(plan: PlanResult, *, requested_candidate_attempts: int, task: str | None, success_criteria: str | None, classification: dict[str, Any] | None, investigation: dict[str, Any] | None) -> tuple[PlanResult, list[str]]:
    """Schema-only repair: clamp unsafe scalar values without injecting domain-specific decomposition."""
    fixed=plan.model_copy(deep=True)
    fixed.candidate_attempts=max(1, min(8, int(fixed.candidate_attempts or requested_candidate_attempts)))
    return fixed, []

class ValidationSection(BaseModel):
    passed: bool = True
    issues: list[str] = Field(default_factory=list)

class CompletenessSection(ValidationSection):
    missing_success_criteria: list[str] = Field(default_factory=list)

class NonRedundancySection(ValidationSection):
    overlapping_subtasks: list[dict[str, Any]] = Field(default_factory=list)

class ParallelSafetySection(ValidationSection):
    unsafe_parallel_groups: list[dict[str, Any]] = Field(default_factory=list)

class DecompositionPlanValidationResult(BaseModel):
    accepted: bool = False
    solvability: ValidationSection = Field(default_factory=ValidationSection)
    completeness: CompletenessSection = Field(default_factory=CompletenessSection)
    non_redundancy: NonRedundancySection = Field(default_factory=NonRedundancySection)
    dependency_validity: ValidationSection = Field(default_factory=ValidationSection)
    parallel_safety: ParallelSafetySection = Field(default_factory=ParallelSafetySection)
    backend_fit: ValidationSection = Field(default_factory=ValidationSection)
    required_revisions: list[str] = Field(default_factory=list)
    revised_plan: dict[str, Any] | None = None

def validate_decomposition_plan(dec: DecompositionResult, *, task: str | None, success_criteria: str | None, backends: dict[str, Backend] | None = None) -> DecompositionPlanValidationResult:
    subtasks=[s.model_dump(mode='json') if hasattr(s,'model_dump') else dict(s) for s in (dec.subtasks or [])]
    res=DecompositionPlanValidationResult()
    ids=[s.get('id') for s in subtasks if s.get('id')]
    idset=set(ids)
    def issue(section, msg):
        sec=getattr(res, section); sec.passed=False; sec.issues.append(msg); res.required_revisions.append(msg)
    if not dec.should_use_decomposition or len(subtasks) < 2:
        issue('completeness','Decomposition must contain at least two executable subtasks when requested.')
    for s in subtasks:
        if not s.get('objective') or not (s.get('success_criteria') or success_criteria): issue('solvability', f"Subtask {s.get('id')} lacks objective or success criteria.")
        if not s.get('title'): issue('solvability', f"Subtask {s.get('id')} lacks title/context.")
    text=' '.join((task or '', success_criteria or '')).lower()
    covered=' '.join((s.get('title','')+' '+s.get('objective','')+' '+(s.get('success_criteria') or '')) for s in subtasks).lower()
    for token in {w.strip('.,;:()[]') for w in text.split() if len(w.strip('.,;:()[]')) >= 5}:
        if token not in covered:
            res.completeness.missing_success_criteria.append(token)
            if len(res.completeness.missing_success_criteria) >= 3: break
    if (success_criteria or task) and res.completeness.missing_success_criteria:
        issue('completeness','Plan may not cover important terms from task/success criteria.')
    seen={}
    for s in subtasks:
        key=' '.join(str(s.get('objective') or s.get('title') or '').lower().split())
        if key in seen:
            res.non_redundancy.overlapping_subtasks.append({'subtasks':[seen[key], s.get('id')], 'reason':'same objective text'})
        else: seen[key]=s.get('id')
    if res.non_redundancy.overlapping_subtasks: issue('non_redundancy','Duplicate or overlapping subtask objectives are not justified.')
    deps={s.get('id'): list(s.get('dependencies') or []) for s in subtasks}
    for sid, ds in deps.items():
        for d in ds:
            if d not in idset: issue('dependency_validity', f'Subtask {sid} depends on unknown subtask {d}.')
    visiting=set(); visited=set()
    def dfs(x):
        if x in visiting: return True
        if x in visited: return False
        visiting.add(x)
        cyc=any(dfs(d) for d in deps.get(x,[]) if d in idset)
        visiting.remove(x); visited.add(x); return cyc
    if any(dfs(i) for i in ids): issue('dependency_validity','Subtask dependency graph contains a cycle.')
    for a in subtasks:
        for b in subtasks:
            if a is b: continue
            if a.get('can_run_parallel') and b.get('can_run_parallel') and (a.get('id') in (b.get('dependencies') or []) or b.get('id') in (a.get('dependencies') or [])):
                res.parallel_safety.unsafe_parallel_groups.append({'subtasks':[a.get('id'),b.get('id')], 'reason':'parallel-safe subtasks depend on each other'})
    file_groups={}
    for s in subtasks:
        if s.get('can_run_parallel'):
            for f in s.get('relevant_files') or []: file_groups.setdefault(f, []).append(s.get('id'))
    for f, group in file_groups.items():
        if len(set(group)) > 1: res.parallel_safety.unsafe_parallel_groups.append({'subtasks':sorted(set(group)), 'reason':f'overlapping file {f}'})
    if res.parallel_safety.unsafe_parallel_groups: issue('parallel_safety','Unsafe parallel subtask layout.')
    if backends:
        enabled=[(n,b) for n,b in backends.items() if getattr(b,'enabled',True)]
        for s in subtasks:
            role=s.get('required_role') or 'coding'; assigned=s.get('assigned_backend')
            candidates=[(n,b) for n,b in enabled if role in (getattr(b,'roles',[]) or [])]
            if assigned and (assigned not in backends or role not in (getattr(backends[assigned],'roles',[]) or [])): issue('backend_fit', f'Subtask {s.get("id")} assigned backend does not support role {role}.')
            if not assigned and not candidates: issue('backend_fit', f'No enabled backend supports required role {role} for subtask {s.get("id")}.')
    res.accepted=all(getattr(res,k).passed for k in ['solvability','completeness','non_redundancy','dependency_validity','parallel_safety','backend_fit'])
    return res

def revise_decomposition_plan(dec: DecompositionResult, validation: DecompositionPlanValidationResult) -> DecompositionResult:
    fixed=dec.model_copy(deep=True)
    ids=set(); unique=[]
    for st in fixed.subtasks:
        if st.id in ids: continue
        ids.add(st.id); st.dependencies=[d for d in st.dependencies if d in ids or any(x.id==d for x in fixed.subtasks)]
        if not st.success_criteria: st.success_criteria=st.objective
        if st.can_run_parallel is None: st.can_run_parallel=not bool(st.dependencies)
        unique.append(st)
    fixed.subtasks=unique
    return fixed

def _parse_plan_payload_from_call(call: LLMCallResult) -> dict[str, Any]:
    if isinstance(call.parsed_json, dict) and call.parsed_json:
        return call.parsed_json
    raw=(call.raw_text or '').strip()
    if raw:
        return json.loads(raw)
    return {}

def build_fixed_graph(candidate_attempts: int, runner: str = 'villani-code', *, run_id: str='', mode: str='performance', classify: bool=True, include_decompose: bool=True) -> OrchestrationGraph:
    nodes=[]
    deps=[]
    if classify:
        nodes.append(OrchestrationNode(id='classify', kind='classify', objective='Classify task difficulty, risk, and category.')); deps=['classify']
    nodes.append(OrchestrationNode(id='investigate', kind='investigate', objective='Understand task, repo context, risks, likely files, and validation plan.', dependencies=deps))
    nodes.append(OrchestrationNode(id='plan', kind='plan', objective='Plan strategy, candidate count, risks, and decomposition choice.', dependencies=['investigate']))
    nodes.append(OrchestrationNode(id='decompose', kind='decompose', objective='Break the task into advisory subtasks if useful.', dependencies=['plan']))
    code_dep='decompose'
    for i in range(1, candidate_attempts+1):
        aid=f'attempt_{i:03d}'
        nodes.append(OrchestrationNode(id=f'code_{aid}', kind='code', objective=f'Generate independent candidate patch {i}.', dependencies=[code_dep], parallel_group='candidate_code', runner=runner))
        nodes.append(OrchestrationNode(id=f'review_{aid}', kind='review', objective=f'Review candidate patch {i}.', dependencies=[f'code_{aid}'], parallel_group='candidate_review'))
    nodes.append(OrchestrationNode(id='select', kind='select', objective='Select the best eligible candidate.', dependencies=[f'review_attempt_{i:03d}' for i in range(1,candidate_attempts+1)]))
    nodes.append(OrchestrationNode(id='verify', kind='verify', objective='Make final acceptance decision and write artifacts.', dependencies=['select']))
    edges=[(d,n.id) for n in nodes for d in n.dependencies]
    return OrchestrationGraph(run_id=run_id, mode=mode, runner=runner, nodes=nodes, edges=edges)

class Planner:
    def __init__(self, client: LLMClient|None=None): self.client=client or LLMClient()
    def plan(self, *, task, classification, investigation, repo_summary: str|None, candidate_attempts: int, mode: str, backend_name: str, backend: Backend, run_dir: Path) -> tuple[PlanResult, LLMCallResult|None]:
        ctx={'task':task.model_dump(mode='json'),'classification':classification,'investigation':investigation,'repo_summary':repo_summary,'candidate_attempts':candidate_attempts,'mode':mode}
        normalized_payload=None; notes=[]
        system_prompt = """You are a planning component. You are not executing shell commands.
Do not return command, thought, action, observation, or tool-call JSON.
Return only the required planning JSON object.

Required schema:
{
  "summary": "One or two sentence plan summary",
  "strategy": "parallel_candidates",
  "should_decompose": false,
  "decomposition_reason": null,
  "candidate_attempts": 3,
  "risks": [],
  "expected_difficulty": "medium",
  "confidence": 0.75
}

Valid strategy values are: single_task, parallel_candidates, decompose_then_execute.
Valid expected_difficulty values are: easy, medium, hard, unknown.
If the task spans multiple separable subsystems or files, set strategy to decompose_then_execute and should_decompose to true.
If likely_files contains 4 or more distinct source files, do not choose single_task unless you explicitly explain why the changes are tightly coupled and not decomposable.
If success criteria lists multiple independent behaviours, prefer decompose_then_execute.
You must return planning JSON only. Do not return command/action/tool-call JSON.
"""
        try:
            call=self.client.complete_json(backend, system_prompt, json.dumps(ctx, indent=2)[:80000], 'PlanResult', estimate_cost=(mode != 'performance'))
            try:
                plan=PlanResult.model_validate(call.parsed_json)
                plan.candidate_attempts=max(1, min(8, int(plan.candidate_attempts or candidate_attempts)))
                write_json_utf8(run_dir/'plan_normalized.json', {'planner_normalized': False, 'planner_normalization_notes': [], 'normalized_payload': plan.model_dump(mode='json'), 'planner_fallback_used': False, 'planner_fallback_reason': None})
            except Exception as original_error:
                try:
                    raw_payload = _parse_plan_payload_from_call(call)
                    normalized_payload, notes = normalize_plan_payload(raw_payload if isinstance(raw_payload, dict) else {}, requested_candidate_attempts=candidate_attempts, task=getattr(task, 'objective', None) or getattr(task, 'instruction', None), success_criteria=getattr(task, 'success_criteria', None), classification=classification if isinstance(classification, dict) else None, investigation=investigation if isinstance(investigation, dict) else None)
                    plan=PlanResult.model_validate(normalized_payload)
                    plan.planner_normalized=True; plan.planner_normalization_notes=notes; plan.planner_fallback_used=False; plan.planner_fallback_reason=None
                    write_json_utf8(run_dir/'plan_normalized.json', {'planner_normalized': True, 'planner_normalization_notes': notes, 'normalized_payload': normalized_payload, 'planner_fallback_used': False, 'planner_fallback_reason': None})
                except Exception as normalize_error:
                    write_json_utf8(run_dir/'plan_normalized.json', {'planner_normalized': False, 'planner_normalization_notes': [], 'normalized_payload': normalized_payload or {}, 'planner_fallback_used': True, 'planner_fallback_reason': f'{original_error}; normalization failed: {normalize_error}'})
                    raise original_error
        except Exception as e:
            call=locals().get('call')
            reason=str(e)
            plan=PlanResult(summary=f'Planner fallback used: {reason}', strategy='parallel_candidates', should_decompose=False, candidate_attempts=candidate_attempts, expected_difficulty='unknown', confidence=0.0, fallback_used=True, planner_fallback_used=True, planner_fallback_reason=reason)
            if not (run_dir/'plan_normalized.json').exists():
                write_json_utf8(run_dir/'plan_normalized.json', {'planner_normalized': False, 'planner_normalization_notes': [], 'normalized_payload': {}, 'raw_payload': getattr(call, 'parsed_json', {}) if call else {}, 'planner_fallback_used': True, 'planner_fallback_reason': reason})
        repair_notes=[]
        if not getattr(plan, 'planner_fallback_used', False):
            plan, repair_notes = repair_plan_against_context(plan, requested_candidate_attempts=candidate_attempts, task=getattr(task, 'objective', None) or getattr(task, 'instruction', None), success_criteria=getattr(task, 'success_criteria', None), classification=classification if isinstance(classification, dict) else None, investigation=investigation if isinstance(investigation, dict) else None)
        if (run_dir/'plan_normalized.json').exists():
            try:
                pn=json.loads((run_dir/'plan_normalized.json').read_text())
                pn.update({'planner_repaired': plan.planner_repaired, 'planner_repair_notes': plan.planner_repair_notes, 'normalized_payload': plan.model_dump(mode='json')})
                write_json_utf8(run_dir/'plan_normalized.json', pn)
            except Exception: pass
        write_text_utf8(run_dir/'plan.raw.txt', (call.raw_text if call else f'ERROR: {plan.planner_fallback_reason or ""}') or '')
        write_json_utf8(run_dir/'plan.json', plan)
        return plan, call if 'call' in locals() else None
    def decompose(self, *, task, plan: PlanResult, investigation, backend: Backend, run_dir: Path, estimate_cost: bool = True) -> tuple[DecompositionResult, LLMCallResult|None]:
        ctx={'task':task.model_dump(mode='json'),'plan':plan.model_dump(mode='json'),'investigation':investigation}
        normalized_payload=None; notes=[]; normalized=False; fallback_reason=None
        try:
            call=self.client.complete_json(backend, 'Return JSON matching DecompositionResult. Decomposition is advisory only.', json.dumps(ctx, indent=2)[:80000], 'DecompositionResult', estimate_cost=estimate_cost)
            try:
                dec=DecompositionResult.model_validate(call.parsed_json)
                dec.advisory_only=True
                write_json_utf8(run_dir/'decomposition_normalized.json', {'decomposition_normalized': False, 'decomposition_normalization_notes': [], 'normalized_payload': dec.model_dump(mode='json'), 'decomposition_fallback_used': False, 'decomposition_fallback_reason': None})
            except Exception as original_error:
                raw_payload=_parse_plan_payload_from_call(call)
                normalized_payload, notes = normalize_decomposition_payload(raw_payload if isinstance(raw_payload, dict) else {}, task=getattr(task, 'objective', None) or getattr(task, 'instruction', None), success_criteria=getattr(task, 'success_criteria', None), plan=plan.model_dump(mode='json'), investigation=investigation if isinstance(investigation, dict) else None)

                if not normalized_payload.get('subtasks') and not str(normalized_payload.get('reason') or '').strip():
                    raise ValueError('normalization produced no useful subtasks or reason')
                dec=DecompositionResult.model_validate(normalized_payload)
                dec.advisory_only=True; dec.planner_normalized=True; dec.planner_normalization_notes=notes; dec.decomposition_normalized=True; dec.decomposition_normalization_notes=notes
                normalized=True
                write_json_utf8(run_dir/'decomposition_normalized.json', {'decomposition_normalized': True, 'decomposition_normalization_notes': notes, 'normalized_payload': normalized_payload, 'decomposition_fallback_used': False, 'decomposition_fallback_reason': None})
        except Exception as e:
            call=locals().get('call')
            fallback_reason=str(e)
            dec=DecompositionResult(should_use_decomposition=False, reason=f'Decomposition fallback used: {e}', subtasks=[], confidence=0.0, advisory_only=True, fallback_used=True, planner_fallback_used=True, planner_fallback_reason=fallback_reason, decomposition_fallback_used=True, decomposition_fallback_reason=fallback_reason)
            if not (run_dir/'decomposition_normalized.json').exists():
                write_json_utf8(run_dir/'decomposition_normalized.json', {'decomposition_normalized': False, 'decomposition_normalization_notes': [], 'normalized_payload': normalized_payload or {}, 'raw_payload': getattr(call, 'parsed_json', {}) if call else {}, 'decomposition_fallback_used': True, 'decomposition_fallback_reason': fallback_reason})
        final=dec.model_dump(mode='json') | {'decomposition_normalized': normalized, 'decomposition_normalization_notes': notes if normalized else [], 'decomposition_fallback_used': bool(getattr(dec, 'fallback_used', False) or getattr(dec, 'planner_fallback_used', False)), 'decomposition_fallback_reason': fallback_reason}
        write_text_utf8(run_dir/'decomposition.raw.txt', (call.raw_text if call else f'ERROR: {fallback_reason or ""}') or '')
        write_json_utf8(run_dir/'decomposition.json', final)
        return dec, call if 'call' in locals() else None
