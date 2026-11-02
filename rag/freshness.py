from datetime import datetime, timezone
def freshness_score(commit_timestamp, now=None):
    if not commit_timestamp: return None, "unknown"
    try:
        dt=datetime.fromisoformat(commit_timestamp.replace("Z","+00:00"))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        now=now or datetime.now(timezone.utc)
        age=max(0,(now-dt.astimezone(timezone.utc)).total_seconds()/3600)
        if age<=24: return 100.0,"recent"
        if age<=24*7: return 90.0,"moderately_recent"
        if age<=24*30: return 75.0,"aging"
        if age<=24*90: return 50.0,"old"
        return 25.0,"very_old"
    except (ValueError,TypeError): return None,"unknown"
