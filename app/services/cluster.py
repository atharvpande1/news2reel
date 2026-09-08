"""Connected components over the combined-similarity graph, with a density
check that rejects single-linkage chaining.

Connected components *is* single-linkage clustering, and single-linkage
chains: A->B at 0.8 and B->C at 0.8 groups A with C even when A->C is 0.3. One
generic bridging article can merge a dozen distinct events into a mega-cluster
that then reports a large masthead count and lands at #1 — the failure mode
that matters most here, because it produces a plausible wrong answer rather
than an error. See CLAUDE.md's domain rules and docs/plan-phase-1.md step 2.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models.article import Article
from app.db.models.cluster import ClusterEdge
from app.services.similarity import (
    deserialize_embedding,
    embed_texts,
    embedding_model_tag,
    pairwise_similarity,
    serialize_embedding,
    tfidf_matrix,
)


@dataclass
class RawCluster:
    article_indices: list[int]
    density: float


def _article_text(article: Article) -> str:
    return f"{article.title} {article.summary or ''}".strip()


def ensure_embeddings(db: Session, articles: list[Article], settings: Settings) -> np.ndarray:
    """Cache embeddings by content_hash + embedding_model; compute only what's
    missing or stale (a model upgrade changes the tag, so old vectors are
    treated as missing). See CLAUDE.md's domain rules."""
    tag = embedding_model_tag(settings)
    vectors: list[np.ndarray | None] = [None] * len(articles)
    missing_idx: list[int] = []
    missing_text: list[str] = []

    for i, article in enumerate(articles):
        if article.embedding is not None and article.embedding_model == tag:
            vectors[i] = deserialize_embedding(article.embedding)
        else:
            missing_idx.append(i)
            missing_text.append(_article_text(article))

    if missing_text:
        computed = embed_texts(missing_text, settings)
        for j, idx in enumerate(missing_idx):
            vectors[idx] = computed[j]
            articles[idx].embedding = serialize_embedding(computed[j])
            articles[idx].embedding_model = tag
        db.flush()

    if not vectors:
        return np.empty((0, 0), dtype=np.float32)
    return np.vstack(vectors)


def _density(sim: np.ndarray, indices: list[int]) -> float:
    n = len(indices)
    if n <= 1:
        return 1.0
    sub = sim[np.ix_(indices, indices)]
    total = sub.sum() - np.trace(sub)  # exclude self-similarity on the diagonal
    return float(total / (n * (n - 1)))


def _components_at(sim: np.ndarray, indices: list[int], threshold: float) -> list[list[int]]:
    """Connected components of the subgraph induced by `indices`, using edges
    where combined similarity clears `threshold`. Returns groups of original
    indices (into the full similarity matrix)."""
    n = len(indices)
    if n <= 1:
        return [indices]

    sub = sim[np.ix_(indices, indices)]
    adjacency = sp.csr_matrix(sub >= threshold)
    _n_components, labels = connected_components(adjacency, directed=False)

    groups: dict[int, list[int]] = {}
    for local_idx, label in enumerate(labels):
        groups.setdefault(int(label), []).append(indices[local_idx])
    return list(groups.values())


def _split(
    sim: np.ndarray, indices: list[int], threshold: float, settings: Settings, depth: int
) -> list[RawCluster]:
    density = _density(sim, indices)
    too_big = len(indices) > settings.max_cluster_size
    too_sparse = density < settings.cluster_density_floor

    if not too_big and not too_sparse:
        return [RawCluster(article_indices=indices, density=density)]

    if depth >= settings.max_split_depth or len(indices) <= 1:
        # Out of retries — accept as-is rather than loop forever. A cluster
        # that still fails here is a signal to inspect ClusterEdge and retune
        # the thresholds, not a reason to silently drop real stories.
        return [RawCluster(article_indices=indices, density=density)]

    # Always recurse at the raised threshold, even if this particular raise
    # doesn't change the grouping yet — a single step often isn't enough to
    # break a strong chained edge (e.g. two 0.8 links either side of a weak
    # 0.3 one), but a few more steps will. The depth check above is what
    # bounds this, not whether one step visibly helped.
    raised_threshold = threshold + settings.split_threshold_step
    sub_groups = _components_at(sim, indices, raised_threshold)

    results: list[RawCluster] = []
    for group in sub_groups:
        results.extend(_split(sim, group, raised_threshold, settings, depth + 1))
    return results


def cluster_articles(
    db: Session, run_id: int, articles: list[Article], settings: Settings
) -> list[RawCluster]:
    """Connected components over the combined-similarity graph, then a
    density/size check that re-splits at a higher threshold rather than
    accepting a single-linkage chained mega-cluster."""
    if not articles:
        return []

    embeddings = ensure_embeddings(db, articles, settings)
    texts = [_article_text(a) for a in articles]
    tfidf = tfidf_matrix(texts, settings)
    embed_sim, tfidf_sim, combined = pairwise_similarity(embeddings, tfidf)

    all_indices = list(range(len(articles)))
    initial_groups = _components_at(combined, all_indices, settings.similarity_threshold)

    clusters: list[RawCluster] = []
    for group in initial_groups:
        clusters.extend(_split(combined, group, settings.similarity_threshold, settings, depth=0))

    _persist_edges(
        db, run_id, articles, embed_sim, tfidf_sim, combined, settings.similarity_threshold
    )
    return clusters


def _persist_edges(
    db: Session,
    run_id: int,
    articles: list[Article],
    embed_sim: np.ndarray,
    tfidf_sim: np.ndarray,
    combined: np.ndarray,
    threshold: float,
) -> None:
    """Persist graph edges (pairs clearing the base threshold) so a bad
    cluster is debuggable after the fact and the threshold is empirically
    tunable. See CLAUDE.md's observability section."""
    n = len(articles)
    edges = [
        ClusterEdge(
            run_id=run_id,
            article_a_id=articles[i].id,
            article_b_id=articles[j].id,
            embed_sim=float(embed_sim[i, j]),
            tfidf_sim=float(tfidf_sim[i, j]),
            combined=float(combined[i, j]),
        )
        for i in range(n)
        for j in range(i + 1, n)
        if combined[i, j] >= threshold
    ]
    if edges:
        db.add_all(edges)
        db.flush()
