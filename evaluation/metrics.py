"""Evaluation metrics and privacy-conscious run telemetry for FreshTrust-MCP.

Labelled retrieval metrics (Precision@K, Recall@K, MRR) are calculated only
when the user supplies expected relevant repository paths. Without labels,
semantic similarity and citation checks are reported as proxies, not accuracy.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_LOG_DIR = _ROOT / "logs"
_RUN_LOG = _LOG_DIR / "evaluation_runs.jsonl"
_REVIEW_LOG = _LOG_DIR / "human_reviews.jsonl"
_LOCK = threading.Lock()

_SOURCE_RE = re.compile(
    r"\[SOURCE:\s*([^:\]\n]+):(\d+)(?:\s*-\s*(\d+))?\s*\]",
    re.IGNORECASE,
)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path, limit: int = 1000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


def _normal_path(value: str) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("./").lower()


def _parse_expected_paths(value: str | list[str] | None) -> set[str]:
    if isinstance(value, str):
        items = re.split(r"[,;\n]", value)
    else:
        items = value or []
    return {_normal_path(item) for item in items if _normal_path(item)}


def _percent(numerator: float, denominator: float) -> float | None:
    return round(100.0 * numerator / denominator, 2) if denominator else None


def calculate_run_metrics(
    *,
    answer: str,
    provenance: list[list[Any]],
    freshness: list[list[Any]],
    latency: dict[str, Any],
    expected_relevant_paths: str | list[str] | None = None,
    k: int = 5,
) -> dict[str, Any]:
    """Calculate per-run metrics from actual retrieval and pipeline outputs."""
    k = max(1, int(k))
    retrieved_paths = [_normal_path(row[0]) for row in provenance if row]
    retrieved_paths = [path for path in retrieved_paths if path]
    similarities = [
        float(row[2]) for row in provenance
        if len(row) > 2 and isinstance(row[2], (int, float))
    ]

    expected = _parse_expected_paths(expected_relevant_paths)
    labelled = bool(expected)
    relevant_flags = [path in expected for path in retrieved_paths[:k]]

    retrieval: dict[str, Any] = {
        "retrieved_chunks": len(retrieved_paths),
        "mean_similarity_proxy": (
            round(sum(similarities) / len(similarities), 4) if similarities else None
        ),
        "top1_similarity_proxy": round(similarities[0], 4) if similarities else None,
        "labelled_evaluation": labelled,
        "precision_at_5": None,
        "recall_at_5": None,
        "mrr": None,
    }
    if labelled:
        # Precision is chunk-ranked at K; recall is file-level so repeated
        # chunks from one relevant file cannot inflate recall above 1.0.
        hits = sum(relevant_flags)
        relevant_retrieved_files = {
            path for path in retrieved_paths[:k] if path in expected
        }
        retrieval["precision_at_5"] = round(hits / k, 4)
        retrieval["recall_at_5"] = round(
            len(relevant_retrieved_files) / len(expected), 4
        )
        first_rank = next(
            (index for index, is_relevant in enumerate(relevant_flags, start=1) if is_relevant),
            None,
        )
        retrieval["mrr"] = round(1.0 / first_rank, 4) if first_rank else 0.0
        retrieval["relevant_expected_files"] = len(expected)
        retrieval["relevant_retrieved_files"] = len(relevant_retrieved_files)

    known_freshness = [
        float(row[2]) for row in freshness
        if len(row) > 2 and isinstance(row[2], (int, float))
    ]
    freshness_count = len(freshness)
    recent_count = sum(
        1 for row in freshness
        if len(row) > 3 and str(row[3]).lower() == "recent"
    )
    freshness_metrics = {
        "mean_freshness_score": (
            round(sum(known_freshness) / len(known_freshness), 2)
            if known_freshness else None
        ),
        "freshness_coverage_pct": _percent(len(known_freshness), freshness_count),
        "recent_sources_pct": _percent(recent_count, freshness_count),
        "sources_with_known_timestamp": len(known_freshness),
        "sources_evaluated": freshness_count,
    }

    # Citation checks are deterministic provenance checks, not an LLM quality score.
    citations = _SOURCE_RE.findall(answer or "")
    valid_citations = 0
    retrieved_ranges: dict[str, list[tuple[int, int]]] = {}
    for row in provenance:
        if len(row) >= 2:
            match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(row[1]))
            if match:
                retrieved_ranges.setdefault(_normal_path(row[0]), []).append(
                    (int(match.group(1)), int(match.group(2)))
                )

    cited_files = set()
    for raw_path, raw_start, raw_end in citations:
        path = _normal_path(raw_path)
        start = int(raw_start)
        end = int(raw_end) if raw_end else start
        ranges = retrieved_ranges.get(path, [])
        if any(start <= retrieved_end and end >= retrieved_start
               for retrieved_start, retrieved_end in ranges):
            valid_citations += 1
            cited_files.add(path)

    response_metrics = {
        "citation_count": len(citations),
        "valid_citation_count": valid_citations,
        "citation_provenance_precision_pct": _percent(valid_citations, len(citations)),
        "retrieved_source_citation_coverage_pct": _percent(
            len(cited_files), len(set(retrieved_paths))
        ),
        "citation_check": (
            "No source citations found" if not citations
            else "All citations match retrieved provenance" if valid_citations == len(citations)
            else "Some citations do not match retrieved provenance"
        ),
        "quality_score": None,
        "quality_score_note": (
            "Requires human review or a labelled answer benchmark; not inferred "
            "from citation presence alone."
        ),
    }

    return {
        "retrieval": retrieval,
        "freshness": freshness_metrics,
        "response": response_metrics,
        "latency": {
            key: round(float(value), 4)
            for key, value in latency.items()
            if isinstance(value, (int, float))
        },
    }


def record_execution(
    *,
    repo_key: str,
    success: bool,
    elapsed_seconds: float,
    metrics: dict[str, Any] | None = None,
    retrieval_mode: str | None = None,
    error_type: str | None = None,
) -> None:
    """Persist operational data only; never store tokens, prompts, or repository text."""
    latency = (metrics or {}).get("latency", {})
    _append_jsonl(_RUN_LOG, {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "repo_hash": hashlib.sha256(repo_key.encode()).hexdigest()[:16],
        "success": bool(success),
        "elapsed_seconds": round(float(elapsed_seconds), 4),
        "retrieval_mode": retrieval_mode,
        "error_type": error_type,
        "latency": latency,
    })


def record_human_review(
    *,
    repo_key: str,
    quality_rating: int,
    grounded: bool,
    complete: bool,
) -> None:
    """Store an explicit user review for response-quality evaluation."""
    rating = max(1, min(5, int(quality_rating)))
    _append_jsonl(_REVIEW_LOG, {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "repo_hash": hashlib.sha256(repo_key.encode()).hexdigest()[:16],
        "quality_rating": rating,
        "grounded": bool(grounded),
        "complete": bool(complete),
    })


def dashboard_summary() -> dict[str, Any]:
    """Summarize persisted runs and explicit human reviews."""
    runs = _read_jsonl(_RUN_LOG)
    reviews = _read_jsonl(_REVIEW_LOG)
    total = len(runs)
    successes = sum(bool(row.get("success")) for row in runs)
    durations = [
        float(row["elapsed_seconds"]) for row in runs
        if isinstance(row.get("elapsed_seconds"), (int, float))
    ]
    sorted_durations = sorted(durations)

    def percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        index = max(0, min(len(values) - 1, math.ceil(p * len(values)) - 1))
        return round(values[index], 3)

    summary = {
        "runs_recorded": total,
        "success_rate_pct": _percent(successes, total),
        "failure_count": total - successes,
        "median_end_to_end_seconds": round(median(durations), 3) if durations else None,
        "p95_end_to_end_seconds": percentile(sorted_durations, 0.95),
        "human_reviews": len(reviews),
        "mean_human_quality_rating": (
            round(sum(int(r["quality_rating"]) for r in reviews) / len(reviews), 2)
            if reviews else None
        ),
        "human_grounded_pct": _percent(
            sum(bool(r.get("grounded")) for r in reviews), len(reviews)
        ),
        "human_complete_pct": _percent(
            sum(bool(r.get("complete")) for r in reviews), len(reviews)
        ),
    }
    return summary
