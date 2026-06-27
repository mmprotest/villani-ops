from pathlib import Path

from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.state import CandidateAttemptState, OpsRunState, SubtaskState
from villani_ops.agentic.state_tooling import OpsToolContext
from villani_ops.agentic.tools import (
    OpsRunValidationInput,
    ValidationCommand,
    build_agentic_review_payload,
    create_attempt_observation,
    h_validation,
    make_validation_decision,
)
from villani_ops.core.acceptance import is_attempt_acceptance_eligible


class RecorderOnly:
    def record(self, *args, **kwargs):
        pass


def _state(tmp_path):
    repo=tmp_path/'repo'; run=tmp_path/'run'; repo.mkdir(); run.mkdir()
    (repo/'app.txt').write_text('base\n')
    return OpsRunState(run_id='r',run_dir=str(run),repo_path=str(repo),task='fix parts',success_criteria='done',mode='performance',runner='villani-code',candidate_attempts=1,plan={'strategy':'decompose_then_execute'},decomposition={'merge_strategy':'ordered'},decomposition_validated=True,decomposition_accepted=True,execution_path='decomposed_subtasks',subtasks=[SubtaskState(subtask_id='part',title='part',objective='fix part',relevant_files=['app.txt'])])


def _ctx(state):
    return OpsToolContext(run_dir=Path(state.run_dir),recorder=OpsEventRecorder(Path(state.run_dir),'r'),transcript=[],production=False,allow_fake_dependencies=True)


def _attempt(tmp_path, state, scope='subtask'):
    wt=tmp_path/'wt'; wt.mkdir()
    (wt/'app.txt').write_text('new\n')
    patch=tmp_path/'diff.patch'
    patch.write_text('diff --git a/app.txt b/app.txt\n--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-base\n+new\n')
    a=CandidateAttemptState(attempt_id='part_attempt_001',status='completed',scope=scope,subtask_id=('part' if scope=='subtask' else None),changed_files=['app.txt'],patch_path=str(patch),worktree_path=str(wt),review={'decision':'pass','recommended_action':'accept','blockers':[]},review_status='passed',exit_code=0)
    if scope=='subtask':
        state.subtasks[0].attempts=[a]
    else:
        a.attempt_id='candidate_001'; state.candidates=[a]
    return a


def test_validation_decision_separates_authority_classes():
    decision=make_validation_decision({'commands':[
        {'cmd':'focused','passed':True,'authority':'acceptance_blocking','scope':'subtask'},
        {'cmd':'support','passed':False,'authority':'supporting_evidence','scope':'subtask'},
        {'cmd':'diag','passed':False,'authority':'diagnostic_only','scope':'subtask'},
    ]})
    assert decision['status']=='passed'
    assert [x['cmd'] for x in decision['passed_blocking_checks']]==['focused']
    assert [x['cmd'] for x in decision['supporting_failures']]==['support']
    assert [x['cmd'] for x in decision['diagnostic_failures']]==['diag']


def test_raw_failed_command_does_not_automatically_fail_decision(tmp_path):
    s=_state(tmp_path); a=_attempt(tmp_path,s); c=_ctx(s)
    res=h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', source='subtask_focused', authority='acceptance_blocking', scope='subtask', subtask_id='part', purpose='focused'),
        ValidationCommand(cmd='python -c "import sys; sys.exit(1)"', source='diagnostic', authority='diagnostic_only', scope='repo', purpose='probe'),
    ]), c)
    assert res['raw_passed'] is False
    assert res['passed'] is True
    assert res['decision']['status']=='passed'


def test_supporting_and_diagnostic_failures_do_not_block_focused_passing_subtask_acceptance(tmp_path):
    s=_state(tmp_path); a=_attempt(tmp_path,s); c=_ctx(s)
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', source='subtask_focused', authority='acceptance_blocking', scope='subtask', subtask_id='part'),
        ValidationCommand(cmd='python -c "import sys; sys.exit(1)"', source='runner_suggested', authority='supporting_evidence', scope='repo'),
        ValidationCommand(cmd='python -c "import sys; sys.exit(1)"', source='diagnostic', authority='diagnostic_only', scope='repo'),
    ]), c)
    ok, blockers=is_attempt_acceptance_eligible(a, state=s)
    assert ok is True
    assert 'validation_failed' not in blockers
    obs=create_attempt_observation(s,a)
    assert obs.outcome=='accepted'
    assert obs.validation_decision_status=='passed'
    assert len(obs.supporting_validation_failures)==1
    assert len(obs.diagnostic_validation_failures)==1
    assert obs.passed_blocking_validations


def test_acceptance_blocking_failure_blocks_candidate_and_observation(tmp_path):
    s=_state(tmp_path); a=_attempt(tmp_path,s,scope='candidate'); c=_ctx(s)
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[ValidationCommand(cmd='python -c "import sys; sys.exit(1)"', source='user_success_criteria', authority='acceptance_blocking', scope='candidate')]), c)
    ok, blockers=is_attempt_acceptance_eligible(a, state=s)
    assert ok is False
    assert 'validation_failed' in blockers
    obs=create_attempt_observation(s,a)
    assert obs.outcome=='validation_failed'
    assert obs.blocking_validation_failures


def test_subtask_review_payload_exposes_scoped_decision_not_global_blocker(tmp_path):
    s=_state(tmp_path); a=_attempt(tmp_path,s); c=_ctx(s)
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', source='subtask_focused', authority='acceptance_blocking', scope='subtask', subtask_id='part'),
        ValidationCommand(cmd='python -c "import sys; sys.exit(1)"', source='diagnostic', authority='diagnostic_only', scope='repo'),
    ]), c)
    payload=build_agentic_review_payload(s,a,'subtask',s.subtasks[0])
    assert payload['validation_decision']['status']=='passed'
    assert any('Diagnostic and exploratory failures are evidence' in x for x in payload['subtask_review_criteria'])
    assert not any('GLOBAL FAILURES' in str(v) for v in payload.values())

def test_integration_validation_failure_remains_acceptance_blocking(tmp_path):
    s=_state(tmp_path); c=_ctx(s)
    wt=tmp_path/'integration_wt'; wt.mkdir(); (wt/'app.txt').write_text('new\n')
    patch=tmp_path/'integration.patch'; patch.write_text('diff --git a/app.txt b/app.txt\n--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-base\n+new\n')
    s.integration={'attempt_id':'integration_001','scope':'integration','status':'completed','worktree_path':str(wt),'patch_path':str(patch),'changed_files':['app.txt'],'review':{'decision':'pass','recommended_action':'accept','blockers':[]},'review_status':'passed','acceptance_blockers':[]}
    h_validation(s, OpsRunValidationInput(target='integration', commands=[ValidationCommand(cmd='python -c "import sys; sys.exit(1)"', source='integration', authority='acceptance_blocking', scope='integration')]), c)
    ok, blockers=is_attempt_acceptance_eligible(s.integration, state=s)
    assert ok is False
    assert 'validation_failed' in blockers


def test_discovered_command_defaults_non_blocking_without_explicit_plan(tmp_path):
    s=_state(tmp_path); a=_attempt(tmp_path,s); c=_ctx(s)
    res=h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(1)"', source='investigation_discovered', scope='subtask', subtask_id='part', purpose='discovered but not selected'),
    ]), c)
    assert res['commands'][0]['authority']=='supporting_evidence'
    assert res['decision']['status']=='inconclusive'
    ok, blockers=is_attempt_acceptance_eligible(a, state=s)
    assert ok is True
    assert 'validation_failed' not in blockers


def test_subtask_name_similarity_does_not_promote_to_blocking(tmp_path):
    s=_state(tmp_path); a=_attempt(tmp_path,s); c=_ctx(s)
    res=h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(1)" # part parser test', source='investigation_discovered', scope='subtask', subtask_id='part'),
    ]), c)
    assert res['commands'][0]['authority']!='acceptance_blocking'
    assert res['decision']['status']=='inconclusive'


def test_validation_decision_recompute_idempotent_and_late_failures(tmp_path):
    base={'commands':[{'cmd':'focused','passed':True,'authority':'acceptance_blocking','scope':'subtask'}]}
    assert make_validation_decision(base)==make_validation_decision(base)
    with_diag={'commands':base['commands']+[{'cmd':'diag','passed':False,'authority':'diagnostic_only','scope':'subtask'}]}
    assert make_validation_decision(with_diag)['status']=='passed'
    with_auth_fail={'commands':with_diag['commands']+[{'cmd':'auth fail','passed':False,'authority':'acceptance_blocking','scope':'subtask'}]}
    assert make_validation_decision(with_auth_fail)['status']=='failed'


def test_review_payload_labels_non_blocking_failures(tmp_path):
    s=_state(tmp_path); a=_attempt(tmp_path,s); c=_ctx(s)
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', source='subtask_focused', authority='acceptance_blocking', scope='subtask', subtask_id='part'),
        ValidationCommand(cmd='python -c "import sys; sys.exit(1)"', source='diagnostic', authority='diagnostic_only', scope='repo'),
    ]), c)
    payload=build_agentic_review_payload(s,a,'subtask',s.subtasks[0])
    nb=payload['non_blocking_diagnostic_supporting_failures']
    assert nb['label']=='NON-BLOCKING DIAGNOSTIC/SUPPORTING FAILURES'
    assert 'sole reason' in nb['instruction']
    assert payload['validation_decision']['status']=='passed'


def test_scheduler_prioritizes_commit_ready_before_retry(tmp_path):
    from villani_ops.agentic.tools import select_next_subtask
    s=_state(tmp_path); a=_attempt(tmp_path,s)
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', source='subtask_focused', authority='acceptance_blocking', scope='subtask', subtask_id='part'),
    ]), _ctx(s))
    st,last=select_next_subtask(s)
    assert st.subtask_id=='part'
    assert last=='commit_ready'


def test_recovery_commits_review_accepted_focused_passing_subtask_instead_of_retry(tmp_path):
    from villani_ops.agentic.recovery import recommend_next_agentic_action
    s=_state(tmp_path); a=_attempt(tmp_path,s)
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', source='subtask_focused', authority='acceptance_blocking', scope='subtask', subtask_id='part'),
    ]), _ctx(s))
    rec=recommend_next_agentic_action(s)
    assert rec.action=='commit_ready_subtask_acceptance'
    assert rec.tool_name=='ops_run_next_subtask_attempt'


def test_runner_trace_failures_are_diagnostic_and_do_not_poison_attempt_validation(tmp_path):
    from villani_ops.agentic.tools import _attach_imported_validation
    s=_state(tmp_path); a=_attempt(tmp_path,s); c=_ctx(s)
    trace_dir=Path(a.artifacts_dir or tmp_path/'artifacts'); trace_dir.mkdir(exist_ok=True)
    a.artifacts_dir=str(trace_dir)
    (trace_dir/'transcript.json').write_text('{"commands":[{"cmd":"python -m pytest","passed":false,"status":"failed"}]}')
    ev=_attach_imported_validation(s,a)
    assert ev
    assert a.validation_status=='inconclusive'
    assert a.validation['commands'][0]['source']=='runner_trace'
    assert a.validation['commands'][0]['authority']=='diagnostic_only'
    assert a.validation['decision']['status']=='inconclusive'
    ok, blockers=is_attempt_acceptance_eligible(a, state=s)
    assert ok is True
    assert 'validation_failed' not in blockers
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', source='subtask_focused', authority='acceptance_blocking', scope='subtask', subtask_id='part'),
    ]), c)
    assert a.validation_source=='ops_run_validation'
    assert a.validation['decision']['status']=='passed'
    ok, blockers=is_attempt_acceptance_eligible(a, state=s)
    assert ok is True


def test_runner_trace_history_is_labelled_non_blocking_in_review_payload(tmp_path):
    s=_state(tmp_path); a=_attempt(tmp_path,s)
    a.validation_results=[{'validation_source':'villani_code_debug_trace','commands':[{'cmd':'python -m pytest','passed':False,'status':'failed','source':'runner_trace','authority':'diagnostic_only'}]}]
    h_validation(s, OpsRunValidationInput(target='candidate', target_id=a.attempt_id, commands=[
        ValidationCommand(cmd='python -c "import sys; sys.exit(0)"', source='subtask_focused', authority='acceptance_blocking', scope='subtask', subtask_id='part'),
    ]), _ctx(s))
    payload=build_agentic_review_payload(s,a,'subtask',s.subtasks[0])
    hist=payload['non_blocking_runner_trace_history']
    assert hist['label']=='NON-BLOCKING RUNNER TRACE HISTORY'
    assert hist['authority']=='diagnostic_only'
    assert payload['validation_decision']['status']=='passed'
