def choose_cached_fallback(cache_items, max_age_seconds, now_epoch):
    acceptable=[]
    for item in cache_items:
        age=now_epoch-item.get("fetched_epoch",0)
        if age >= 0 and age <= max_age_seconds:
            item=dict(item); item["retrieval_mode"]="validated_cache_fallback"; acceptable.append(item)
    return acceptable
