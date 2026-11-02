import time
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from rag.chunker import chunk_source
from rag.embeddings import embed_texts, lexical_fallback
from rag.freshness import freshness_score
from rag.reranker import combine_scores


_METADATA_TTL_SECONDS = 300
_METADATA_MAX_ENTRIES = 2048
_metadata_cache = {}
_metadata_lock = threading.Lock()


def _commit_metadata_uncached(owner, repo, path, token):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/commits",
        params={"path": path, "per_page": 1},
        headers=headers,
        timeout=(5, 12),
    )
    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        raise RuntimeError("GITHUB_RATE_LIMIT")
    response.raise_for_status()

    data = response.json()
    if not data:
        return None, None

    commit = data[0].get("commit", {})
    committer = commit.get("committer") or commit.get("author") or {}
    return data[0].get("sha"), committer.get("date")


def _commit_metadata(owner, repo, path, token):
    """Use a short-lived metadata cache to avoid repeat per-file API calls."""
    # Never share private-repository metadata across credentials.
    credential_scope = "authenticated" if token else "public"
    key = (owner.lower(), repo.lower(), path, credential_scope)
    now = time.monotonic()

    with _metadata_lock:
        cached = _metadata_cache.get(key)
        if cached and now - cached[0] < _METADATA_TTL_SECONDS:
            return cached[1], cached[2]

    value = _commit_metadata_uncached(owner, repo, path, token)

    with _metadata_lock:
        if len(_metadata_cache) >= _METADATA_MAX_ENTRIES:
            oldest = min(_metadata_cache, key=lambda item: _metadata_cache[item][0])
            _metadata_cache.pop(oldest, None)
        _metadata_cache[key] = (now, value[0], value[1])

    return value


def _get_metadata_for_file(args):
    owner, repo, path, token = args
    try:
        return _commit_metadata(owner, repo, path, token)
    except Exception:
        # Preserve the existing graceful behavior: retrieval can continue with
        # unknown commit metadata when GitHub metadata is temporarily unavailable.
        return None, None


def rank_repository(question, files, owner, repo, token, top_k=5):
    start = time.perf_counter()
    chunks = []

    metadata_args = [
        (owner, repo, item["path"], token)
        for item in files
    ]

    # Bounded parallel requests reduce serial network wait without flooding GitHub.
    workers = min(4, max(1, len(metadata_args)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        metadata = list(executor.map(_get_metadata_for_file, metadata_args))

    for file_item, (sha, timestamp) in zip(files, metadata):
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
        vectors = embed_texts([question] + texts)
        similarities = np.dot(vectors[1:], vectors[0])
        embedding_mode = "sentence_transformer_cached"
    except Exception:
        similarities, embedding_mode = lexical_fallback(question, texts)

    ranked = combine_scores(chunks, similarities)[:max(1, int(top_k))]
    return ranked, {
        "embedding_and_ranking_seconds": time.perf_counter() - start,
        "embedding_mode": embedding_mode,
        "chunks_processed": len(chunks),
        "embedding_cache_enabled": True,
    }
