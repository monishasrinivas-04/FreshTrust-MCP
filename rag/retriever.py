
import time

import numpy as np

from rag.chunker import chunk_source
from rag.embeddings import embed_texts, lexical_fallback
from rag.freshness import freshness_score
from rag.reranker import combine_scores


def rank_repository(
    question,
    files,
    owner,
    repo,
    token=None,
    top_k=5,
    cache_embeddings=True,
):
    start = time.perf_counter()
    chunks = []

    # Commit SHA and timestamp are supplied by GitHubMCPClient via list_commits.
    # Retrieval ranking, embeddings, fallback and score fusion remain unchanged.
    for file_item in files:
        timestamp = file_item.get("commit_timestamp")
        sha = file_item.get("commit_sha")
        score, status = freshness_score(timestamp)

        for chunk in chunk_source(file_item["content"], file_item["path"]):
            chunk.update(
                commit_sha=sha,
                commit_timestamp=timestamp,
                freshness_score=score,
                freshness_status=status,
            )
            chunks.append(chunk)

    if not chunks:
        raise RuntimeError("No text chunks were available for retrieval.")

    texts = [item["text"] for item in chunks]

    try:
        vectors = embed_texts(
            [question] + texts,
            use_cache=cache_embeddings,
        )

        similarities = np.dot(vectors[1:], vectors[0])

        embedding_mode = (
            "sentence_transformer_cached"
            if cache_embeddings
            else "sentence_transformer_uncached"
        )

    except Exception:
        similarities, embedding_mode = lexical_fallback(question, texts)

    ranked = combine_scores(chunks, similarities)[:max(1, int(top_k))]

    return ranked, {
        "embedding_and_ranking_seconds": time.perf_counter() - start,
        "embedding_mode": embedding_mode,
        "chunks_processed": len(chunks),
        "embedding_cache_enabled": cache_embeddings,
    }
