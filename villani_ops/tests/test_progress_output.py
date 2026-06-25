from villani_ops.orchestration.progress import ConsoleProgressReporter, NullProgressReporter
from villani_ops.performance.models import SelectionResult

class N:
    def __init__(self, kind, assigned_backend='code', assigned_model='m'):
        self.kind=kind; self.assigned_backend=assigned_backend; self.assigned_model=assigned_model

def test_default_progress_prints_candidate_review_selector_final(capsys):
    r=ConsoleProgressReporter()
    r.start_run(run_dir='.villani-ops/runs/x', mode='performance', runner='villani-code', candidate_attempts=3)
    r.candidate_started('attempt_001',1,3,'qwen35b'); r.candidate_completed('attempt_001',1,3,{'exit_code':0,'changed_files':['a.py'],'patch_path':'p'})
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
    assert 'deterministic fallback selected attempt_001' in capsys.readouterr().out
