from pathlib import Path
from villani_ops.agentic.state import CandidateAttemptState
from villani_ops.agentic.state_tooling import execute_tool_with_policy
from villani_ops.agentic.recovery import recommend_next_agentic_action
from villani_ops.tests.test_agentic_tools import state, ctx


def _eligible_attempt(tmp_path, s, aid='candidate_001'):
    wt=tmp_path/aid/'worktree'; wt.mkdir(parents=True)
    patch=tmp_path/aid/'diff.patch'; patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text('diff --git a/a.py b/a.py\nindex e69de29..2e65efe 100644\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x\n')
    a=CandidateAttemptState(attempt_id=aid,status='completed',scope='candidate',worktree_path=str(wt),patch_path=str(patch),changed_files=['a.py'],exit_code=0,review={'decision':'pass','recommended_action':'accept','score':1,'summary':'ok','evidence':['ok'],'issues':[]})
    s.candidates.append(a); s.investigation={'summary':'s','confidence':1}; s.plan={'strategy':'parallel_candidates'}; s.execution_path='parallel_candidates'; s.phase='selecting'; return a, wt


def test_candidate_validation_defaults_to_worktree_and_attaches(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); a, wt=_eligible_attempt(tmp_path,s)
    res=execute_tool_with_policy(s,'ops_run_validation',{'target':'candidate','target_id':a.attempt_id,'commands':[{'cmd':'python -c "import pathlib; pathlib.Path(\'marker.txt\').write_text(\'ok\')"'}]},'v',c)
    assert not res.is_error
    assert (wt/'marker.txt').read_text()=='ok'
    assert a.validation_status=='passed'
    assert a.validation and a.validation['passed'] is True
    assert a.acceptance_eligible is True


def test_candidate_validation_rejects_cwd_escape_and_embedded_cd(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); a, _=_eligible_attempt(tmp_path,s)
    out=execute_tool_with_policy(s,'ops_run_validation',{'target':'candidate','target_id':a.attempt_id,'commands':[{'cmd':'python -c "print(1)"','cwd':str(tmp_path)}]},'v',c)
    assert not out.is_error
    assert out.content['status']=='infrastructure_error'
    assert 'validation_command_rejected' not in a.acceptance_blockers
    assert 'validation_failed' not in a.acceptance_blockers
    out=execute_tool_with_policy(s,'ops_run_validation',{'target':'candidate','target_id':a.attempt_id,'commands':[{'cmd':'cd / && python -c "print(1)"'}]},'v2',c)
    assert out.content['status']=='infrastructure_error'
    assert 'remove embedded cd' in out.content['commands'][0]['error']


def test_repo_validation_uses_repo_path_not_candidate_evidence(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir(); s=state(tmp_path); s.repo_path=str(repo); c=ctx(tmp_path); a,_=_eligible_attempt(tmp_path,s)
    res=execute_tool_with_policy(s,'ops_run_validation',{'target':'repo','commands':[{'cmd':'python -c "import pathlib; pathlib.Path(\'repo_marker.txt\').write_text(\'ok\')"'}]},'v',c)
    assert not res.is_error
    assert (repo/'repo_marker.txt').exists()
    assert a.validation is None
    assert s.repo_validation_results


def test_recovery_recommends_selection_then_finalization(tmp_path):
    s=state(tmp_path); a,_=_eligible_attempt(tmp_path,s)
    a.validation={'passed':True,'status':'passed','commands':[{'cmd':'python -c "print(1)"','passed':True,'status':'passed'}]}
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name=='ops_select_winner' and rec.can_execute_deterministically
    s.selection={'decision':'select','selected_attempt_id':a.attempt_id}
    rec=recommend_next_agentic_action(s)
    assert rec.tool_name=='ops_finalize_run' and rec.can_execute_deterministically


def test_candidate_ids_sequential_with_parallel_batches(tmp_path):
    s=state(tmp_path); c=ctx(tmp_path); c.max_parallel=2
    execute_tool_with_policy(s,'ops_submit_investigation',{'summary':'s','confidence':1},'i',c)
    execute_tool_with_policy(s,'ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':3,'expected_difficulty':'easy','confidence':1},'p',c)
    execute_tool_with_policy(s,'ops_select_execution_path',{'path':'parallel_candidates','reason':'r'},'x',c)
    res=execute_tool_with_policy(s,'ops_launch_candidates',{'attempts':3,'reason':'r'},'l',c)
    assert not res.is_error
    assert [a.attempt_id for a in s.candidates]==['candidate_001','candidate_002','candidate_003']

def test_windows_mode_rejects_unix_head(monkeypatch, tmp_path):
    import villani_ops.agentic.tools as tools
    s=state(tmp_path); c=ctx(tmp_path); a,_=_eligible_attempt(tmp_path,s)
    monkeypatch.setattr(tools, '_validation_platform_is_windows', lambda: True)
    res=execute_tool_with_policy(s,'ops_run_validation',{'target':'candidate','target_id':a.attempt_id,'commands':[{'cmd':'python -m pytest 2>&1 | head -300'}]},'v',c)
    assert not res.is_error
    assert res.content['status']=='infrastructure_error'
    assert res.content['commands'][0]['reason']=='platform_unsupported_command'
    assert 'validation_command_rejected' not in a.acceptance_blockers
    assert 'validation_failed' not in a.acceptance_blockers


def test_no_tool_call_with_eligible_candidate_auto_selects_and_finalizes(tmp_path):
    from villani_ops.agentic.runner import OpsRunner
    from villani_ops.tests.test_agentic_orchestrator import FakeClient, req, tc
    blocks=[
        tc('ops_submit_investigation',{'summary':'s','confidence':1.0}),
        tc('ops_submit_plan',{'summary':'p','strategy':'parallel_candidates','should_decompose':False,'candidate_attempts':1,'expected_difficulty':'easy','confidence':1.0}),
        tc('ops_select_execution_path',{'path':'parallel_candidates','reason':'r'}),
        tc('ops_launch_candidates',{'attempts':1,'reason':'r'}),
        tc('ops_review_attempt',{'attempt_id':'candidate_001','scope':'candidate'}),
        tc('ops_run_validation',{'target':'candidate','target_id':'candidate_001','commands':[{'cmd':'python -c "print(1)"'}]}),
        [],
        [],
    ]
    r=OpsRunner(client=FakeClient(blocks)).run(req(tmp_path,candidate_attempts=1))
    assert r.state.status=='completed'
    assert r.state.selection['selected_attempt_id']=='candidate_001'
