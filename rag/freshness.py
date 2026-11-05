"""Commit-timestamp-based freshness scoring for FreshTrust-MCP."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO-8601 timestamps or Unix epoch seconds into UTC."""
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def freshness_score(commit_timestamp: Any, now: datetime | None = None):
    """Return (score, status), preserving the existing retriever interface.

    Scores:
      <= 24 hours: 100 / recent
      <= 7 days:    90 / moderately_recent
      <= 30 days:   75 / aging
      <= 90 days:   50 / old
      > 90 days:    25 / very_old

    Missing or invalid timestamps return (None, 'unknown', None).
    Timestamps more than five minutes in the future are treated as invalid
    metadata rather than incorrectly receiving a perfect freshness score.
    """
    committed_at = _parse_timestamp(commit_timestamp)
    if committed_at is None:
        return None, "unknown"

    current_time = _parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
    if current_time is None:
        current_time = datetime.now(timezone.utc)

    age_seconds = (current_time - committed_at).total_seconds()

    # Allow minor clock skew, but reject implausible future commit timestamps.
    if age_seconds < -300:
        return None, "unknown"

    age_hours = max(0.0, age_seconds / 3600.0)

    if age_hours <= 24:
        score, status = 100.0, "recent"
    elif age_hours <= 24 * 7:
        score, status = 90.0, "moderately_recent"
    elif age_hours <= 24 * 30:
        score, status = 75.0, "aging"
    elif age_hours <= 24 * 90:
        score, status = 50.0, "old"
    else:
        score, status = 25.0, "very_old"

    return score, status
