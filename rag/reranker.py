"""Combine reciprocal-rank-fusion relevance with commit freshness."""
from config.settings import settings


def combine_scores(chunks, relevance_scores):
    values = [float(x) for x in relevance_scores]
    maximum = max(values, default=0.0)
    minimum = min(values, default=0.0)
    span = maximum - minimum

    ranked = []
    relevance_weight = float(getattr(settings, "semantic_weight", 0.75))
    freshness_weight = float(getattr(settings, "freshness_weight", 0.25))
    total_weight = relevance_weight + freshness_weight
    if total_weight <= 0:
        relevance_weight, freshness_weight, total_weight = 1.0, 0.0, 1.0
    relevance_weight /= total_weight
    freshness_weight /= total_weight

    for chunk, raw_relevance in zip(chunks, values):
        relevance = (
            (raw_relevance - minimum) / span if span > 0
            else (1.0 if raw_relevance > 0 else 0.0)
        )
        fresh = chunk.get("freshness_score")
        if fresh is None:
            combined = relevance
        else:
            combined = relevance_weight * relevance + freshness_weight * (
                max(0.0, min(100.0, float(fresh))) / 100.0
            )
        item = dict(chunk)
        item["relevance_score"] = float(relevance)
        item["rrf_score"] = float(raw_relevance)
        item["combined_score"] = float(combined)
        ranked.append(item)
    return sorted(ranked, key=lambda item: item["combined_score"], reverse=True)
