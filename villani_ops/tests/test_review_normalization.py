from villani_ops.review.reviewer import ReviewResult, normalize_review_payload


def test_real_reviewer_payload_normalizes_and_validates():
    raw={"passed":True,"score":10,"decision":"accept","summary":"The patch correctly implements the required pricing logic.","evidence":"Runner exit code: 0. Stdout confirms tests pass.","issues":[],"recommended_action":"Accept attempt","confidence":0.95,"requires_human_approval":False}
    review=ReviewResult.model_validate(normalize_review_payload(raw))
    assert review.decision == "pass"
    assert review.recommended_action == "accept"
    assert review.evidence == ["Runner exit code: 0. Stdout confirms tests pass."]
    assert review.confidence == 0.95
    assert review.score == 1.0
