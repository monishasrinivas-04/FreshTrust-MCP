"""PostgreSQL-backed evaluation metrics and privacy-conscious telemetry."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    desc,
    select,
)

from config.settings import settings


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def _database_url() -> str:
    if settings.database_url:
        url = settings.database_url.strip()

        if url.startswith("postgres://"):
            return url.replace(
                "postgres://", "postgresql+psycopg://", 1
            )

        if url.startswith("postgresql://"):
            return url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )

        return url

    # Local development fallback.
    root = Path(__file__).resolve().parents[1]
    local_db = root / "data" / "evaluation.sqlite3"
    local_db.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{local_db.as_posix()}"


engine = create_engine(
    _database_url(),
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args=(
        {"check_same_thread": False}
        if _database_url().startswith("sqlite:")
        else {}
    ),
)

metadata = MetaData()

evaluation_runs = Table(
    "evaluation_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp_utc", DateTime(timezone=True), nullable=False),
    Column("repo_hash", String(16), nullable=False),
    Column("success", Boolean, nullable=False),
    Column("elapsed_seconds", Float, nullable=False),
    Column("retrieval_mode", String(100)),
    Column("error_type", String(150)),
    Column("latency", JSON, nullable=False),
)

human_reviews = Table(
    "human_reviews",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp_utc", DateTime(timezone=True), nullable=False),
    Column("repo_hash", String(16), nullable=False),
    Column("quality_rating", Integer, nullable=False),
    Column("grounded", Boolean, nullable=False),
    Column("complete", Boolean, nullable=False),
)

metadata.create_all(engine)


# --------------------------------------------------
# SHARED HELPERS
# --------------------------------------------------

_SOURCE_RE = re.compile(
    r"\[SOURCE:\s*([^:\]\n]+):(\d+)(?:\s*-\s*(\d+))?\s*\]",
    re.IGNORECASE,
)


def _percent(numerator: float, denominator: float):
    return (
        round(100.0 * numerator / denominator, 2)
        if denominator else None
    )


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _normal_path(value: Any) -> str:
    path = str(value or "").replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/").lower()


def _parse_expected_paths(value):
    if isinstance(value, str):
        items = re.split(r"[,;\n]", value)
    else:
        items = value or []

    return {
        _normal_path(item)
        for item in items
        if _normal_path(item)
    }


def _unique_freshness_rows(rows):
    selected = {}

    for row in rows or []:
        if not row:
            continue

        path = _normal_path(row[0])
        if not path:
            continue

        current = selected.get(path)

        has_metadata = (
            len(row) > 2
            and _finite_number(row[2])
            and len(row) > 1
            and bool(row[1])
        )

        current_has_metadata = (
            current is not None
            and len(current) > 2
            and _finite_number(current[2])
            and len(current) > 1
            and bool(current[1])
        )

        if current is None or (
            has_metadata and not current_has_metadata
        ):
            selected[path] = row

    return list(selected.values())


# --------------------------------------------------
# RUN METRICS
# --------------------------------------------------

def calculate_run_metrics(
    *,
    answer: str,
    provenance: list[list[Any]],
    freshness: list[list[Any]],
    latency: dict[str, Any],
    expected_relevant_paths=None,
    k: int = 5,
) -> dict[str, Any]:

    k = max(1, int(k))

    provenance_rows = [
        row for row in (provenance or []) if row
    ]

    retrieved_chunk_paths = [
        _normal_path(row[0])
        for row in provenance_rows
        if _normal_path(row[0])
    ]

    unique_retrieved_paths = list(
        dict.fromkeys(retrieved_chunk_paths)
    )

    top_files = unique_retrieved_paths[:k]

    relevance_values = [
        float(row[2])
        for row in provenance_rows
        if len(row) > 2 and _finite_number(row[2])
    ]

    expected = _parse_expected_paths(
        expected_relevant_paths
    )

    labelled = bool(expected)

    retrieved_relevant = [
        path for path in top_files if path in expected
    ]

    retrieval_denominator = min(k, len(top_files))

    retrieval = {
        "retrieved_chunks": len(retrieved_chunk_paths),
        "retrieved_unique_files": len(unique_retrieved_paths),
        "mean_similarity_proxy": (
            round(
                sum(relevance_values) / len(relevance_values), 4
            ) if relevance_values else None
        ),
        "top1_similarity_proxy": (
            round(relevance_values[0], 4)
            if relevance_values else None
        ),
        "labelled_evaluation": labelled,
        "evaluation_unit": "unique_repository_files",
        "evaluation_cutoff": k,
        "precision_at_5": None,
        "recall_at_5": None,
        "mrr": None,
    }

    if labelled:
        retrieval["precision_at_5"] = (
            round(
                len(retrieved_relevant) / retrieval_denominator,
                4,
            ) if retrieval_denominator else 0.0
        )

        retrieval["recall_at_5"] = round(
            len(set(retrieved_relevant)) / len(expected), 4
        )

        first_relevant_rank = next(
            (
                rank
                for rank, path in enumerate(top_files, start=1)
                if path in expected
            ),
            None,
        )

        retrieval["mrr"] = (
            round(1.0 / first_relevant_rank, 4)
            if first_relevant_rank is not None else 0.0
        )

        retrieval["relevant_expected_files"] = len(expected)
        retrieval["relevant_retrieved_files"] = len(
            set(retrieved_relevant)
        )

    # Freshness metrics
    unique_freshness = _unique_freshness_rows(
        freshness or []
    )

    known_scores = []
    known_timestamp_count = 0
    recent_count = 0

    status_counts = {
        "recent": 0,
        "moderately_recent": 0,
        "aging": 0,
        "old": 0,
        "very_old": 0,
        "unknown": 0,
    }

    for row in unique_freshness:
        timestamp = row[1] if len(row) > 1 else None
        score = row[2] if len(row) > 2 else None
        status = str(
            row[3] if len(row) > 3 else "unknown"
        ).lower()

        if status not in status_counts:
            status = "unknown"

        status_counts[status] += 1

        if timestamp and _finite_number(score):
            known_timestamp_count += 1
            known_scores.append(float(score))

            if status == "recent":
                recent_count += 1

    freshness_total = len(unique_freshness)

    freshness_metrics = {
        "mean_freshness_score": (
            round(
                sum(known_scores) / len(known_scores), 2
            ) if known_scores else None
        ),
        "freshness_coverage_pct": _percent(
            known_timestamp_count, freshness_total
        ),
        "recent_sources_pct": _percent(
            recent_count, known_timestamp_count
        ),
        "sources_with_known_timestamp": known_timestamp_count,
        "sources_evaluated": freshness_total,
        "unknown_sources": status_counts["unknown"],
        "recent_sources": status_counts["recent"],
        "moderately_recent_sources": status_counts[
            "moderately_recent"
        ],
        "aging_sources": status_counts["aging"],
        "old_sources": status_counts["old"],
        "very_old_sources": status_counts["very_old"],
    }

    # Citation validation
    citations = _SOURCE_RE.findall(answer or "")

    retrieved_ranges = {}

    for row in provenance_rows:
        if len(row) < 2:
            continue

        match = re.match(
            r"^\s*(\d+)\s*-\s*(\d+)\s*$",
            str(row[1]),
        )

        if not match:
            continue

        start_line, end_line = (
            int(match.group(1)),
            int(match.group(2)),
        )

        if start_line >= 1 and end_line >= start_line:
            retrieved_ranges.setdefault(
                _normal_path(row[0]), []
            ).append((start_line, end_line))

    valid_citations = 0
    cited_files = set()
    invalid_citations = 0

    for raw_path, raw_start, raw_end in citations:
        path = _normal_path(raw_path)
        start_line = int(raw_start)
        end_line = int(raw_end) if raw_end else start_line

        if start_line < 1 or end_line < start_line:
            invalid_citations += 1
            continue

        ranges = retrieved_ranges.get(path, [])

        fully_contained = any(
            retrieved_start <= start_line
            and end_line <= retrieved_end
            for retrieved_start, retrieved_end in ranges
        )

        if fully_contained:
            valid_citations += 1
            cited_files.add(path)
        else:
            invalid_citations += 1

    response_metrics = {
        "citation_count": len(citations),
        "valid_citation_count": valid_citations,
        "invalid_citation_count": invalid_citations,
        "citation_provenance_precision_pct": _percent(
            valid_citations, len(citations)
        ),
        "retrieved_source_citation_coverage_pct": _percent(
            len(cited_files), len(set(retrieved_chunk_paths))
        ),
        "citation_check": (
            "No source citations found"
            if not citations
            else "All citations are fully contained in retrieved provenance"
            if valid_citations == len(citations)
            else "Some citations are missing or exceed retrieved provenance ranges"
        ),
        "quality_score": None,
        "quality_score_note": (
            "Requires human review or a labelled answer benchmark; "
            "not inferred from citation presence alone."
        ),
    }

    latency_metrics = {
        key: round(float(value), 4)
        for key, value in (latency or {}).items()
        if _finite_number(value)
    }

    return {
        "retrieval": retrieval,
        "freshness": freshness_metrics,
        "response": response_metrics,
        "latency": latency_metrics,
    }


# --------------------------------------------------
# DATABASE PERSISTENCE
# --------------------------------------------------

def _repo_hash(repo_key: str) -> str:
    return hashlib.sha256(
        repo_key.encode("utf-8")
    ).hexdigest()[:16]


def record_execution(
    *,
    repo_key: str,
    success: bool,
    elapsed_seconds: float,
    metrics: dict[str, Any] | None = None,
    retrieval_mode: str | None = None,
    error_type: str | None = None,
) -> None:

    latency = (metrics or {}).get("latency", {})

    record = {
        "timestamp_utc": datetime.now(timezone.utc),
        "repo_hash": _repo_hash(repo_key),
        "success": bool(success),
        "elapsed_seconds": round(float(elapsed_seconds), 4),
        "retrieval_mode": retrieval_mode,
        "error_type": error_type,
        "latency": latency,
    }

    with engine.begin() as connection:
        connection.execute(
            evaluation_runs.insert().values(**record)
        )


def record_human_review(
    *,
    repo_key: str,
    quality_rating: int,
    grounded: bool,
    complete: bool,
) -> None:

    rating = max(1, min(5, int(quality_rating)))

    record = {
        "timestamp_utc": datetime.now(timezone.utc),
        "repo_hash": _repo_hash(repo_key),
        "quality_rating": rating,
        "grounded": bool(grounded),
        "complete": bool(complete),
    }

    with engine.begin() as connection:
        connection.execute(
            human_reviews.insert().values(**record)
        )


# --------------------------------------------------
# DASHBOARD SUMMARY
# --------------------------------------------------

def dashboard_summary() -> dict[str, Any]:

    with engine.connect() as connection:
        runs = connection.execute(
            select(evaluation_runs)
            .order_by(desc(evaluation_runs.c.id))
            .limit(1000)
        ).mappings().all()

        reviews = connection.execute(
            select(human_reviews)
            .order_by(desc(human_reviews.c.id))
            .limit(1000)
        ).mappings().all()

    total = len(runs)
    successes = sum(bool(row["success"]) for row in runs)

    durations = [
        float(row["elapsed_seconds"])
        for row in runs
        if _finite_number(row["elapsed_seconds"])
        and float(row["elapsed_seconds"]) >= 0
    ]

    sorted_durations = sorted(durations)

    def percentile(values, p):
        if not values:
            return None

        index = max(
            0,
            min(
                len(values) - 1,
                math.ceil(p * len(values)) - 1,
            ),
        )

        return round(values[index], 3)

    valid_ratings = [
        int(row["quality_rating"])
        for row in reviews
        if isinstance(row["quality_rating"], int)
        and not isinstance(row["quality_rating"], bool)
        and 1 <= row["quality_rating"] <= 5
    ]

    return {
        "runs_recorded": total,
        "success_rate_pct": _percent(successes, total),
        "failure_count": total - successes,
        "median_end_to_end_seconds": (
            round(median(durations), 3)
            if durations else None
        ),
        "p95_end_to_end_seconds": percentile(
            sorted_durations, 0.95
        ),
        "human_reviews": len(reviews),
        "mean_human_quality_rating": (
            round(
                sum(valid_ratings) / len(valid_ratings), 2
            ) if valid_ratings else None
        ),
        "human_grounded_pct": _percent(
            sum(bool(row["grounded"]) for row in reviews),
            len(reviews),
        ),
        "human_complete_pct": _percent(
            sum(bool(row["complete"]) for row in reviews),
            len(reviews),
        ),
    }