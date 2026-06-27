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
