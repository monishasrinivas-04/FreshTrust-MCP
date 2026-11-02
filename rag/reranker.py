from config.settings import settings
def combine_scores(chunks, semantic_scores):
    out=[]
    for chunk,semantic in zip(chunks,semantic_scores):
        fresh=chunk.get("freshness_score")
        if fresh is None:
            combined=float(semantic)
        else:
            combined=settings.semantic_weight*float(semantic)+settings.freshness_weight*(float(fresh)/100)
        item=dict(chunk); item.update(semantic_score=float(semantic),combined_score=float(combined))
        out.append(item)
    return sorted(out,key=lambda x:x["combined_score"],reverse=True)
