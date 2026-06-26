import json
from pathlib import Path
from villani_ops.agentic.state import SubtaskState, CandidateAttemptState, OpsRunState
from villani_ops.agentic.tools import build_subtask_runner_prompt, import_villani_code_debug_evidence, assess_scope_compliance, build_agentic_review_payload
from villani_ops.core.acceptance import is_attempt_acceptance_eligible


def test_subtask_prompt_sections_and_scope_rules():
    st=SubtaskState(subtask_id='s1', title='Fix parser', objective='Fix parser bug', success_criteria='Parser test passes', relevant_files=['src/parser.py'])
    prompt=build_subtask_runner_prompt(parent_task='parent', parent_success_criteria='all parent criteria', subtask=st, allowed_files=st.relevant_files, forbidden_files=['.villani','.villani_code'], validation_commands=['python -m pytest tests/test_parser.py -v'], dependency_context=None, merge_contract=None)
    assert 'ONE Villani Ops subtask, not the whole parent task' in prompt
    assert 'Parent success criteria are provided only so you understand the larger system' in prompt
    assert '- src/parser.py' in prompt
    assert 'python -m pytest tests/test_parser.py -v' in prompt
    assert 'SCOPE_EXCEPTION:' in prompt
    assert 'Do not create helper scripts' in prompt
    assert 'SUBTASK_RESULT:' in prompt


def test_import_villani_code_debug_evidence_filters_validation_commands(tmp_path):
    adir=tmp_path/'attempt'; adir.mkdir()
    (adir/'commands.jsonl').write_text(json.dumps({'command':'ls','exit_code':0})+'\n'+json.dumps({'command':'python -m pytest tests/test_x.py -v','exit_code':0,'stdout':'ok'})+'\n')
    a=CandidateAttemptState(attempt_id='a',status='completed',scope='candidate',artifacts_dir=str(adir))
    ev=import_villani_code_debug_evidence(a)
    assert len(ev)==1
    assert ev[0]['cmd']=='python -m pytest tests/test_x.py -v'
    assert ev[0]['passed'] is True
    assert ev[0]['source']=='villani_code_debug_trace'


def test_scope_assessment_subtask_extra_files_require_exception():
    blocked=assess_scope_compliance(scope='subtask', changed_files=['a.py','b.py'], allowed_files=['a.py'], scope_exception_text=None, subtask=None)
    assert not blocked.compliant and 'subtask_scope_overreach' in blocked.blockers
    allowed=assess_scope_compliance(scope='subtask', changed_files=['a.py','b.py'], allowed_files=['a.py'], scope_exception_text='SCOPE_EXCEPTION:\n- Extra files modified: b.py\n- Why each extra file was necessary: shared API\n- Why the change is minimal: one line\n- Why this does not solve unrelated subtasks: no extra behavior', subtask=None)
    assert allowed.compliant and allowed.scope_exception_adequate
    cand=assess_scope_compliance(scope='candidate', changed_files=['a.py','b.py'], allowed_files=['a.py'], scope_exception_text=None, subtask=None)
    assert cand.compliant
    internal=assess_scope_compliance(scope='candidate', changed_files=['.villani/log'], allowed_files=[], scope_exception_text=None, subtask=None)
    assert not internal.compliant


def test_imported_failed_validation_blocks_candidate_acceptance_and_review_payload(tmp_path):
    patch=tmp_path/'diff.patch'; patch.write_text('diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n')
    a=CandidateAttemptState(attempt_id='c1',status='reviewed',scope='candidate',patch_path=str(patch),changed_files=['a.py'],exit_code=0,review={'decision':'pass','recommended_action':'accept','score':1,'summary':'ok','evidence':[],'issues':[],'blockers':[]},validation={'passed':False,'status':'failed','validation_source':'villani_code_debug_trace','commands':[{'cmd':'pytest','passed':False,'status':'failed'}]},validation_results=[{'passed':False,'status':'failed','validation_source':'villani_code_debug_trace','commands':[{'cmd':'pytest','passed':False,'status':'failed'}]}])
    state=OpsRunState(run_id='r',run_dir=str(tmp_path),repo_path=str(tmp_path),task='t',success_criteria='s',mode='agentic',runner='villani-code',candidate_attempts=1,execution_path='parallel_candidates')
    ok, blockers=is_attempt_acceptance_eligible(a,state=state)
    assert not ok and 'validation_failed' in blockers
    payload=build_agentic_review_payload(state,a,'candidate')
    assert payload['imported_debug_validation']
