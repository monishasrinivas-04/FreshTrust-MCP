from rag.freshness import freshness_score
def test_recent_score():
    score,status=freshness_score("2026-11-02T00:00:00+00:00")
    assert score is not None and status in {"recent","moderately_recent","aging","old","very_old"}
def test_unknown():
    assert freshness_score(None)==(None,"unknown")
