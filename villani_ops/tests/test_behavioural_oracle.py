from villani_ops.agentic.state import OpsRunState, CandidateAttemptState, SubtaskState
from villani_ops.agentic.state_tooling import OpsToolContext, execute_tool_with_policy
from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.tools import build_candidate_runner_prompt, build_adaptive_subtask_runner_prompt, build_decomposition_fallback_prompt
from villani_ops.core.acceptance import candidate_ranking_key


def ctx(tmp_path):
    return OpsToolContext(run_dir=tmp_path/'run', recorder=OpsEventRecorder(tmp_path/'run','r'), transcript=[], production=False)


def state(tmp_path):
    run=tmp_path/'run'; repo=tmp_path/'repo'; run.mkdir(); repo.mkdir()
    return OpsRunState(run_id='r', run_dir=str(run), repo_path=str(repo), task='cancel queued work without cancelling cleanup for active work', success_criteria='active cleanup completes; queued work never starts', mode='performance', runner='villani-code', candidate_attempts=2, investigation={'summary':'work queue cancellation risk','risks':['queued task may start after cancellation'],'validation_plan':{'commands':[{'cmd':'custom validation','purpose':'user check','authority':'strong_evidence'}]}}, plan={'strategy':'single_task'}, execution_path='single_task')


def test_behavioural_oracle_derivation_and_probe_materialization(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path)
    res=execute_tool_with_policy(s,'ops_derive_behavioral_oracle',{'scope':'task','reason':'before solving'},'o',c)
    assert not res.is_error
    oracle=res.content['behavioural_oracle']
    assert oracle['requirements'] and oracle['requirements'][0]['priority']=='critical'
    assert oracle['edge_cases'] and oracle['adversarial_review_checklist']
    assert all(p['related_requirement_ids'] for p in oracle['validation_probes'])
    mat=execute_tool_with_policy(s,'ops_materialize_validation_probes',{'scope':'task','reason':'run probes'},'p',c).content
    assert mat['materialized_probes'][0]['stored_outside_solution_patch'] is True
    assert mat['materialized_probes'][0]['authority'] != 'acceptance_blocking'
    assert (tmp_path/'run'/'validation_probes'/'task'/'probes.json').exists()


def test_prompts_include_behavioural_oracle_and_gaps(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path)
    execute_tool_with_policy(s,'ops_derive_behavioral_oracle',{'scope':'task','reason':'before solving'},'o',c)
    prompt=build_candidate_runner_prompt(s, reason='next')
    assert 'BEHAVIOURAL ORACLE SUMMARY' in prompt
    st=SubtaskState(subtask_id='q', title='queue', objective='fix queue cancellation')
    s.subtasks=[st]; s.execution_path='decomposed_subtasks'
    execute_tool_with_policy(s,'ops_derive_behavioral_oracle',{'scope':'subtask','subtask_id':'q','reason':'subtask'},'so',c)
    sub=build_adaptive_subtask_runner_prompt(s, st, reason='sub')
    assert 'BEHAVIOURAL' in sub.upper() or 'queue cancellation' in sub
    s.decomposed_execution_status='blocked'; s.fallback_execution_path='parallel_candidates_after_decomposition_deadlock'
    fb=build_decomposition_fallback_prompt(s, reason='deadlock')
    assert 'BEHAVIOURAL ORACLE GAPS' in fb


def test_candidate_ranking_prioritizes_behavioural_coverage_over_review_score(tmp_path):
    s=state(tmp_path)
    a1=CandidateAttemptState(attempt_id='candidate_001', status='reviewed', scope='candidate', changed_files=['x'], patch_path=__file__, review={'decision':'pass','recommended_action':'accept','score':0.99}, oracle_coverage_score=0.3, critical_requirements_failed=['R1'])
    a2=CandidateAttemptState(attempt_id='candidate_002', status='reviewed', scope='candidate', changed_files=['x'], patch_path=__file__, review={'decision':'pass','recommended_action':'accept','score':0.7}, oracle_coverage_score=1.0)
    assert candidate_ranking_key(a2, state=s) > candidate_ranking_key(a1, state=s)
