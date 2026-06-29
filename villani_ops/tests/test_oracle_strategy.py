from pathlib import Path

from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.state import OpsRunState, SubtaskState
from villani_ops.agentic.state_tooling import OpsToolContext, execute_tool_with_policy
from villani_ops.agentic.tools import (
    OpsDiscoverOracleInput,
    ValidationOracle,
    ValidationStrategy,
    _strategy_to_validation_plan,
    h_discover_oracle,
    make_validation_decision,
)


def _state(tmp_path):
    repo=tmp_path/'repo'; run=tmp_path/'run'; repo.mkdir(); run.mkdir()
    return OpsRunState(run_id='r', run_dir=str(run), repo_path=str(repo), task='t', success_criteria='done', mode='performance', runner='villani-code', candidate_attempts=1)


def _ctx(state):
    return OpsToolContext(run_dir=Path(state.run_dir), recorder=OpsEventRecorder(Path(state.run_dir),'r'), transcript=[], production=False, allow_fake_dependencies=True)


def test_oracle_discovery_creates_task_assessment_without_authority_from_discovery(tmp_path):
    s=_state(tmp_path); s.investigation={'summary':'i'}
    out=h_discover_oracle(s, OpsDiscoverOracleInput(scope='task', reason='before execution'), _ctx(s))
    assert out['oracle_assessment']['scope']=='task'
    assert out['oracle_assessment']['oracle_quality']=='evidence_only'
    assert out['validation_strategy']['strategy_type']=='evidence_based_acceptance'
    assert out['validation_plan']['authoritative_commands']==[]
    assert s.oracle_assessments[0]['missing_oracle_reason'].startswith('No authoritative')


def test_oracle_discovery_creates_subtask_assessment(tmp_path):
    s=_state(tmp_path); s.investigation={'summary':'i'}
    s.subtasks=[SubtaskState(subtask_id='s1', title='S1', objective='do one part')]
    out=h_discover_oracle(s, OpsDiscoverOracleInput(scope='subtask', subtask_id='s1', reason='before subtask'), _ctx(s))
    assert out['oracle_assessment']['scope']=='subtask'
    assert out['oracle_assessment']['subtask_id']=='s1'
    assert s.subtasks[0].oracle_assessment['oracle_quality']=='evidence_only'
    assert s.subtasks[0].validation_strategy['strategy_type']=='evidence_based_acceptance'


def test_authority_flows_from_validation_strategy_only():
    oracle=ValidationOracle(oracle_type='user_command', authority='acceptance_blocking', scope='task', command='python -c "print(1)"', description='explicit user command', rationale='selected by strategy')
    strategy=ValidationStrategy(scope='task', strategy_type='authoritative_validation', authoritative_checks=[oracle], acceptance_rule='must pass', rationale='authoritative oracle selected')
    plan=_strategy_to_validation_plan(strategy)
    assert plan.authoritative_commands[0].authority=='acceptance_blocking'
    assert plan.commands==[]


def test_discovered_command_not_authoritative_unless_strategy_selected(tmp_path):
    s=_state(tmp_path)
    s.investigation={'summary':'i','validation_plan':{'commands':[{'cmd':'python -c "print(1)"','source':'project_detected','confidence':'high'}]}}
    out=h_discover_oracle(s, OpsDiscoverOracleInput(scope='task', reason='discovered command exists'), _ctx(s))
    assert out['oracle_assessment']['oracle_quality']=='evidence_only'
    assert out['validation_plan']['authoritative_commands']==[]


def test_runner_trace_and_diagnostic_failures_remain_non_blocking():
    decision=make_validation_decision({'commands':[{'cmd':'runner debug','status':'failed_candidate','passed':False,'authority':'diagnostic_only','source':'runner_trace','scope':'candidate'}]})
    assert decision['status']=='inconclusive'
    assert decision['acceptance_basis']=='inconclusive'
    assert decision['diagnostic_failures'][0]['source']=='runner_trace'


def test_strong_derived_portfolio_is_evidence_based_not_validated():
    decision=make_validation_decision({'commands':[{'cmd':'property check','passed':True,'status':'passed','authority':'strong_evidence','scope':'candidate'}]})
    assert decision['status']=='inconclusive'
    assert decision['acceptance_basis']=='evidence_based_acceptance'


def test_strict_policy_marks_unverified_final_human_required(tmp_path):
    s=_state(tmp_path); c=_ctx(s); s.oracle_policy='strict'; s.execution_path='parallel_candidates'; s.selection={'decision':'select','selected_attempt_id':'candidate_001','decision_bucket':'accepted_unverified'}
    patch=tmp_path/'run'/'diff.patch'; patch.write_text('diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1 @@\n+x\n')
    from villani_ops.agentic.state import CandidateAttemptState
    s.candidates.append(CandidateAttemptState(attempt_id='candidate_001',status='reviewed',scope='candidate',exit_code=0,patch_path=str(patch),changed_files=['a.txt'],review={'decision':'pass','recommended_action':'accept','score':1,'summary':'ok','evidence':[],'issues':[]},validation={'status':'inconclusive','passed':False,'decision':{'status':'inconclusive','acceptance_basis':'inconclusive'},'commands':[]},validation_source='ops_run_validation',review_status='passed'))
    res=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'accepted','selected_attempt_id':'candidate_001','summary':'done'},'f',c)
    assert not res.is_error
    assert s.final_decision['acceptance_basis']=='human_required'
    assert s.status=='failed'
    assert 'human_review_packet' in s.final_decision
