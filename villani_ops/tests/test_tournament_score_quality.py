from types import SimpleNamespace


def _review(cid, correctness=0.5, risk=0.5, confidence=0.6, quality='model_full'):
    from villani_ops.agentic.state import CandidateRiskReview
    return CandidateRiskReview(
        candidate_id=cid, summary='summary', changed_files=['a.py'], likely_correct=True,
        confidence=confidence, implementation_strategy='strategy', evidence_used=[], evidence_gaps=[],
        strengths=[], risks=[], likely_hidden_failures=[], edge_cases_considered=[], edge_cases_missed=[],
        minimality_score=0.5, correctness_score=correctness, hidden_test_risk_score=risk,
        review_quality=quality, recommendation='uncertain', rationale='rationale'
    )


def test_normalize_score_rules():
    from villani_ops.agentic.state import normalize_score
    assert normalize_score(0.85) == 0.85
    assert normalize_score(8.5) == 0.85
    assert normalize_score(3.5) == 0.35
    assert normalize_score(15) == 1.0
    assert normalize_score(-2) == 0.0
    assert normalize_score(None, default=0.42) == 0.42
    assert normalize_score(float('nan'), default=0.42) == 0.42
    assert normalize_score('bad', default=0.42) == 0.42


def test_tournament_models_normalize_scores_on_construction():
    from villani_ops.agentic.state import PairwiseCandidateComparison, TournamentRanking, RankedCandidate
    r = _review('candidate_001', correctness=8.5, risk=3.5, confidence=7)
    assert r.correctness_score == 0.85
    assert r.hidden_test_risk_score == 0.35
    assert r.confidence == 0.7
    cmp = PairwiseCandidateComparison(candidate_a='a', candidate_b='b', winner='tie', confidence=7, rationale='r')
    assert cmp.confidence == 0.7
    ranking = TournamentRanking(ranked_candidates=[RankedCandidate(candidate_id='a', rank=1, correctness_score=8.5, hidden_test_risk_score=3.5, pairwise_wins=0, pairwise_losses=0, materiality_notes='m')], selected_candidate_id='a', selection_confidence=6.5, rationale='r')
    assert ranking.selection_confidence == 0.65
    assert ranking.ranked_candidates[0].correctness_score == 0.85


def test_unproven_critical_behaviour_caps_review_scores():
    r = _review('candidate_001', correctness=0.95, risk=0.1)
    data = r.model_dump(mode='json')
    data['evidence_gaps'] = ['critical required behaviour is unproven and not guaranteed']
    data['recommendation'] = 'accept'
    from villani_ops.agentic.state import CandidateRiskReview
    capped = CandidateRiskReview.model_validate(data)
    assert capped.correctness_score == 0.65
    assert capped.hidden_test_risk_score == 0.65
    assert capped.recommendation == 'uncertain'


def test_ranking_treats_8_5_and_0_85_equivalently_after_normalization(tmp_path):
    from villani_ops.agentic.tools import _rank_tournament
    from villani_ops.agentic.state import PairwiseCandidateComparison
    s = SimpleNamespace(candidate_risk_reviews={}, pairwise_comparisons=[], candidate_evidence_packets={}, candidate_summaries={}, candidates=[], selection_basis=None)
    s.candidate_risk_reviews = {'candidate_001': _review('candidate_001', 8.5, 3.5), 'candidate_002': _review('candidate_002', 0.85, 0.35)}
    s.pairwise_comparisons = [PairwiseCandidateComparison(candidate_a='candidate_001', candidate_b='candidate_002', winner='tie', confidence=.4, comparison_quality='deterministic_fallback', rationale='tie')]
    rank = _rank_tournament(s)
    assert rank.ranked_candidates[0].correctness_score == rank.ranked_candidates[1].correctness_score == 0.85
    assert rank.ranked_candidates[0].hidden_test_risk_score == rank.ranked_candidates[1].hidden_test_risk_score == 0.35
    assert rank.selection_confidence <= 0.5


def test_pairwise_fallback_ties_close_normalized_scores_and_blocks_scale_domination():
    from villani_ops.agentic.tools import _compare_pair
    a = _review('candidate_001', correctness=8.5, risk=3.5, quality='model_full')
    b = _review('candidate_002', correctness=0.85, risk=0.35, quality='model_full')
    cmp = _compare_pair(a, b)
    assert cmp.winner == 'tie'
    assert cmp.confidence <= 0.5


def test_candidate_review_prompt_contains_adversarial_quality_guidance(tmp_path):
    from villani_ops.agentic.tools import build_candidate_review_prompt
    from villani_ops.agentic.state import CandidateEvidencePacket
    state = SimpleNamespace(task='do task', success_criteria='must work')
    packet = CandidateEvidencePacket(candidate_id='c', patch_summary='p', runner_status='completed', evidence_quality='low')
    prompt = build_candidate_review_prompt(state, packet)
    assert 'Do not use 0-10 scoring' in prompt
    assert 'Do not reward a candidate merely because it contains words or structures' in prompt
    assert 'actually guarantees the required behaviour' in prompt
    assert 'cleanup, concurrency, resource lifecycle' in prompt
    assert 'Runner-executed checks are evidence' in prompt


def test_pairwise_prompt_is_compact_and_includes_signature_not_full_state(tmp_path):
    from villani_ops.agentic.tools import build_pairwise_comparison_prompt
    from villani_ops.agentic.state import CandidateEvidencePacket, CandidateImplementationSignature
    state = SimpleNamespace(task='task', success_criteria='criteria')
    sig = CandidateImplementationSignature(candidate_id='a', changed_files=['x'], normalized_patch_hash='h', patch_fingerprint='fp', strategy_summary='summary')
    a = CandidateEvidencePacket(candidate_id='a', patch_summary='pa', patch_diff_excerpt='+'*5000, implementation_signature=sig, runner_status='completed', evidence_quality='medium')
    b = CandidateEvidencePacket(candidate_id='b', patch_summary='pb', patch_diff_excerpt='-'*5000, implementation_signature=sig, runner_status='completed', evidence_quality='medium')
    prompt = build_pairwise_comparison_prompt(state, a, b)
    assert len(prompt) <= 15000
    assert 'implementation_signature' in prompt
    assert 'full state JSON' in prompt
