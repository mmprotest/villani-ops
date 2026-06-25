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
    s=str(value or '').strip().replace(' ', '_')
    return ''.join(ch for ch in s if ch.isalnum() or ch in {'_','-','.'})

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
    _, raw_items=_first_present(data, ('subtasks','tasks','work_items','steps','components','modules'))
    if raw_items is None: raw_items=[]
    if not isinstance(raw_items, list): raw_items=[]
    subtasks=[]
    diff_map={'easy':'easy','medium':'medium','hard':'hard','unknown':'unknown','simple':'easy','low':'easy','moderate':'medium','normal':'medium','complex':'hard','high':'hard','difficult':'hard'}
    risk_map={'low':'low','medium':'medium','high':'high','unknown':'unknown','simple':'low','safe':'low','moderate':'medium','normal':'medium','complex':'high','dangerous':'high'}
    for idx,item in enumerate(raw_items, 1):
        if item is None or item == '': continue
        if isinstance(item, str):
            title=_short_text(item) or f'subtask {idx:03d}'; objective=str(item).strip() or title
            subtasks.append({'id':f'subtask_{idx:03d}','title':title,'objective':objective,'success_criteria':None,'relevant_files':[],'dependencies':[],'expected_difficulty':'unknown','risk':'unknown','confidence':0.0})
            notes.append('Converted string subtask to title/objective')
            continue
        if not isinstance(item, dict): continue
        src=dict(item)
        id_key, raw_id=_first_present(src, ('id','name','key','slug','task_id'))
        sid=_slug(raw_id) if raw_id is not None else f'subtask_{idx:03d}'
        if raw_id is None: notes.append(f'Generated deterministic subtask id {sid}')
        title_key, title=_first_present(src, ('title','name','summary','label'))
        obj_key, objective=_first_present(src, ('objective','description','details','task','instruction','goal'))
        if not title:
            title=_short_text(objective) or sid.replace('_',' ')
            if obj_key == 'description': notes.append('Mapped subtask description to title/objective')
        if not objective: objective=title
        sc_key, sc=_first_present(src, ('success_criteria','acceptance_criteria','validation','done_when'))
        files_key, files=_first_present(src, ('relevant_files','files','file_paths','paths','modules','affected_files'))
        deps_key, deps=_first_present(src, ('dependencies','depends_on','prerequisites','blocked_by'))
        if files_key and files_key != 'relevant_files': notes.append(f'Mapped subtask {files_key} to relevant_files')
        _, diff=_first_present(src, ('expected_difficulty','difficulty','complexity'))
        _, risk=_first_present(src, ('risk','risk_level','impact'))
        _, conf=_first_present(src, ('confidence','confidence_score','certainty'))
        subtasks.append({'id':sid or f'subtask_{idx:03d}','title':str(title).strip(),'objective':str(objective).strip(),'success_criteria':_string_or_none(sc),'relevant_files':_as_list(files),'dependencies':_as_list(deps),'expected_difficulty':diff_map.get(str(diff).strip().lower(), 'unknown'),'risk':risk_map.get(str(risk).strip().lower(), 'unknown'),'confidence':_parse_confidence(conf)})
    flag_key, flag=_first_present(data, ('should_use_decomposition','should_decompose','use_decomposition','decompose','requires_decomposition','needs_decomposition'))
    parsed=_parse_boolish(flag)
    if parsed is None:
        if subtasks: parsed=True; notes.append('Defaulted should_use_decomposition to true because subtasks were present')
        elif isinstance(plan, dict) and plan.get('should_decompose'): parsed=True; notes.append('Defaulted should_use_decomposition to true because plan requested decomposition')
        else: parsed=False
    reason_key, reason=_first_present(data, ('reason','rationale','decomposition_reason','why_decompose','summary','analysis'))
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
Concrete example:
{
  "summary": "Inspect checkout-related modules, then fix pricing and inventory behavior with targeted tests.",
  "strategy": "decompose_then_execute",
  "should_decompose": true,
  "decomposition_reason": "Task spans separable pricing and inventory subsystems.",
  "candidate_attempts": 3,
  "risks": ["Changes may need coordination across checkout modules."],
  "expected_difficulty": "medium",
  "confidence": 0.75
}
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
