from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from pathlib import Path
import json
import random
import re
import shlex
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMClient

POLICY='binary_verifier_quality_tie'
LLM_COMPARE_POLICY='binary_verifier_llm_compare_tie'
LLM_COMPARE_FALLBACK_POLICY='binary_verifier_llm_compare_tie_fallback_quality'
VALID_ON_ALL_FAIL={'fail','random','best-confidence'}

_CLEANUP_REQUIREMENT_RE = re.compile(r"\b(interrupt|interruption|cancel|cancellation|cleanup|clean up|shutdown|shut down|resource|rollback)\b", re.IGNORECASE)
_ABNORMAL_REQUIREMENT_RE = re.compile(r"\b(cleanup|clean up|cancellation|cancel|interrupt|interruption|rollback|shutdown|shut down|failure handling|timeout|persistence|recovery|edge cases?|resource management|resource)\b", re.IGNORECASE)
_SEVERE_RISK_RE = re.compile(r"\b(no command log|no repo test|only candidate|pass/fail unknown|critical requirement|no direct evidence|missing cleanup|missing cancellation|broad exception|swallow|fake test|hardcod|ignored failure|unrelated|resource leak)\b", re.IGNORECASE)
_VALIDATION_TERM_RE = re.compile(r"\b(tests?|specs?|checks?|verify|verification|validate|validation|assert|assertion)\b", re.IGNORECASE)
_BEHAVIOR_RE = re.compile(r"\b(runtime|validation|validated|execution|executed|observed|output|cleanup|cancellation|rollback|persistence|end-to-end|e2e|passed|ran|verified)\b", re.IGNORECASE)
_SOURCE_RE = re.compile(r"\b(source|inspection|appears|implemented|code added|function modified|import changed|diff|patch|changed file|updated)\b", re.IGNORECASE)
_COMMAND_KEYS = {'command','cmd','shell'}


def _result_label(v: dict[str, Any]) -> str:
    r = v.get('result')
    if r == 1 or str(v.get('verdict') or '').lower() == 'success': return 'pass'
    if r == 0 or str(v.get('verdict') or '').lower() in {'failure', 'failed', 'fail'}: return 'fail'
    return 'unknown'


def _get_field(obj: Any, *names: str) -> Any:
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None: return value
        if isinstance(obj, dict) and name in obj and obj[name] is not None: return obj[name]
    return None


def _candidate_debug_dir(candidate) -> Path | None:
    value = _get_field(candidate, 'debug_dir', 'candidateDebugDir', 'debugDir', 'resolved_trace_dir', 'resolvedTraceDir', 'debug_root', 'debugRoot', 'artifacts_dir')
    return Path(value) if value else None

def _candidate_stdout_path(candidate) -> Path | None:
    value = _get_field(candidate, 'stdout_path', 'stdoutPath')
    return Path(value) if value else None

def _candidate_stderr_path(candidate) -> Path | None:
    value = _get_field(candidate, 'stderr_path', 'stderrPath')
    return Path(value) if value else None

def _candidate_patch_path(candidate) -> Path | None:
    value = _get_field(candidate, 'patch_path', 'patchPath')
    return Path(value) if value else None

def _candidate_changed_files(candidate) -> list[str]:
    value = _get_field(candidate, 'changed_files', 'changedFiles') or []
    return [str(x) for x in value] if isinstance(value, (list, tuple, set)) else []


def _read_json_file(path) -> dict | list | None:
    try:
        p=Path(path)
        if not p.is_file() or p.stat().st_size > 2_000_000: return None
        data=json.loads(p.read_text(encoding='utf-8', errors='replace'))
        return data if isinstance(data, (dict, list)) else None
    except Exception:
        return None


def _read_jsonl_file(path) -> list[dict]:
    out=[]
    try:
        p=Path(path)
        if not p.is_file() or p.stat().st_size > 5_000_000: return out
        with p.open(encoding='utf-8', errors='replace') as fh:
            for line in fh:
                try:
                    item=json.loads(line)
                    if isinstance(item, dict): out.append(item)
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _walk_command_records(obj: Any) -> list[dict]:
    found=[]
    def visit(x):
        if isinstance(x, dict):
            for k,v in x.items():
                if k in _COMMAND_KEYS and isinstance(v, str) and v.strip():
                    rec=dict(x); rec['_command']=v; found.append(rec)
                elif k in {'args','input','parameters','tool_input','toolInput'} and isinstance(v, dict):
                    cv=v.get('command') or v.get('cmd') or v.get('shell')
                    if isinstance(cv, str) and cv.strip():
                        rec=dict(x); rec['_command']=cv; found.append(rec)
                visit(v)
        elif isinstance(x, list):
            for i in x: visit(i)
    visit(obj)
    return found


def _extract_command_records_from_debug_dir(debug_dir) -> list[dict]:
    if not debug_dir: return []
    root=Path(debug_dir); records=[]
    for name in ('commands.jsonl','tool_calls.jsonl'):
        for rec in _read_jsonl_file(root/name):
            records.extend(_walk_command_records(rec) or [rec])
    for name in ('final_summary.json','summary.json','session_meta.json'):
        data=_read_json_file(root/name)
        if data is not None: records.extend(_walk_command_records(data))
    return records


def _extract_command_records_from_text(text) -> list[dict]:
    records=[]
    for line in str(text or '').splitlines():
        m=re.search(r"(?:\$|command:|cmd:)\s*(.+)$", line, re.IGNORECASE)
        if m: records.append({'_command': m.group(1).strip()})
    return records


def _record_command(record: dict) -> str | None:
    for rec in _walk_command_records(record) or [record]:
        cmd=rec.get('_command') or rec.get('command') or rec.get('cmd') or rec.get('shell')
        if isinstance(cmd, str) and cmd.strip(): return cmd.strip()
    return None


def _passed_from_record(record) -> bool | None:
    if not isinstance(record, dict): return None
    for k in ('exitCode','exit_code','returncode','return_code','code'):
        if k in record:
            try: return int(record[k]) == 0
            except Exception: pass
    for k in ('status','result','success'):
        if k in record:
            v=record[k]
            if isinstance(v, bool): return v
            sv=str(v).strip().lower()
            if sv in {'success','passed','pass','ok','completed'}: return True
            if sv in {'failure','failed','fail','error'}: return False
    return None


def _extract_candidate_commands(candidate, verifier_result) -> list[str]:
    records=_extract_command_records_from_debug_dir(_candidate_debug_dir(candidate))
    for path in (_candidate_stdout_path(candidate), _candidate_stderr_path(candidate)):
        if path and path.is_file():
            try: records.extend(_extract_command_records_from_text(path.read_text(encoding='utf-8', errors='replace')[:200000]))
            except Exception: pass
    seen=set(); out=[]
    for rec in records:
        cmd=_record_command(rec)
        if cmd and cmd not in seen:
            seen.add(cmd); out.append(cmd)
    # Backward-compatible fallback to explicit verifier tools only when no artifact commands exist.
    if not out:
        for x in _list_field(verifier_result, 'toolsUsed'):
            sx=str(x)
            if sx and sx not in seen:
                seen.add(sx); out.append(sx)
    return out


def _normalize_command_tokens(command: str) -> list[str]:
    text = str(command or '').strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


_METADATA_VALIDATION_FIELDS = {'kind', 'type', 'category', 'name', 'label', 'phase', 'purpose', 'role'}
_METADATA_SOURCE_FIELDS = {'source', 'origin', 'provenance', 'owner'}
_REPO_SOURCE_TERMS = {'repo', 'repository', 'existing', 'baseline', 'upstream', 'oracle'}
_CANDIDATE_SOURCE_TERMS = {'candidate', 'generated', 'new', 'ad_hoc', 'adhoc', 'temporary', 'scratch', 'inline'}
_GENERIC_TARGET_FLAGS = {'--file', '--path', '--target', '--case', '--suite', '--config'}
_TEMP_INLINE_RE = re.compile(r"(<<|/tmp/|\b(?:temp|tmp|scratch|generated|ad-hoc|adhoc|one-off|inline)\b)", re.IGNORECASE)
_INLINE_ARG_RE = re.compile(r"(^|\s)-[A-Za-z]*[ce](\s|=).*(\bassert\b|[;{}()])", re.IGNORECASE | re.DOTALL)


def _record_declares_test(record) -> bool:
    if not isinstance(record, dict):
        return False
    for key, value in record.items():
        if str(key) in _METADATA_VALIDATION_FIELDS and _VALIDATION_TERM_RE.search(str(value or '')):
            return True
    return False


def _command_looks_like_validation(command) -> bool:
    return bool(_VALIDATION_TERM_RE.search(str(command or '')))


def _clean_path_token(token: str) -> str:
    return str(token or '').strip().strip('"\'`.,;:()[]{}<>')


def _looks_path_like(token: str) -> bool:
    t = _clean_path_token(token)
    return bool(t) and ('/' in t or '\\' in t or t.startswith('.') or t.startswith('..'))


def _extract_path_like_tokens(command) -> list[str]:
    tokens = _normalize_command_tokens(str(command or ''))
    out=[]
    for i, token in enumerate(tokens):
        clean = _clean_path_token(token)
        if _looks_path_like(clean):
            out.append(clean)
        if clean in _GENERIC_TARGET_FLAGS and i + 1 < len(tokens):
            nxt = _clean_path_token(tokens[i + 1])
            if _looks_path_like(nxt):
                out.append(nxt)
    seen=set(); unique=[]
    for token in out:
        norm = token.replace('\\', '/').lstrip('./')
        if norm and norm not in seen:
            seen.add(norm); unique.append(token)
    return unique


def _normalize_path_for_overlap(path: str) -> str:
    return _clean_path_token(path).replace('\\', '/').lstrip('./')


def _path_overlaps_changed_files(path_token, changed_files) -> bool:
    token = _normalize_path_for_overlap(str(path_token or ''))
    if not token:
        return False
    changed = [_normalize_path_for_overlap(str(x)) for x in (changed_files or []) if str(x).strip()]
    if not changed:
        return False
    direct = [c for c in changed if token == c or token.endswith('/' + c) or c.endswith('/' + token)]
    if direct:
        return True
    basename = token.split('/')[-1]
    basename_matches = [c for c in changed if c.split('/')[-1] == basename]
    return len(basename_matches) == 1 and sum(1 for c in changed if c.split('/')[-1] == basename) == 1


def _metadata_source(record) -> str | None:
    if not isinstance(record, dict):
        return None
    for key, value in record.items():
        if str(key) not in _METADATA_SOURCE_FIELDS:
            continue
        normalized = str(value or '').strip().lower().replace('-', '_')
        if normalized in _REPO_SOURCE_TERMS:
            return 'repo'
        if normalized in _CANDIDATE_SOURCE_TERMS:
            return 'candidate'
    return None


def _classify_test_source(command, changed_files, record=None) -> str:
    meta = _metadata_source(record)
    if meta:
        return meta
    cmd = str(command or '')
    if _TEMP_INLINE_RE.search(cmd) or _INLINE_ARG_RE.search(cmd):
        return 'candidate'
    paths = _extract_path_like_tokens(cmd)
    if paths:
        overlaps = [_path_overlaps_changed_files(path, changed_files) for path in paths]
        if any(overlaps):
            return 'candidate'
        return 'repo' if len(paths) == 1 else 'unknown'
    if _command_looks_like_validation(cmd) or _record_declares_test(record or {}):
        return 'repo'
    return 'unknown'


# Backward-compatible name for callers; internally this means generic validation.
def _is_test_command(command: str) -> bool:
    return _command_looks_like_validation(command)


def _abnormal_path_keywords() -> tuple[str, ...]:
    return (
        'cleanup', 'clean up', 'shutdown', 'shut down', 'teardown', 'close',
        'closed', 'resource', 'resource leak', 'cancellation', 'cancel',
        'cancelled', 'canceled', 'signal', 'interrupt',
        'interruption', 'rollback', 'failure handling',
        'error handling', 'exception', 'timeout', 'recovery', 'persistence',
        'edge case', 'abnormal path',
    )


def _mentions_abnormal_path(text: str) -> bool:
    lowered = str(text or '').lower()
    return any(keyword in lowered for keyword in _abnormal_path_keywords())


def _row_direct_evidence_texts(row) -> list[str]:
    texts = []
    for evidence in row.get('direct_behavioral_evidence', []):
        if not isinstance(evidence, dict):
            continue
        if evidence.get('requirement') == 'candidate test':
            continue
        texts.append(str(evidence.get('evidence') or ''))
    return texts


def _row_test_evidence_texts(row) -> list[str]:
    texts = []
    for test in row.get('tests_run', []):
        if not isinstance(test, dict):
            continue
        parts = [test.get('command'), test.get('path'), test.get('evidence'), test.get('stdout'), test.get('stderr')]
        texts.append(' '.join(str(part) for part in parts if part))
    return texts


def _has_repo_abnormal_path_test_evidence(row) -> bool:
    for test in row.get('tests_run', []):
        if test.get('source') == 'repo' and test.get('passed') is True:
            if _mentions_abnormal_path(' '.join(str(v) for v in test.values())):
                return True
    return False


def _has_candidate_abnormal_path_test_evidence(row) -> bool:
    for test in row.get('tests_run', []):
        if test.get('source') == 'candidate' and test.get('passed') is True:
            if _mentions_abnormal_path(' '.join(str(v) for v in test.values())):
                return True
    return False


def _has_direct_abnormal_path_evidence(row) -> bool:
    if _has_repo_abnormal_path_test_evidence(row):
        return True
    return any(_mentions_abnormal_path(text) for text in _row_direct_evidence_texts(row))


def _extract_candidate_tests(candidate, verifier_result, commands, changed_files) -> list[dict]:
    tests=[]; seen=set()
    records=_extract_command_records_from_debug_dir(_candidate_debug_dir(candidate))
    by_cmd={}
    for rec in records:
        cmd=_record_command(rec)
        if cmd and cmd not in by_cmd: by_cmd[cmd]=rec
    for cmd in commands:
        rec=by_cmd.get(cmd, {})
        if _record_declares_test(rec) or _command_looks_like_validation(cmd):
            item={'command': cmd, 'passed': _passed_from_record(rec), 'source': _classify_test_source(cmd, changed_files, rec)}
            for key in ('path', 'evidence', 'stdout', 'stderr'):
                if isinstance(rec, dict) and rec.get(key):
                    item[key] = str(rec.get(key))[:500]
            tests.append(item); seen.add(cmd)
    if not tests:
        for field, passed in (('successEvidence', True), ('failureEvidence', False)):
            for item in _list_field(verifier_result, field):
                text=str(item)
                if _command_looks_like_validation(text) and text not in seen:
                    tests.append({'command': text[:300], 'passed': passed, 'source': 'unknown'}); seen.add(text)
    return tests


def _strength(text: Any) -> str:
    t = str(text or '')
    if _STRONG_SUCCESS_EVIDENCE_RE.search(t): return 'strong'
    if t.strip(): return 'medium'
    return 'weak'


def _evidence_item(requirement: str, evidence: Any) -> dict[str, str]:
    return {'requirement': str(requirement or 'unspecified requirement'), 'evidence': str(evidence or ''), 'strength': _strength(evidence)}


def build_candidate_evidence_matrix(candidates: list[Any], selected_candidate_id: str | None = None) -> list[dict[str, Any]]:
    rows=[]
    for c in candidates:
        v=_vr(c); cid=str(_cid(c) or '')
        reqs=[r for r in _list_field(v,'requirementResults') if isinstance(r, dict)]
        success=_list_field(v,'successEvidence'); missing=[str(x) for x in _list_field(v,'missingEvidence')]
        risks=[str(x) for x in _list_field(v,'riskFlags')]
        changed=_candidate_changed_files(c)
        commands=_extract_candidate_commands(c, v)
        tests=_extract_candidate_tests(c, v, commands, changed)
        missing_flags=missing + [str(r.get('requirement') or r.get('id') or 'unsatisfied requirement') for r in reqs if r.get('status') == 'unsatisfied']
        if v.get('criticalRequirementCovered') is False: missing_flags.append('critical requirement was not covered')
        elif v.get('criticalRequirementCovered') is True and v.get('criticalRequirementCoverageProven') is not True: missing_flags.append('critical requirement coverage was not proven by same-condition evidence')
        direct=[]; source=[]
        for t in tests:
            if t.get('passed') is True:
                direct.append(_evidence_item('repo test' if t.get('source')=='repo' else 'candidate test', t['command']))
        for x in success:
            success_text = str(x)
            target = source
            if _BEHAVIOR_RE.search(success_text) and (not _SOURCE_RE.search(success_text) or _mentions_abnormal_path(success_text)):
                target = direct
            target.append(_evidence_item((reqs[0].get('requirement') if reqs else v.get('criticalRequirement')) or 'task behavior', x))
        if v.get('directEvidenceForCriticalRequirement'):
            direct.append(_evidence_item(v.get('criticalRequirement') or 'critical requirement', v.get('directEvidenceForCriticalRequirement')))
        for r in reqs:
            ev=r.get('evidence') or r.get('directEvidence') or r.get('reason')
            if r.get('status') == 'satisfied' and ev and _BEHAVIOR_RE.search(str(ev)):
                direct.append(_evidence_item(r.get('requirement') or r.get('id') or 'requirement', ev))
        evidence_probe = {
            'tests_run': tests,
            'direct_behavioral_evidence': direct,
        }
        for r in reqs:
            req=str(r.get('requirement') or r.get('id') or '')
            if r.get('status') == 'satisfied' and _ABNORMAL_REQUIREMENT_RE.search(req):
                if _has_direct_abnormal_path_evidence(evidence_probe):
                    continue
                if _has_candidate_abnormal_path_test_evidence(evidence_probe):
                    risks.append('abnormal-path evidence is candidate-authored only')
                    continue
                missing_flags.append('no direct evidence for abnormal-path requirement: '+req)
        if changed: source.append(_evidence_item('changed files', ', '.join(changed)))
        if _candidate_patch_path(c): source.append(_evidence_item('patch', str(_candidate_patch_path(c))))
        if not commands: risks.append('no command log evidence')
        if commands and not tests: risks.append('no repo test evidence found')
        if tests and all(t.get('passed') is None for t in tests): risks.append('test pass/fail unknown for all tests')
        if tests and any(t.get('source')=='candidate' for t in tests) and not any(t.get('source')=='repo' for t in tests): risks.append('only candidate-authored tests found')
        satisfied=sum(1 for r in reqs if r.get('status')=='satisfied'); total=max(1,len(reqs))
        if not reqs and v.get('criticalRequirementCovered') is True: satisfied=1; total=1
        if v.get('criticalRequirementCoverageProven') is True: satisfied=max(satisfied,total)
        repo_tests=sum(1 for t in tests if t['source']=='repo' and t['passed'] is True)
        candidate_tests=sum(1 for t in tests if t['source']=='candidate' and t['passed'] is True)
        severe=sum(1 for r in risks + missing_flags if _SEVERE_RISK_RE.search(str(r)))
        risk_penalty=len(risks)*2.0 + len(missing_flags)*4.0 + severe*6.0
        score={'direct_behavioral':float(sum({'strong':4,'medium':3,'weak':1}[e['strength']] for e in direct)), 'repo_tests':float(repo_tests*8), 'candidate_tests':float(candidate_tests*1.5), 'source_inference':float(min(len(source),3)*0.75), 'requirement_coverage':float(12*satisfied/total), 'risk_penalty':float(risk_penalty), 'final':0.0}
        score['final']=score['direct_behavioral']+score['repo_tests']+score['candidate_tests']+score['source_inference']+score['requirement_coverage']-score['risk_penalty']
        rows.append({'candidate_id':cid,'verifier_result':_result_label(v),'verifier_confidence':v.get('confidence'),'commands_run':commands,'tests_run':tests,'files_changed':changed,'debug_dir':str(_candidate_debug_dir(c)) if _candidate_debug_dir(c) else None,'stdout_path':str(_candidate_stdout_path(c)) if _candidate_stdout_path(c) else None,'stderr_path':str(_candidate_stderr_path(c)) if _candidate_stderr_path(c) else None,'patch_path':str(_candidate_patch_path(c)) if _candidate_patch_path(c) else None,'direct_behavioral_evidence':direct,'source_level_inference_evidence':source,'missing_requirement_flags':missing_flags,'risk_flags':risks,'evidence_score':score,'selection_status':'selected' if cid==selected_candidate_id else 'rejected','final_selection_reason':''})
    return rows


def _evidence_rank_components(row: dict[str, Any]) -> tuple[Any, ...]:
    s=row['evidence_score']; severe=sum(1 for r in row['risk_flags'] + row['missing_requirement_flags'] if _SEVERE_RISK_RE.search(str(r)))
    return (-severe, -len(row['missing_requirement_flags']), -len(row['risk_flags']), s['repo_tests'], s['direct_behavioral'], s['candidate_tests'], s['requirement_coverage'], s['source_inference'], -s['risk_penalty'], s['final'], float(row['verifier_confidence'] or 0))

def _evidence_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return _evidence_rank_components(row)


def rank_candidates_by_evidence(candidates: list[Any]) -> list[dict[str, Any]]:
    rows=build_candidate_evidence_matrix(candidates)
    rows=sorted(rows, key=lambda r: r['candidate_id'])
    rows.sort(key=_evidence_rank_components, reverse=True)
    return rows


def _snippet(items, key='evidence'):
    for item in items or []:
        if isinstance(item, dict):
            val=item.get(key) or item.get('command')
            if val: return str(val)
        elif item: return str(item)
    return None


def _make_selection_reason(row, winner, all_rows) -> str:
    repo=next((t['command'] for t in row.get('tests_run',[]) if t.get('source')=='repo' and t.get('passed') is True), None)
    direct=_snippet(row.get('direct_behavioral_evidence'))
    covered=f"covered {row['evidence_score']['requirement_coverage']:.0f} requirement-coverage points"
    loser=next((r for r in all_rows if r is not row), None)
    parts=[f"Selected because {row['candidate_id']} had the strongest evidence-ranked selection record"]
    details=[]
    if direct: details.append(f"had direct behavioral evidence: {direct}")
    if repo: details.append(f"passed repo test evidence from \"{repo}\"")
    details.append(covered)
    if not row.get('missing_requirement_flags'): details.append('had no missing requirement flags')
    if loser:
        gap=_snippet(loser.get('missing_requirement_flags')) or _snippet(loser.get('risk_flags')) or _snippet(loser.get('source_level_inference_evidence')) or 'weaker evidence'
        details.append(f"while closest losing candidate {loser['candidate_id']} had {gap}")
    return parts[0] + ' ' + ', '.join(details) + '.'


def _make_rejection_reason(row, winner) -> str:
    gaps=[]
    if row.get('missing_requirement_flags'): gaps.append('lacked '+str(row['missing_requirement_flags'][0]))
    if row.get('risk_flags'): gaps.append('had '+str(row['risk_flags'][0]))
    if not any(t.get('source')=='repo' and t.get('passed') is True for t in row.get('tests_run',[])): gaps.append('had no passed repo test evidence')
    if not row.get('direct_behavioral_evidence') and row.get('source_level_inference_evidence'): gaps.append('only provided source-level inference')
    win_direct=_snippet((winner or {}).get('direct_behavioral_evidence'))
    if winner and win_direct: gaps.append(f"while {winner['candidate_id']} had direct behavioral validation: {win_direct}")
    return 'Rejected because ' + '; '.join(gaps or ['its concrete evidence was weaker than the selected candidate']) + '.'


def _finalize_evidence_reasons(rows: list[dict[str, Any]], winner_id: str | None) -> list[dict[str, Any]]:
    rows=sorted(rows, key=lambda r: r['candidate_id']); rows.sort(key=_evidence_rank_components, reverse=True)
    winner=next((r for r in rows if r['candidate_id']==winner_id), None)
    for r in rows:
        if r['candidate_id']==winner_id:
            r['selection_status']='selected'; r['final_selection_reason']=_make_selection_reason(r, winner, rows)
        else:
            r['selection_status']='rejected'; r['final_selection_reason']=_make_rejection_reason(r, winner)
    return rows


def write_candidate_evidence_matrix(path: str | Path, matrix: list[dict[str, Any]]) -> Path:
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(matrix, indent=2, default=str), encoding='utf-8'); return p


def _bullets(title, values):
    lines=[f"- {title}:"]
    vals=list(values or [])
    if not vals: return lines+['  - none recorded']
    for v in vals[:5]: lines.append(f"  - {v}")
    return lines


def write_selection_report(path: str | Path, matrix: list[dict[str, Any]], winner_id: str | None) -> Path:
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    winner=next((r for r in matrix if r['candidate_id']==winner_id), None)
    lines=['# Selection Report','','## Winner',f"- Candidate: {winner_id or ''}",f"- Reason: {(winner or {}).get('final_selection_reason','No winner selected.')}"]
    llm_recommended=next((r for r in matrix if r.get('llm_comparison_recommended')), None)
    if llm_recommended:
        note=llm_recommended.get('llm_comparison_advisory_note') or ('LLM comparison matched the evidence-ranked winner.' if llm_recommended['candidate_id']==winner_id else f"LLM comparison recommended {llm_recommended['candidate_id']}, but evidence-ranked selector selected {winner_id} because {(winner or {}).get('final_selection_reason','it had stronger evidence.')}")
        lines += ['', '## LLM Comparison Advisory', f"- Recommended candidate: {llm_recommended['candidate_id']}", f"- Evidence-ranked winner: {winner_id or ''}", '- Used for final decision: no', f"- Notes: {note}"]
    lines += ['', '## Candidate Ranking', 'candidate_id | verifier_result | coverage | direct_behavioral | repo_tests | source_inference | risk_flags | status | reason', '--- | --- | ---: | ---: | ---: | ---: | --- | --- | ---']
    ranked=sorted(matrix, key=lambda r:r['candidate_id']); ranked.sort(key=_evidence_rank_components, reverse=True)
    for r in ranked:
        s=r['evidence_score']; lines.append(f"{r['candidate_id']} | {r['verifier_result']} | {s['requirement_coverage']:.2f} | {s['direct_behavioral']:.2f} | {s['repo_tests']:.2f} | {s['source_inference']:.2f} | {'; '.join(r['risk_flags'])} | {r['selection_status']} | {r['final_selection_reason']}")
    lines += ['', '## Why the winner won', (winner or {}).get('final_selection_reason','No winner selected.')]
    if winner:
        lines += _bullets('Direct behavioral evidence', [e.get('evidence') for e in winner.get('direct_behavioral_evidence',[])])
        lines += _bullets('Repo test evidence', [t.get('command') for t in winner.get('tests_run',[]) if t.get('source')=='repo'])
        cand_tests=[t.get('command') for t in winner.get('tests_run',[]) if t.get('source')=='candidate']
        if cand_tests: lines += _bullets('Candidate-authored test evidence', cand_tests)
        lines += _bullets('Source-level inference', [e.get('evidence') for e in winner.get('source_level_inference_evidence',[])])
        lines += ['- Missing requirements avoided: ' + ('none recorded' if not winner.get('missing_requirement_flags') else '; '.join(winner.get('missing_requirement_flags',[])[:3]))]
        losers=[r for r in ranked if r['candidate_id']!=winner_id]
        lower='; '.join((losers[0].get('risk_flags') or losers[0].get('missing_requirement_flags') or ['weaker concrete evidence'])[:3]) if losers else 'no losing candidate'
        lines += ['- Risks avoided or lower risks versus strongest loser: '+lower]
    lines += ['', '## Why other candidates lost']
    for r in ranked:
        if r['candidate_id']==winner_id: continue
        lines += [f"### {r['candidate_id']}", r['final_selection_reason']]
        lines += _bullets('Missing requirement flags', r.get('missing_requirement_flags'))
        lines += _bullets('Risk flags', r.get('risk_flags'))
        lines += ['- Test evidence weakness: ' + ('no passed repo test evidence' if not any(t.get('source')=='repo' and t.get('passed') is True for t in r.get('tests_run',[])) else 'has passed repo test evidence')]
        lines += ['- Direct behavioral evidence weakness: ' + ('none recorded' if not r.get('direct_behavioral_evidence') else _snippet(r.get('direct_behavioral_evidence')))]
        if winner: lines += [f"- Specific comparison to winner: {winner['candidate_id']} - {winner.get('final_selection_reason','')}"]
    gaps=[]
    for r in matrix:
        if not r.get('commands_run'): gaps.append(f"{r['candidate_id']}: no command logs found")
        if not any(t.get('source')=='repo' for t in r.get('tests_run',[])): gaps.append(f"{r['candidate_id']}: no repo test evidence found")
        if r.get('tests_run') and all(t.get('source')=='candidate' for t in r.get('tests_run',[])): gaps.append(f"{r['candidate_id']}: only candidate-authored tests found")
        if r.get('tests_run') and all(t.get('passed') is None for t in r.get('tests_run',[])): gaps.append(f"{r['candidate_id']}: pass/fail unknown")
        for g in r.get('missing_requirement_flags',[]):
            if 'coverage was not proven' in g: gaps.append(f"{r['candidate_id']}: critical requirement coverage unproven")
            if 'abnormal-path' in g: gaps.append(f"{r['candidate_id']}: no direct evidence for abnormal-path requirement")
            gaps.append(f"{r['candidate_id']}: {g}")
    lines += ['', '## Evidence gaps', *(f"- {g}" for g in (gaps or ['No evidence gaps recorded.']))]
    p.write_text('\n'.join(lines)+'\n', encoding='utf-8'); return p


_STRONG_SUCCESS_EVIDENCE_RE = re.compile(
    r"\b(test|tests|passed|validation|validated|verified|behavior|runtime|end-to-end|e2e|integration|executed|ran|imported|output|cleanup|cancellation|install|fresh|downstream|smoke)\b",
    re.IGNORECASE,
)

@dataclass
class SelectionResult:
    schemaVersion: str
    selectionPolicy: str
    seed: int
    onAllFail: str
    winnerCandidateId: str|None
    winnerResult: int|None
    tieBreak: bool
    candidatePool: list[str]
    allCandidates: list[dict[str,Any]]
    reason: str
    fallback: str|None=None
    qualityTieBreakApplied: bool=False
    winnerQualityKey: dict[str,Any]|None=None
    candidateQuality: list[dict[str,Any]]|None=None
    llmComparison: dict[str,Any]|None=None
    def to_dict(self): return self.__dict__.copy()

def _vr(c):
    if hasattr(c,'verifier_result'): return getattr(c,'verifier_result') or {}
    return c.get('verifier_result') or c.get('verifierResult') or c

def _cid(c): return getattr(c,'candidate_id',None) or c.get('candidateId') or c.get('candidate_id')

def _confidence(c):
    try: return float((_vr(c).get('confidence') if _vr(c).get('confidence') is not None else 0.0) or 0.0)
    except Exception: return 0.0

def _summary(c):
    v=_vr(c)
    return {'candidateId':_cid(c),'result':v.get('result'),'verdict':v.get('verdict'),'confidence':v.get('confidence'),'recommendedAction':v.get('recommendedAction'),'criticalRequirement':v.get('criticalRequirement'),'directEvidenceForCriticalRequirement':v.get('directEvidenceForCriticalRequirement'),'criticalRequirementCovered':v.get('criticalRequirementCovered'),'criticalRequirementCoverageProven':v.get('criticalRequirementCoverageProven'),'criticalRequirementEvidenceMatch':v.get('criticalRequirementEvidenceMatch'),'warnings':v.get('warnings'),'traceDir':v.get('traceDir') or v.get('trace_dir')}

def _recommended_action(c):
    return str((_vr(c).get('recommendedAction') or '')).strip().lower()

def _critical_covered(c) -> bool:
    return _vr(c).get('criticalRequirementCovered') is True

def _critical_proven(c) -> bool:
    return _vr(c).get('criticalRequirementCoverageProven') is True

def _strong_accept(c) -> bool:
    return _recommended_action(c)=='accept' and _critical_covered(c) and _critical_proven(c)

def _list_field(v: dict[str, Any], name: str) -> list[Any]:
    value = v.get(name)
    return value if isinstance(value, list) else []

def _uncertainty_level(v: dict[str, Any]) -> str:
    uncertainty = v.get('uncertainty')
    level = uncertainty.get('level') if isinstance(uncertainty, dict) else v.get('uncertaintyLevel')
    return str(level or 'medium').lower()

def verifier_quality_details(candidate: Any) -> dict[str, Any]:
    """Return generic verifier-quality diagnostics for candidate selection."""
    v = _vr(candidate)
    level = _uncertainty_level(v)
    uncertainty_rank = {'low': 2, 'medium': 1, 'high': 0}.get(level, 1)
    failure_count = len(_list_field(v, 'failureEvidence'))
    missing_count = len(_list_field(v, 'missingEvidence'))
    risk_count = len(_list_field(v, 'riskFlags'))
    reqs = _list_field(v, 'requirementResults')
    satisfied_count = sum(1 for r in reqs if isinstance(r, dict) and r.get('status') == 'satisfied')
    unsatisfied_count = sum(1 for r in reqs if isinstance(r, dict) and r.get('status') == 'unsatisfied')
    success = _list_field(v, 'successEvidence')
    behavioral_count = sum(1 for item in success if _STRONG_SUCCESS_EVIDENCE_RE.search(str(item)))
    tool_count = len(_list_field(v, 'toolsUsed'))
    confidence = _confidence(candidate)
    critical_covered = 1 if _critical_covered(candidate) else 0
    critical_proven = 1 if _critical_proven(candidate) else 0
    strong_accept = 1 if _strong_accept(candidate) else 0
    key = (
        critical_proven,
        critical_covered,
        strong_accept,
        uncertainty_rank,
        -failure_count,
        -missing_count,
        -risk_count,
        satisfied_count,
        -unsatisfied_count,
        behavioral_count,
        tool_count,
        confidence,
    )
    return {
        'candidateId': _cid(candidate),
        'result': v.get('result'),
        'confidence': confidence,
        'criticalRequirementCoverageProven': bool(critical_proven),
        'criticalRequirementCovered': bool(critical_covered),
        'strongAccept': bool(strong_accept),
        'uncertaintyLevel': level if level in {'low','medium','high'} else 'medium',
        'failureEvidenceCount': failure_count,
        'missingEvidenceCount': missing_count,
        'riskFlagCount': risk_count,
        'satisfiedRequirementCount': satisfied_count,
        'unsatisfiedRequirementCount': unsatisfied_count,
        'behavioralSuccessEvidenceCount': behavioral_count,
        'toolCount': tool_count,
        'qualityKey': key,
    }

def verifier_quality_key(candidate: Any) -> tuple[Any, ...]:
    return verifier_quality_details(candidate)['qualityKey']

def _quality_rows(candidates: list[Any]) -> list[dict[str, Any]]:
    rows=[verifier_quality_details(c) for c in candidates]
    ranked=sorted({r['qualityKey'] for r in rows}, reverse=True)
    ranks={key:i+1 for i,key in enumerate(ranked)}
    for row in rows:
        row['qualityRank']=ranks[row['qualityKey']]
        row['qualityKey']={
            'criticalRequirementCoverageProven': row['qualityKey'][0],
            'criticalRequirementCovered': row['qualityKey'][1],
            'strongAccept': row['qualityKey'][2],
            'uncertaintyRank': row['qualityKey'][3],
            'negativeFailureEvidenceCount': row['qualityKey'][4],
            'negativeMissingEvidenceCount': row['qualityKey'][5],
            'negativeRiskFlagCount': row['qualityKey'][6],
            'satisfiedRequirementCount': row['qualityKey'][7],
            'negativeUnsatisfiedRequirementCount': row['qualityKey'][8],
            'behavioralSuccessEvidenceCount': row['qualityKey'][9],
            'toolCount': row['qualityKey'][10],
            'confidence': row['qualityKey'][11],
        }
    return rows

def _pick_by_quality(bucket: list[Any], rng: random.Random) -> tuple[Any|None, list[str], bool]:
    if not bucket: return None, [], False
    ranked_rows = rank_candidates_by_evidence(bucket)
    best_id = ranked_rows[0]['candidate_id']
    best_key = _evidence_rank_components(ranked_rows[0])
    best = [r['candidate_id'] for r in ranked_rows if _evidence_rank_components(r) == best_key]
    return next(c for c in bucket if _cid(c) == best_id), best, len(best) > 1

def select_winner(candidates:list[Any], seed:int, on_all_fail:str='fail')->SelectionResult:
    if on_all_fail not in VALID_ON_ALL_FAIL: raise ValueError('invalid on_all_fail')
    rng=random.Random(seed); allc=[_summary(c) for c in candidates]; candidate_quality=_quality_rows(candidates)
    successes=[c for c in candidates if _vr(c).get('result')==1]
    zeros=[c for c in candidates if _vr(c).get('result')==0]
    errors=[c for c in candidates if _vr(c).get('result') not in (0,1)]
    fallback=None; quality_applied=False
    if successes:
        accepted=[c for c in successes if _strong_accept(c)]
        bucket=accepted or successes
        win,pool,random_tie=_pick_by_quality(bucket,rng); quality_applied=len(bucket)>1
        if accepted:
            reason=(f'Selected {_cid(win)} by verifier quality tie-break among candidates with result = 1 and recommendedAction = accept with evidence-proven critical-requirement coverage.' if not random_tie else 'Selected randomly among strong accept-recommended candidates tied on verifier result and verifier quality key.')
        else:
            reason=(f'Selected {_cid(win)} by verifier quality tie-break among candidates with result = 1.' if not random_tie else 'Selected randomly among candidates tied on verifier result and verifier quality key.')
    elif on_all_fail=='fail':
        return SelectionResult('villani-ops-verifier-parallel-selection-v1',POLICY,seed,on_all_fail,None,None,False,[],allc,'No candidates had verifier result = 1; on-all-fail=fail skipped integration.',candidateQuality=candidate_quality)
    elif on_all_fail=='random':
        bucket=zeros or errors; pool=[_cid(c) for c in bucket]; win=rng.choice(bucket) if bucket else None; fallback='all-fail-random'; reason='All candidates failed; selected random fallback.'
    else:
        if zeros:
            maxc=max(_confidence(c) for c in zeros); bucket=[c for c in zeros if _confidence(c)==maxc]
            if len(bucket)>1:
                win,pool,random_tie=_pick_by_quality(bucket,rng); quality_applied=True
            else:
                win=bucket[0]; pool=[_cid(win)]; random_tie=False
            reason=('All candidates failed; selected result = 0 candidate with highest verifier confidence.' if not random_tie else 'All candidates failed; selected randomly among result = 0 candidates tied on verifier confidence and verifier quality key.')
        else:
            bucket=errors; pool=[_cid(c) for c in bucket]; win=rng.choice(bucket) if bucket else None; random_tie=len(bucket)>1; reason='All candidates had verifier errors; selected random error fallback.'
        fallback='all-fail-best-confidence'
    v=_vr(win) if win else {}
    winner_quality=next((r for r in candidate_quality if r['candidateId']==_cid(win)), None) if win else None
    return SelectionResult('villani-ops-verifier-parallel-selection-v1',POLICY,seed,on_all_fail,_cid(win) if win else None,v.get('result'),len(pool)>1,pool,allc,reason,fallback,quality_applied,winner_quality,candidate_quality)


def _truncate_text(value: Any, limit: int = 1200) -> str:
    text = str(value or '')
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated {len(text)-limit} chars]"

def _truncate_list(value: Any, max_items: int = 5, item_limit: int = 500) -> list[Any]:
    items = value if isinstance(value, list) else []
    out = [_truncate_text(item, item_limit) for item in items[:max_items]]
    if len(items) > max_items:
        out.append(f"…[truncated {len(items)-max_items} items]")
    return out

def build_llm_comparison_packet(candidates: list[Any], *, diff_limit: int = 2000, evidence_limit: int = 500) -> list[dict[str, Any]]:
    packets=[]
    for c in candidates:
        v=_vr(c)
        patch_path = getattr(c, 'patch_path', None) or (c.get('patchPath') if isinstance(c, dict) else None)
        diff=''
        if patch_path:
            try: diff=Path(patch_path).read_text(encoding='utf-8', errors='replace')
            except Exception: diff=''
        changed = getattr(c, 'changed_files', None) or (c.get('changedFiles') if isinstance(c, dict) else None) or []
        packets.append({
            'candidateId': _cid(c),
            'verifier': {
                'result': v.get('result'),
                'confidence': v.get('confidence'),
                'recommendedAction': v.get('recommendedAction'),
                'criticalRequirement': v.get('criticalRequirement'),
                'directEvidenceForCriticalRequirement': v.get('directEvidenceForCriticalRequirement'),
                'criticalRequirementCovered': v.get('criticalRequirementCovered'),
                'criticalRequirementEvidenceRefs': _truncate_list(v.get('criticalRequirementEvidenceRefs'), 10, evidence_limit),
                'criticalRequirementCoverageProven': v.get('criticalRequirementCoverageProven'),
                'criticalRequirementEvidenceMatch': v.get('criticalRequirementEvidenceMatch') if isinstance(v.get('criticalRequirementEvidenceMatch'), dict) else {},
                'warnings': _truncate_list(v.get('warnings'), 5, evidence_limit),
                'reason': _truncate_text(v.get('reason') or v.get('summary'), evidence_limit),
                'requirementResults': _truncate_list(v.get('requirementResults'), 5, evidence_limit),
                'successEvidence': _truncate_list(v.get('successEvidence'), 5, evidence_limit),
                'failureEvidence': _truncate_list(v.get('failureEvidence'), 5, evidence_limit),
                'missingEvidence': _truncate_list(v.get('missingEvidence'), 5, evidence_limit),
                'riskFlags': _truncate_list(v.get('riskFlags'), 5, evidence_limit),
            },
            'changedFiles': list(changed)[:30],
            'diffExcerpt': _truncate_text(diff, diff_limit),
        })
    return packets

def select_success_with_llm_comparison(*, task: str, success_criteria: str | None, candidates: list[Any], model: str | None, base_url: str | None, provider: str | None, api_key: str | None, timeout_s: int | None = None) -> dict[str, Any] | None:
    eligible={_cid(c) for c in candidates}
    if not eligible or not model or not base_url:
        return None
    backend=Backend(name='verifier-parallel-selector', provider=provider or 'openai-compatible', base_url=base_url, model=model, api_key=api_key)
    packet=build_llm_comparison_packet(candidates)
    system='You are a strict comparative selector. Return strict JSON only.'
    user=json.dumps({
        'instruction': 'Compare candidate patches against the task and success criteria. Prefer direct evidence that the riskiest or most specific requirements are satisfied. Prefer behavioural evidence over source-shape evidence when available. Penalize unresolved failure evidence, missing required outputs, or weak/indirect validation. Choose exactly one eligible candidate id.',
        'responseSchema': {'selectedCandidateId': 'candidate id from eligible list', 'reason': 'brief reason'},
        'task': task,
        'successCriteria': success_criteria,
        'eligibleCandidateIds': sorted(eligible),
        'candidates': packet,
    }, indent=2)[:60000]
    call=LLMClient().complete_json(backend, system, user, 'VerifierParallelSelection', timeout_seconds=timeout_s, estimate_cost=False)
    data=call.parsed_json or {}
    selected=data.get('selectedCandidateId') or data.get('selected_candidate_id')
    if selected not in eligible:
        raise ValueError(f'LLM comparative selector returned invalid candidate id: {selected}')
    return {'selectedCandidateId': selected, 'reason': str(data.get('reason') or data.get('reasoning') or ''), 'packet': packet}
