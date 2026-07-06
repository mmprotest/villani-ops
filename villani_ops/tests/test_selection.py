from types import SimpleNamespace
from pathlib import Path
from villani_ops.orchestrator.selection import select_winner, build_llm_comparison_packet


def test_accept_recommended_success_beats_inspect_success():
    s=select_winner([
        {'candidateId':'inspect','result':1,'confidence':1,'recommendedAction':'inspect_manually'},
        {'candidateId':'accept','result':1,'confidence':0,'recommendedAction':'accept'},
    ], seed=1)
    assert s.winnerCandidateId == 'accept'


def test_no_accept_falls_back_to_quality_logic():
    s=select_winner([
        {'candidateId':'weak','result':1,'recommendedAction':'inspect_manually','failureEvidence':['x']},
        {'candidateId':'strong','result':1,'recommendedAction':'inspect_manually','successEvidence':['runtime validation passed']},
    ], seed=1)
    assert s.winnerCandidateId == 'strong'


def test_rejected_candidate_does_not_beat_success():
    s=select_winner([
        {'candidateId':'rejected','result':0,'recommendedAction':'accept','confidence':1},
        {'candidateId':'success','result':1,'recommendedAction':'inspect_manually','confidence':0},
    ], seed=1)
    assert s.winnerCandidateId == 'success'


def test_accept_tie_is_seed_deterministic():
    cs=[{'candidateId':f'c{i}','result':1,'recommendedAction':'accept'} for i in range(4)]
    assert select_winner(cs, 99).winnerCandidateId == select_winner(cs, 99).winnerCandidateId


def test_comparison_packet_includes_and_truncates(tmp_path):
    patch=tmp_path/'diff.patch'; patch.write_text('diff --git a/x b/x\n' + 'A'*5000)
    c=SimpleNamespace(candidate_id='candidate-001', patch_path=patch, changed_files=['x'], verifier_result={'result':1,'confidence':.8,'recommendedAction':'accept','reason':'summary','successEvidence':['B'*1000]})
    packet=build_llm_comparison_packet([c], diff_limit=100, evidence_limit=50)
    row=packet[0]
    assert row['candidateId']=='candidate-001'
    assert row['verifier']['reason']=='summary'
    assert row['changedFiles']==['x']
    assert 'diff --git' in row['diffExcerpt'] and 'truncated' in row['diffExcerpt']
    assert 'truncated' in row['verifier']['successEvidence'][0]
