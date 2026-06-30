from types import SimpleNamespace


def _review(cid='candidate_001', **kw):
    from villani_ops.agentic.state import CandidateRiskReview
    data=dict(candidate_id=cid, summary='s', changed_files=['x'], likely_correct=True, confidence=kw.pop('confidence',0.9), implementation_strategy='s', evidence_used=kw.pop('evidence_used',[]), evidence_gaps=kw.pop('evidence_gaps',[]), strengths=kw.pop('strengths',[]), risks=kw.pop('risks',[]), likely_hidden_failures=kw.pop('likely_hidden_failures',[]), edge_cases_considered=[], edge_cases_missed=kw.pop('edge_cases_missed',[]), debug_artifact_findings=[], command_findings=[], patch_findings=[], minimality_score=0.8, correctness_score=kw.pop('correctness_score',0.95), hidden_test_risk_score=kw.pop('hidden_test_risk_score',0.1), review_quality=kw.pop('review_quality','model_full'), recommendation=kw.pop('recommendation','strong_accept'), rationale=kw.pop('rationale','ok'))
    data.update(kw)
    return CandidateRiskReview(**data)


def test_detects_generic_critical_evidence_gap_phrases():
    from villani_ops.agentic.tools import detect_critical_evidence_gaps
    phrases=['no test exercises critical path','not verified cleanup','no evidence rollback happens','cannot determine recovery behaviour','error path not exercised']
    for phrase in phrases:
        assert detect_critical_evidence_gaps(_review(evidence_gaps=[phrase])), phrase


def test_detector_requires_unverified_and_critical_and_honors_authoritative_validation():
    from villani_ops.agentic.tools import detect_critical_evidence_gaps
    assert not detect_critical_evidence_gaps(_review(evidence_gaps=['no test']))
    assert not detect_critical_evidence_gaps(_review(evidence_gaps=['cleanup']))
    assert not detect_critical_evidence_gaps(_review(evidence_gaps=['not verified cleanup'], strengths=['authoritative validation passed']))


def test_review_penalty_caps_scores_and_downgrades():
    from villani_ops.agentic.tools import apply_review_risk_penalties
    rv=apply_review_risk_penalties(_review(evidence_gaps=['no evidence rollback happens'], correctness_score=9.5, hidden_test_risk_score=1, confidence=9, recommendation='strong_accept'))
    assert rv.correctness_score == 0.65
    assert rv.hidden_test_risk_score >= 0.65
    assert rv.confidence == 0.65
    assert rv.recommendation != 'strong_accept'
    assert rv.risk_penalty_applied
    assert rv.original_scores


def test_pairwise_draft_conversion_and_fallback_gap_caps():
    from villani_ops.agentic.state import PairwiseComparisonDraft
    from villani_ops.agentic.tools import _pairwise_from_draft, _compare_pair, apply_review_risk_penalties
    draft=PairwiseComparisonDraft(candidate_a='a', candidate_b='b', material_differences=['hash differs'], winner='candidate_a', confidence=8, rationale='r', extra='ignored')
    cmp=_pairwise_from_draft(draft,'model_compact','a','b')
    assert cmp.comparison_quality == 'model_compact'
    assert cmp.confidence == 0.8
    a=apply_review_risk_penalties(_review('a', evidence_gaps=['not verified cleanup'], correctness_score=.95, hidden_test_risk_score=.1))
    b=_review('b', correctness_score=.7, hidden_test_risk_score=.4)
    fb=_compare_pair(a,b)
    assert not (fb.winner == 'candidate_a' and fb.confidence > 0.55)
    assert any('critical evidence gap' in x for x in fb.material_differences)


def test_ranking_downgrades_fallback_selected_with_critical_gap(tmp_path):
    from villani_ops.agentic.tools import _rank_tournament, apply_review_risk_penalties
    from villani_ops.agentic.state import PairwiseCandidateComparison
    s=SimpleNamespace(candidate_risk_reviews={}, pairwise_comparisons=[], candidate_evidence_packets={}, candidate_summaries={}, candidates=[], selection_basis=None)
    s.candidate_risk_reviews={'a':apply_review_risk_penalties(_review('a', evidence_gaps=['no test exercises error path'])),'b':_review('b', correctness_score=.5, hidden_test_risk_score=.5)}
    s.pairwise_comparisons=[PairwiseCandidateComparison(candidate_a='a', candidate_b='b', winner='candidate_a', confidence=.9, comparison_quality='deterministic_fallback', fallback_reason='model_pairwise_unavailable', rationale='r')]
    rank=_rank_tournament(s)
    assert s.selection_basis == 'best_effort_tournament_selection'
    assert rank.selection_confidence <= .45
    assert any('critical evidence gaps' in r for r in rank.unresolved_risks)
