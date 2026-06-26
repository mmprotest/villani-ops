import json
from pathlib import Path

import pytest

from villani_ops.core.durable_io import durable_write_text, durable_write_json
from villani_ops.agentic.event_recorder import OpsEventRecorder
from villani_ops.agentic.recovery import recommend_next_agentic_action
from villani_ops.agentic.state import OpsRunState, CandidateAttemptState
from villani_ops.agentic.state_tooling import execute_tool_with_policy
from villani_ops.tests.test_agentic_tools import ctx


def _eligible_state(tmp_path):
    patch=tmp_path/'p.diff'; patch.write_text('diff --git a/a.txt b/a.txt\n')
    s=OpsRunState(run_id='r',run_dir=str(tmp_path),repo_path=str(tmp_path),task='t',mode='performance',runner='villani-code',candidate_attempts=1,execution_path='parallel_candidates',phase='selecting',investigation={'summary':'i'},plan={'strategy':'parallel_candidates'})
    s.candidates.append(CandidateAttemptState(attempt_id='candidate_001',status='reviewed',scope='candidate',patch_path=str(patch),changed_files=['a.txt'],exit_code=0,validation={'passed':True,'status':'passed'},validation_status='passed',review={'decision':'pass','recommended_action':'accept','blockers':[]}))
    s.selection={'decision':'select','selected_attempt_id':'candidate_001','summary':'ok','confidence':1.0}
    return s


def test_recovery_finalizes_valid_selected_winner(tmp_path):
    s=_eligible_state(tmp_path)
    rec=recommend_next_agentic_action(s)
    assert rec.action=='finalize_selected_winner'
    assert rec.tool_name=='ops_finalize_run'
    assert rec.tool_input['decision']=='accepted'


def test_repeated_select_and_finalize_are_idempotent(tmp_path):
    s=_eligible_state(tmp_path); c=ctx(tmp_path)
    select=execute_tool_with_policy(s,'ops_select_winner',{'decision':'select','selected_attempt_id':'candidate_001','summary':'ok','confidence':1.0},'sel',c)
    assert select.content['already_selected'] is True
    fin=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'accepted','summary':'ok','selected_attempt_id':'candidate_001'},'fin',c)
    assert fin.content['decision']=='accepted'
    again=execute_tool_with_policy(s,'ops_finalize_run',{'decision':'accepted','summary':'ok','selected_attempt_id':'candidate_001'},'fin2',c)
    assert again.content['already_finalized'] is True


def test_recorder_suppresses_duplicate_terminal_events(tmp_path):
    r=OpsEventRecorder(tmp_path,'r')
    r.record('selection_completed', payload={'decision':'select','selected_attempt_id':'a'})
    r.record('selection_completed', payload={'decision':'select','selected_attempt_id':'a'})
    r.record('run_finalized', payload={'decision':'accepted','selected_attempt_id':'a'})
    r.record('run_finalized', payload={'decision':'accepted','selected_attempt_id':'a'})
    types=[e['type'] for e in r.events()]
    assert types.count('selection_completed')==1
    assert types.count('run_finalized')==1


def test_durable_write_preserves_prior_target_after_replace_failure(tmp_path, monkeypatch):
    target=tmp_path/'state.json'; target.write_text('{"ok": true}', encoding='utf-8')
    def fail_replace(src, dst):
        raise PermissionError('busy')
    monkeypatch.setattr('villani_ops.core.durable_io.os.replace', fail_replace)
    with pytest.raises(PermissionError):
        durable_write_text(target, '{"ok": false}', attempts=2, initial_delay_seconds=0)
    assert target.read_text(encoding='utf-8')=='{"ok": true}'
    assert not list(tmp_path.glob('.*.tmp'))


def test_durable_json_retries_transient_replace(tmp_path, monkeypatch):
    target=tmp_path/'usage.json'; calls=[]
    real_replace=__import__('os').replace
    def flaky(src, dst):
        calls.append(src)
        if len(calls)==1:
            raise PermissionError('busy')
        return real_replace(src,dst)
    monkeypatch.setattr('villani_ops.core.durable_io.os.replace', flaky)
    durable_write_json(target, {'ok': True}, attempts=3, initial_delay_seconds=0)
    assert json.loads(target.read_text()) == {'ok': True}
    assert len(set(calls)) == len(calls)
