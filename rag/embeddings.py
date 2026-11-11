"""Cloudflare Workers AI embeddings with public-only persistent caching."""

from hashlib import sha256
import os
from pathlib import Path
import sqlite3

import numpy as np
import requests

from config.settings import settings


CACHE_DB = Path(".cache") / "freshtrust_embeddings.sqlite3"
EMBEDDING_MODEL = os.getenv(
    "CLOUDFLARE_EMBEDDING_MODEL",
    "@cf/baai/bge-small-en-v1.5",
)
EMBEDDING_BATCH_SIZE = 32
EMBEDDING_TIMEOUT_SECONDS = 60


def _cache_key(text: str) -> str:
    # Include the hosted model ID so vectors from different models never mix.
    return sha256(
        f"{EMBEDDING_MODEL}\0{text}".encode("utf-8")
    ).hexdigest()


def _connect_cache():
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
            cache_key TEXT PRIMARY KEY,
            vector BLOB NOT NULL,
            dimension INTEGER NOT NULL
        )
        """
    )
    return conn


def _read_cached(keys):
    found = {}
    if not keys:
        return found

    try:
        with _connect_cache() as conn:
            for key in keys:
                row = conn.execute(
                    "SELECT vector, dimension FROM embeddings WHERE cache_key = ?",
                    (key,),
                ).fetchone()

                if row:
                    vector = np.frombuffer(row[0], dtype=np.float32)
                    if vector.size == row[1]:
                        found[key] = vector.copy()

    except (sqlite3.Error, OSError):
        return {}

    return found


def _write_cached(items):
    if not items:
        return

    try:
        with _connect_cache() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO embeddings(cache_key, vector, dimension)
                VALUES (?, ?, ?)
                """,
                [
                    (
                        key,
                        np.asarray(vector, dtype=np.float32).tobytes(),
                        int(vector.size),
                    )
                    for key, vector in items.items()
                ],
            )

    except (sqlite3.Error, OSError):
        # Caching is an optimization; embedding generation can still succeed.
        pass


def _normalize_vectors(vectors, expected_count):
    try:
        result = np.asarray(vectors, dtype=np.float32)
    except (TypeError, ValueError):
        raise RuntimeError(
            "Cloudflare returned embeddings in an invalid format."
        ) from None

    if result.ndim != 2 or result.shape[0] != expected_count:
        raise RuntimeError(
            "Cloudflare returned an unexpected number or shape of embeddings."
        )

    if result.shape[1] == 0 or not np.all(np.isfinite(result)):
        raise RuntimeError("Cloudflare returned invalid embedding values.")

    # Match the previous SentenceTransformer normalize_embeddings=True behavior.
    norms = np.linalg.norm(result, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise RuntimeError("Cloudflare returned a zero-length embedding.")

    return result / norms


def _cloudflare_embed_batch(texts):
    if not settings.cloudflare_account_id or not settings.cloudflare_api_token:
        raise RuntimeError(
            "Cloudflare Workers AI embeddings are not configured. "
            "Check CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN."
        )

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.cloudflare_account_id}/ai/run/{EMBEDDING_MODEL}"
    )
    headers = {
        "Authorization": f"Bearer {settings.cloudflare_api_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json={"text": texts},
            timeout=EMBEDDING_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        raise RuntimeError(
            "Cloudflare Workers AI embeddings endpoint is unreachable."
        ) from None

    if not response.ok:
        raise RuntimeError(
            f"Cloudflare Workers AI embeddings returned HTTP {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(
            "Cloudflare Workers AI returned an invalid embeddings response."
        ) from None

    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError(
            "Cloudflare Workers AI did not successfully generate embeddings."
        )

    result = payload.get("result") or {}
    vectors = result.get("data") if isinstance(result, dict) else None

    if not isinstance(vectors, list):
        raise RuntimeError(
            "Cloudflare Workers AI response did not contain embedding vectors."
        )

    return _normalize_vectors(vectors, len(texts))


def _generate_embeddings(texts):
    """Generate hosted embeddings in bounded batches."""
    batches = []

    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[start:start + EMBEDDING_BATCH_SIZE]
        batches.append(_cloudflare_embed_batch(batch))

    if not batches:
        return np.empty((0, 0), dtype=np.float32)

    return np.vstack(batches).astype(np.float32, copy=False)


def embed_texts(texts, use_cache=True):
    """
    Return normalized Cloudflare embeddings.

    Public requests may use the persistent SQLite cache. Private requests pass
    use_cache=False and bypass persistent storage completely.
    """
    texts = [str(text) for text in texts]

    if not texts:
        return np.empty((0, 0), dtype=np.float32)

    # Private repository embeddings are never written to the persistent cache.
    if not use_cache:
        return _generate_embeddings(texts)

    keys = [_cache_key(text) for text in texts]
    cached = _read_cached(list(dict.fromkeys(keys)))

    missing_texts = []
    missing_keys = []
    seen_missing = set()

    for key, text in zip(keys, texts):
        if key not in cached and key not in seen_missing:
            missing_keys.append(key)
            missing_texts.append(text)
            seen_missing.add(key)

    if missing_texts:
        vectors = _generate_embeddings(missing_texts)

        new_vectors = {
            key: vector for key, vector in zip(missing_keys, vectors)
        }

        _write_cached(new_vectors)
        cached.update(new_vectors)

    return np.vstack(
        [cached[key] for key in keys]
    ).astype(np.float32, copy=False)


def lexical_fallback(query, texts):
    """Deterministic fallback if the hosted embedding service is unavailable."""
    q = set(query.lower().split())
    scores = []

    for text in texts:
        t = set(text.lower().split())
        scores.append(len(q & t) / max(1, len(q)))

    return np.asarray(scores, dtype=float), "lexical_fallback"
