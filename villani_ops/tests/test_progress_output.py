from villani_ops.orchestration.progress import ConsoleProgressReporter, NullProgressReporter
from villani_ops.performance.models import SelectionResult

class N:
    def __init__(self, kind, assigned_backend='code', assigned_model='m'):
        self.kind=kind; self.assigned_backend=assigned_backend; self.assigned_model=assigned_model

def test_default_progress_prints_candidate_review_selector_final(capsys, tmp_path):
    r=ConsoleProgressReporter()
    r.start_run(run_dir='.villani-ops/runs/x', mode='performance', runner='villani-code', candidate_attempts=3)
    patch=tmp_path/'patch.diff'; patch.write_text('diff --git a/a b/a')
    r.candidate_started('attempt_001',1,3,'qwen35b'); r.candidate_completed('attempt_001',1,3,{'exit_code':0,'changed_files':['a.py'],'patch_path':str(patch)})
    r.review_completed('attempt_001',1,3,{'decision':'pass','recommended_action':'accept','score':1.0,'acceptance_eligible':True})
    r.selector_completed(SelectionResult(decision='select', selected_attempt_id='attempt_002'), ['Normalized selected_candidate_id to selected_attempt_id=attempt_002'])
    r.final_decision(True, 'attempt_002')
    out=capsys.readouterr().out
    assert 'Villani Ops run started' in out
    assert 'Candidate attempt 1 complete' in out
    assert 'Review complete: pass/accept' in out
    assert 'normalized' in out.lower()
    assert 'Final decision: accepted, winner=attempt_002' in out
    assert 'sk-' not in out and 'ghp_' not in out

def test_quiet_progress_suppresses_updates(capsys):
    r=NullProgressReporter(); r.start_run(run_dir='x', mode='performance', runner='villani-code', candidate_attempts=1); r.final_decision(False, reason='No candidate passed acceptance gates.')
    assert capsys.readouterr().out == ''

def test_verbose_progress_prints_backend(capsys):
    r=ConsoleProgressReporter(verbose=True); r.node_started(N('classify'))
    assert 'Backend: code/m' in capsys.readouterr().out

def test_non_interactive_progress_is_not_special_cased(capsys):
    r=ConsoleProgressReporter(); r.node_started(N('classify'))
    assert 'Classifying task' in capsys.readouterr().out

def test_fallback_line_printed(capsys):
    r=ConsoleProgressReporter(); r.fallback_used('invalid selected attempt', 'attempt_001')
    assert 'Selector fallback selected attempt_001' in capsys.readouterr().out


def test_patch_progress_checks_non_empty_content(capsys, tmp_path):
    r=ConsoleProgressReporter(); p=tmp_path/'patch.diff'
    p.write_text('')
    r.candidate_completed('attempt_001',1,1,{'exit_code':0,'changed_files':['a.py'],'patch_path':str(p)})
    p.write_text('   \n')
    r.candidate_completed('attempt_001',1,1,{'exit_code':0,'changed_files':['a.py'],'patch_path':str(p)})
    p.write_text('diff --git a/a b/a')
    r.candidate_completed('attempt_001',1,1,{'exit_code':0,'changed_files':['a.py'],'patch_path':str(p)})
    r.candidate_completed('attempt_001',1,1,{'exit_code':0,'changed_files':['a.py']})
    out=capsys.readouterr().out
    assert out.count('patch=no') == 3
    assert out.count('patch=yes') == 1

def test_normalization_and_fallback_progress(capsys):
    r=ConsoleProgressReporter()
    r.node_completed(N('plan'), {'strategy':'parallel_candidates','candidate_attempts':3,'should_decompose':False,'planner_normalized':True})
    r.node_completed(N('investigate'), {'relevant_files':['a.py'],'confidence':.7,'investigation_normalized':True})
    r.selector_completed(SelectionResult(decision='select', selected_attempt_id='attempt_001', selector_reason_synthesized=True), [])
    r.node_completed(N('plan'), {'strategy':'parallel_candidates','candidate_attempts':3,'planner_fallback_used':True,'planner_fallback_reason':'bad'})
    out=capsys.readouterr().out
    assert 'normalized=true' in out
    assert 'Selector reason synthesized from candidate evidence' in out
    assert 'Plan complete using fallback' in out
