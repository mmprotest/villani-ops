from villani_ops.agentic.state import CandidateAttemptState
from villani_ops.agentic.state_tooling import execute_tool_with_policy
from villani_ops.tests.test_agentic_tools import state, ctx
from villani_ops.core.acceptance import is_attempt_acceptance_eligible, validation_evidence_strength
from villani_ops.agentic.tools import h_select_winner, h_finalize, OpsSelectWinnerInput, OpsFinalizeRunInput


def _patch(tmp_path):
    p=tmp_path/'run'/'diff.patch'
    p.parent.mkdir(exist_ok=True)
    p.write_text('diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n')
    return str(p)


def _candidate(tmp_path, validation, review=None):
    return CandidateAttemptState(
        attempt_id='candidate_001', status='completed', scope='candidate', exit_code=0,
        patch_path=_patch(tmp_path), changed_files=['a.txt'], validation=validation,
        validation_status=validation.get('status','not_run'), validation_source='ops_run_validation',
        review=review or {'decision':'pass','recommended_action':'accept','score':0.9,'summary':'ok','evidence':[],'issues':[],'blockers':[]},
        review_status='passed')


def test_generated_smoke_pass_is_unverified_not_acceptance_eligible(tmp_path):
    s=state(tmp_path)
    val={'status':'passed','passed':True,'evidence_strength':'generated_smoke','commands':[{'cmd':'shape check','passed':True,'status':'passed','source':'generated','confidence':'low','authority':'diagnostic_only','blocking':False,'evidence_strength':'generated_smoke'}], 'decision':{'status':'passed'}}
    a=_candidate(tmp_path, val); s.candidates.append(a)
    ok, blockers=is_attempt_acceptance_eligible(a,state=s)
    assert ok is False
    assert 'validation_unverified' in blockers
    assert validation_evidence_strength(val) == 'generated_smoke'


def test_generated_smoke_can_select_unverified_and_final_state_exposes_strength(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.investigation={'summary':'i'}; s.execution_path='single_task'; s.plan={'summary':'p'}; s.phase='selecting'
    val={'status':'passed','passed':True,'evidence_strength':'generated_smoke','commands':[{'cmd':'shape check','passed':True,'status':'passed','source':'generated','confidence':'low','authority':'diagnostic_only','blocking':False,'evidence_strength':'generated_smoke'}], 'decision':{'status':'passed'}}
    s.candidates.append(_candidate(tmp_path, val))
    sel=h_select_winner(s, OpsSelectWinnerInput(decision='select', selected_attempt_id='candidate_001', summary='best plausible', confidence=0.6), c)
    assert sel['decision_bucket'] == 'accepted_unverified'
    assert s.selection['decision_bucket'] == 'accepted_unverified'
    assert s.selection['validation_strength'] == 'generated_smoke'
    fin=h_finalize(s, OpsFinalizeRunInput(decision='accepted', selected_attempt_id='candidate_001', summary='best available'), c)
    assert s.final_decision['decision_bucket'] == 'accepted_unverified'
    assert s.final_decision['materialization_signal'] == 'unverified_best_candidate'
    assert s.final_decision['validation_strength'] == 'generated_smoke'


def test_explicit_user_validation_passing_can_verify_acceptance(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.investigation={'summary':'i'}; s.execution_path='single_task'; s.plan={'summary':'p'}; s.phase='selecting'
    val={'status':'passed','passed':True,'evidence_strength':'explicit_user_command','commands':[{'cmd':'user verifier','passed':True,'status':'passed','source':'user_provided','confidence':'high','authority':'acceptance_blocking','blocking':True,'evidence_strength':'explicit_user_command'}], 'decision':{'status':'passed'}}
    s.candidates.append(_candidate(tmp_path, val))
    ok, blockers=is_attempt_acceptance_eligible(s.candidates[0],state=s)
    assert ok is True, blockers
    sel=h_select_winner(s, OpsSelectWinnerInput(decision='select', selected_attempt_id='candidate_001', summary='verified', confidence=0.95), c)
    assert sel['decision_bucket'] == 'accepted_verified'
    assert s.selection['decision_bucket'] == 'accepted_verified'


def test_high_confidence_project_validation_passing_can_verify_acceptance(tmp_path):
    s=state(tmp_path)
    val={'status':'passed','passed':True,'evidence_strength':'high_confidence_project_detected','commands':[{'cmd':'project verifier','passed':True,'status':'passed','source':'project_detected','confidence':'high','authority':'acceptance_blocking','blocking':True,'evidence_strength':'high_confidence_project_detected'}], 'decision':{'status':'passed'}}
    a=_candidate(tmp_path, val); s.candidates.append(a)
    ok, blockers=is_attempt_acceptance_eligible(a,state=s)
    assert ok is True, blockers


def test_reliable_validation_failure_prevents_verified_acceptance(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.investigation={'summary':'i'}; s.execution_path='single_task'; s.plan={'summary':'p'}; s.phase='selecting'
    val={'status':'failed_candidate','passed':False,'evidence_strength':'explicit_user_command','commands':[{'cmd':'user verifier','passed':False,'status':'failed_candidate','source':'user_provided','confidence':'high','authority':'acceptance_blocking','blocking':True,'evidence_strength':'explicit_user_command'}], 'decision':{'status':'failed','blocking_failures':[{'cmd':'user verifier','status':'failed_candidate'}]}}
    s.candidates.append(_candidate(tmp_path, val))
    try:
        h_select_winner(s, OpsSelectWinnerInput(decision='select', selected_attempt_id='candidate_001', summary='bad', confidence=0.9), c)
        assert False, 'selection should fail'
    except ValueError:
        pass
    assert 'validation_failed' in s.candidates[0].acceptance_blockers


def test_infrastructure_and_diagnostic_failures_do_not_auto_reject(tmp_path):
    for status, strength in [('infrastructure_error','infrastructure_error'), ('diagnostic_failed','diagnostic_only')]:
        s=state(tmp_path)
        val={'status':status,'passed':False,'evidence_strength':strength,'commands':[{'cmd':'diag','passed':False,'status':status,'source':'diagnostic','authority':'diagnostic_only','blocking':False,'evidence_strength':strength}], 'decision':{'status':'inconclusive'}}
        a=_candidate(tmp_path, val); s.candidates.append(a)
        _ok, blockers=is_attempt_acceptance_eligible(a,state=s)
        assert 'validation_failed' not in blockers


def test_review_runs_after_validation_and_receives_evidence(tmp_path):
    class CapturingReviewer:
        name='capturing-reviewer'
        def __init__(self): self.payloads=[]
        def review(self, *, state, attempt, scope):
            self.payloads.append(attempt)
            assert attempt['current_validation']['validation_status'] == 'passed'
            assert attempt['current_validation']['validation_evidence_strength'] == 'explicit_user_command'
            return {'decision':'pass','recommended_action':'accept','score':1.0,'summary':'explicit validation passed','evidence':['ok'],'issues':[]}
    s=state(tmp_path); c=ctx(tmp_path); c.reviewer=CapturingReviewer(); s.investigation={'summary':'i'}; s.execution_path='single_task'; s.plan={'summary':'p'}; s.phase='selecting'
    val={'status':'passed','passed':True,'evidence_strength':'explicit_user_command','commands':[{'cmd':'user verifier','passed':True,'status':'passed','source':'user_provided','confidence':'high','authority':'acceptance_blocking','blocking':True,'evidence_strength':'explicit_user_command'}], 'decision':{'status':'passed'}}
    s.candidates.append(CandidateAttemptState(attempt_id='candidate_001',status='completed',scope='candidate',exit_code=0,patch_path=_patch(tmp_path),changed_files=['a.txt'],validation=val,validation_status='passed',validation_source='ops_run_validation'))
    res=execute_tool_with_policy(s,'ops_review_attempt',{'attempt_id':'candidate_001','scope':'candidate'},'rev',c)
    assert not res.is_error, res.content
    assert len(c.reviewer.payloads) == 1
    assert s.candidates[0].review_status == 'passed'
    assert 'validation_missing' not in (s.candidates[0].review.get('blockers') or [])


def test_selection_prefers_reliable_validation_over_shallow_diagnostics(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); s.investigation={'summary':'i'}; s.execution_path='single_task'; s.plan={'summary':'p'}; s.phase='selecting'
    weak={'status':'passed','passed':True,'evidence_strength':'generated_smoke','commands':[{'cmd':'shape','passed':True,'status':'passed','source':'generated','authority':'diagnostic_only','blocking':False,'evidence_strength':'generated_smoke'}], 'decision':{'status':'passed'}}
    strong={'status':'passed','passed':True,'evidence_strength':'explicit_user_command','commands':[{'cmd':'user verifier','passed':True,'status':'passed','source':'user_provided','confidence':'high','authority':'acceptance_blocking','blocking':True,'evidence_strength':'explicit_user_command'}], 'decision':{'status':'passed'}}
    s.candidates.append(_candidate(tmp_path, weak))
    good=_candidate(tmp_path, strong); good.attempt_id='candidate_002'; s.candidates.append(good)
    try:
        h_select_winner(s, OpsSelectWinnerInput(decision='select', selected_attempt_id='candidate_001', summary='weak', confidence=0.9), c)
        assert False, 'weak diagnostic candidate must not beat verified alternative'
    except ValueError as e:
        assert 'verified alternatives' in str(e)
    sel=h_select_winner(s, OpsSelectWinnerInput(decision='select', selected_attempt_id='candidate_002', summary='strong', confidence=0.9), c)
    assert sel['decision_bucket'] == 'accepted_verified'
