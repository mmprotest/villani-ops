from types import SimpleNamespace
import time
import threading

from villani_ops.agentic.state import OpsRunState, CandidateAttemptState
from villani_ops.agentic.tournament import (
    build_tournament_candidate_prompt,
    prompt_is_clean,
    FORBIDDEN_PROMPT_PHRASES,
    CandidateRiskReview,
    PairwiseCandidateComparison,
    rank_candidates,
    summarize_candidate_agreement,
    decide_launch_count,
)
from villani_ops.agentic import tools
from villani_ops.agentic.tools import OpsLaunchCandidatesInput


def _state(tmp_path, attempts=5):
    return OpsRunState(run_id='r', run_dir=str(tmp_path), repo_path=str(tmp_path/'repo'), task='Implement feature X', success_criteria='All criteria pass', mode='performance', runner='villani-code', candidate_attempts=attempts, execution_path='parallel_candidates')


def test_tournament_candidate_prompt_is_clean_pass_through():
    prompt=build_tournament_candidate_prompt('Original task', 'Original success criteria')
    assert 'Original task' in prompt
    assert 'Original success criteria' in prompt
    assert prompt_is_clean(prompt)
    lowered=prompt.lower()
    for phrase in FORBIDDEN_PROMPT_PHRASES:
        assert phrase.lower() not in lowered


def test_candidate_prompt_does_not_include_attempt_review_or_oracle_text():
    prompt=build_tournament_candidate_prompt('Fix bug', 'Done when fixed')
    forbidden=['Candidate 1 failed','previous attempt','oracle','behavioural oracle','review said','hidden test','comparison','try differently from','another candidate']
    assert all(x.lower() not in prompt.lower() for x in forbidden)


def test_parallel_candidate_execution_respects_max_parallel_and_isolated_worktrees(monkeypatch, tmp_path):
    st=_state(tmp_path, attempts=5)
    active=0; max_seen=0; lock=threading.Lock(); worktrees=[]
    class Rec:
        def record(self,*a,**k): pass
    ctx=SimpleNamespace(coding_backend=SimpleNamespace(max_parallel=4), backend=SimpleNamespace(max_parallel=4), max_parallel=4, coding_backend_name='b', backend_name='b', timeout_seconds=None, recorder=Rec())
    def fake_run(state, ctx, aid, scope, task, success, subtask_id=None, backend_name=None, record_events=True):
        nonlocal active,max_seen
        with lock:
            active+=1; max_seen=max(max_seen,active)
        time.sleep(0.03)
        with lock:
            active-=1
        wt=str(tmp_path/'attempts'/aid/'worktree'); worktrees.append(wt)
        return CandidateAttemptState(attempt_id=aid, backend_name='b', status='completed', scope='candidate', worktree_path=wt, artifacts_dir=str(tmp_path/'attempts'/aid), patch_path=str(tmp_path/'attempts'/aid/'diff.patch'), changed_files=[f'{aid}.py'])
    monkeypatch.setattr(tools, '_run_attempt', fake_run)
    out=tools.h_launch_candidates(st, OpsLaunchCandidatesInput(attempts=5, reason='tournament'), ctx)
    assert out['attempts_launched']==5
    assert max_seen==4
    assert len(set(worktrees))==5
    assert st.candidate_concurrency['max_parallel']==4


def test_launch_count_can_be_lower_than_requested_when_budget_tight():
    policy=decide_launch_count(5, 4, timeout_seconds=260, estimated_time_per_candidate=120, review_budget=120, finalization_budget=60)
    assert policy['candidate_attempts_requested']==5
    assert policy['candidate_attempts_launched']==0 or policy['candidate_attempts_launched'] < 5
    assert policy['reason']=='reserved time for review and finalization'


def test_tournament_ranking_prioritizes_pairwise_and_hidden_risk_before_generic_score():
    a=CandidateRiskReview(candidate_id='a', summary='a', likely_correct=True, confidence=.7, minimality_score=.8, correctness_score=.8, hidden_test_risk_score=.1, recommendation='accept', rationale='')
    b=CandidateRiskReview(candidate_id='b', summary='b', likely_correct=True, confidence=.7, minimality_score=.8, correctness_score=.9, hidden_test_risk_score=.6, recommendation='accept', rationale='')
    cmp=PairwiseCandidateComparison(candidate_a='a', candidate_b='b', winner='candidate_a', confidence=.7, rationale='a handles edge cases')
    ranking=rank_candidates([a,b],[cmp],validation={'a':'not_run','b':'not_run'},generic_scores={'b':1.0,'a':0.0})
    assert ranking.selected_candidate_id=='a'
    assert ranking.unresolved_risks


def test_authoritative_validation_dominates_when_available():
    a=CandidateRiskReview(candidate_id='a', summary='a', likely_correct=True, confidence=.7, minimality_score=.8, correctness_score=.8, hidden_test_risk_score=.1, recommendation='accept', rationale='')
    b=CandidateRiskReview(candidate_id='b', summary='b', likely_correct=True, confidence=.7, minimality_score=.8, correctness_score=.7, hidden_test_risk_score=.2, recommendation='accept', rationale='')
    ranking=rank_candidates([a,b],[],validation={'b':'passed','a':'not_run'})
    assert ranking.selected_candidate_id=='b'
    assert not ranking.unresolved_risks


def test_candidate_agreement_summary_is_produced():
    cs=[CandidateAttemptState(attempt_id='c1',status='completed',scope='candidate',changed_files=['a.py']), CandidateAttemptState(attempt_id='c2',status='completed',scope='candidate',changed_files=['a.py'])]
    s=summarize_candidate_agreement(cs)
    assert s['same_patch'] is True
    assert s['consensus_strength']==1.0
