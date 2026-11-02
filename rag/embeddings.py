from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import sqlite3

import numpy as np

from config.settings import settings


CACHE_DB = Path(".cache") / "freshtrust_embeddings.sqlite3"


@lru_cache(maxsize=1)
def get_embedder():
    """Load the embedding model once per application process."""
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(settings.embedding_model, device="cpu")


def _cache_key(text: str) -> str:
    model_name = str(settings.embedding_model)
    return sha256(f"{model_name}\0{text}".encode("utf-8")).hexdigest()


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
        # Cache failure must never prevent repository analysis.
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
                    (key, np.asarray(vector, dtype=np.float32).tobytes(), int(vector.size))
                    for key, vector in items.items()
                ],
            )
    except (sqlite3.Error, OSError):
        # Inference still succeeds when the local cache cannot be written.
        pass


def embed_texts(texts):
    """
    Return normalized embeddings, reusing vectors stored in a local SQLite cache.
    Missing texts are encoded together in one batch for efficient model inference.
    """
    texts = [str(text) for text in texts]
    if not texts:
        return np.empty((0, 0), dtype=np.float32)

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
        model = get_embedder()
        vectors = model.encode(
            missing_texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
            batch_size=32,
        )
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        new_vectors = {
            key: vector for key, vector in zip(missing_keys, vectors)
        }
        _write_cached(new_vectors)
        cached.update(new_vectors)

    return np.vstack([cached[key] for key in keys]).astype(np.float32, copy=False)


def lexical_fallback(query, texts):
    """Deterministic fallback if the embedding package cannot initialize."""
    q = set(query.lower().split())
    scores = []
    for text in texts:
        t = set(text.lower().split())
        scores.append(len(q & t) / max(1, len(q)))
    return np.asarray(scores, dtype=float), "lexical_fallback"
