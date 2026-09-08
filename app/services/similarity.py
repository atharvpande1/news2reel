"""Combined article similarity: 0.5 x embedding cosine + 0.5 x TF-IDF cosine.

See CLAUDE.md's domain rules for why the lexical half matters: proper nouns
and figures are rare, high-IDF tokens, so two unrelated same-category
incidents in one city stay close in embedding space but separate lexically.
Reused as-is by the cross-day seen-store (docs/plan-phase-1.md step 8) — one
similarity implementation, two windows.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

import numpy as np
import scipy.sparse as sp
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import HashingVectorizer

from app.core.config import Settings


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embedding_model_tag(settings: Settings) -> str:
    """Cache key stored on Article.embedding_model. Bumping either half of
    settings.embedding_model_name/_version invalidates the cache — see
    CLAUDE.md's domain rules."""
    return f"{settings.embedding_model_name}:{settings.embedding_model_version}"


@lru_cache(maxsize=4)
def _get_embedding_model(model_name: str) -> SentenceTransformer:
    return SentenceTransformer(model_name)


def embed_texts(texts: list[str], settings: Settings) -> np.ndarray:
    """Returns an (n, dim) L2-normalized matrix — normalized so cosine
    similarity between rows reduces to a plain dot product."""
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    model = _get_embedding_model(settings.embedding_model_name)
    return np.asarray(
        model.encode(texts, normalize_embeddings=True, show_progress_bar=False),
        dtype=np.float32,
    )


def serialize_embedding(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def deserialize_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _make_vectorizer(settings: Settings) -> HashingVectorizer:
    # char_wb: n-grams within word boundaries — robust to the heavy
    # inflection in Indic languages, where word-level matching under-matches
    # variants of the same name. norm="l2" so, like the embeddings, cosine
    # similarity reduces to a dot product.
    return HashingVectorizer(
        analyzer="char_wb",
        ngram_range=(settings.tfidf_ngram_min, settings.tfidf_ngram_max),
        n_features=settings.tfidf_n_features,
        alternate_sign=False,
        norm="l2",
    )


def tfidf_matrix(texts: list[str], settings: Settings) -> sp.csr_matrix:
    """Returns an (n, n_features) sparse, L2-normalized matrix. No fitting —
    HashingVectorizer's weights don't depend on the corpus, which is what
    keeps them stationary across runs (see CLAUDE.md)."""
    if not texts:
        return sp.csr_matrix((0, settings.tfidf_n_features))
    return _make_vectorizer(settings).transform(texts)


def pairwise_similarity(
    embeddings: np.ndarray, tfidf: sp.csr_matrix
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (embed_sim, tfidf_sim, combined), each an (n, n) dense matrix.
    Both inputs are pre-normalized, so each half is a plain dot product. The
    two halves are returned separately (not just the combined score) so
    ClusterEdge rows carry real diagnostic values — see CLAUDE.md's
    observability section."""
    n = embeddings.shape[0]
    if n == 0:
        empty = np.empty((0, 0), dtype=np.float32)
        return empty, empty, empty
    embed_sim = embeddings @ embeddings.T
    tfidf_sim = (tfidf @ tfidf.T).toarray()
    combined = 0.5 * embed_sim + 0.5 * tfidf_sim
    return embed_sim, tfidf_sim, combined
