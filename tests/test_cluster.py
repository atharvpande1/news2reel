"""The highest-value tests in the codebase — see CLAUDE.md's conventions.
Two failure modes matter more than any other clustering bug:

1. Chaining: A-B similar, B-C similar, A-C not, and connected components
   (being single-linkage) merges all three anyway. The density check exists
   specifically to catch this.
2. Two unrelated same-category incidents in one city (e.g. two different road
   accidents) getting merged because they share generic vocabulary and
   therefore sit close in embedding space — the lexical half of the combined
   score is what's supposed to prevent this.
"""

import numpy as np
import pytest

from app.core.config import Settings
from app.db.models.cluster import ClusterEdge
from app.services.cluster import _components_at, _density, _split, cluster_articles


def _settings(**overrides) -> Settings:
    defaults = dict(
        similarity_threshold=0.55,
        cluster_density_floor=0.45,
        max_cluster_size=8,
        max_split_depth=3,
        split_threshold_step=0.1,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# --- unit tests: the graph algorithm against a hand-crafted matrix -----------


def test_density_excludes_self_similarity() -> None:
    # A pair at similarity 0.8 has density 0.8, not diluted by the 1.0
    # self-similarity on the diagonal.
    sim = np.array([[1.0, 0.8], [0.8, 1.0]])
    assert _density(sim, [0, 1]) == pytest.approx(0.8)


def test_components_at_threshold_splits_disconnected_pairs() -> None:
    # 0-1 connected, 2 isolated.
    sim = np.array(
        [
            [1.0, 0.8, 0.1],
            [0.8, 1.0, 0.1],
            [0.1, 0.1, 1.0],
        ]
    )
    groups = _components_at(sim, [0, 1, 2], threshold=0.5)
    assert sorted(sorted(g) for g in groups) == [[0, 1], [2]]


def test_chaining_is_broken_up_by_density_floor() -> None:
    """The exact scenario from CLAUDE.md: A-B at 0.8, B-C at 0.8, A-C at 0.3.
    Connected components alone merges all three; the density floor must
    reject that merge and re-split at a higher threshold."""
    sim = np.array(
        [
            [1.0, 0.8, 0.3],
            [0.8, 1.0, 0.8],
            [0.3, 0.8, 1.0],
        ]
    )
    # density floor above the 3-node group's mean similarity ((0.8+0.8+0.3)/3
    # per unordered pair -> mean of all off-diagonal entries = 0.633), so a
    # floor of 0.7 must force a split.
    settings = _settings(
        similarity_threshold=0.5,
        cluster_density_floor=0.7,
        max_cluster_size=8,
        split_threshold_step=0.2,
    )
    clusters = _split(sim, [0, 1, 2], threshold=0.5, settings=settings, depth=0)

    # A and C must not end up in the same cluster.
    for cluster in clusters:
        assert not ({0, 2} <= set(cluster.article_indices))


def test_max_cluster_size_splits_two_cliques_joined_by_a_bridge() -> None:
    """Two genuine 3-node events (dense within, ~0.85) connected by one weak
    bridging edge (~0.6) — the initial threshold sees one 6-node component,
    which exceeds max_cluster_size. Raising the threshold should cut the
    bridge (weaker than the real intra-event edges) and recover the two
    proper clusters, not shatter everything into singletons."""
    n = 6
    sim = np.full((n, n), 0.1)  # unrelated by default
    for group in ([0, 1, 2], [3, 4, 5]):
        for i in group:
            for j in group:
                sim[i, j] = 0.85
    np.fill_diagonal(sim, 1.0)
    sim[2, 3] = sim[3, 2] = 0.6  # the single bridge joining the two events

    settings = _settings(
        cluster_density_floor=0.0,
        max_cluster_size=3,
        similarity_threshold=0.5,
        split_threshold_step=0.15,
    )
    clusters = _split(sim, list(range(n)), threshold=0.5, settings=settings, depth=0)

    groups = sorted(sorted(c.article_indices) for c in clusters)
    assert groups == [[0, 1, 2], [3, 4, 5]]


def test_split_terminates_and_accepts_when_raising_threshold_does_not_help() -> None:
    # A uniform, fully-dense-but-oversized block: raising the threshold above
    # 0.9 never splits it (every pair is exactly 0.9), so _split must give up
    # after max_split_depth rather than looping forever.
    n = 10
    sim = np.full((n, n), 0.9)
    np.fill_diagonal(sim, 1.0)
    settings = _settings(max_cluster_size=3, similarity_threshold=0.5, max_split_depth=2)
    clusters = _split(sim, list(range(n)), threshold=0.5, settings=settings, depth=0)
    assert sum(len(c.article_indices) for c in clusters) == n


# --- integration tests: the real embedding + TF-IDF pipeline -----------------


def test_two_same_city_accidents_do_not_merge(
    db_session, make_source, make_run, make_article
) -> None:
    """The specific regression CLAUDE.md calls out: two unrelated accidents in
    the same city, sharing generic vocabulary, must NOT cluster together."""
    source = make_source()
    run = make_run()
    articles = [
        make_article(
            source,
            title="Two killed in road accident on Kamptee Road",
            summary="A speeding truck collided with a car near Kamptee Road, killing two people.",
        ),
        make_article(
            source,
            title="Bus overturns near Wardha Road, five injured",
            summary="A state transport bus overturned on Wardha Road, injuring five passengers.",
        ),
    ]
    settings = _settings()
    clusters = cluster_articles(db_session, run.id, articles, settings)

    assert len(clusters) == 2
    for cluster in clusters:
        assert len(cluster.article_indices) == 1


def test_near_duplicate_wire_copy_clusters_together(
    db_session, make_source, make_run, make_article
) -> None:
    """Positive control: near-identical syndicated copy across two sources
    should still cluster — otherwise the density/threshold tuning is too
    strict and every story fragments into singletons."""
    source_a = make_source(name="Paper A", publisher_group="paper-a")
    source_b = make_source(name="Paper B", publisher_group="paper-b")
    run = make_run()
    headline = "Fire breaks out at a chemical factory in Butibori MIDC"
    summary = (
        "A major fire broke out at a chemical factory in the Butibori MIDC area on Tuesday night."
    )
    articles = [
        make_article(source_a, title=headline, summary=summary),
        make_article(source_b, title=headline, summary=summary),
    ]
    settings = _settings()
    clusters = cluster_articles(db_session, run.id, articles, settings)

    assert len(clusters) == 1
    assert len(clusters[0].article_indices) == 2


def test_cluster_articles_persists_edges(db_session, make_source, make_run, make_article) -> None:
    source_a = make_source(name="Paper A", publisher_group="paper-a")
    source_b = make_source(name="Paper B", publisher_group="paper-b")
    run = make_run()
    headline = "Fire breaks out at a chemical factory in Butibori MIDC"
    articles = [
        make_article(source_a, title=headline, summary="Details of the fire."),
        make_article(source_b, title=headline, summary="Details of the fire."),
    ]
    cluster_articles(db_session, run.id, articles, _settings())

    edges = db_session.query(ClusterEdge).filter_by(run_id=run.id).all()
    assert len(edges) == 1
    assert edges[0].combined >= _settings().similarity_threshold


def test_cluster_articles_caches_embeddings(
    db_session, make_source, make_run, make_article
) -> None:
    source = make_source()
    run = make_run()
    article = make_article(source, title="Some headline", summary="Some summary.")
    assert article.embedding is None

    cluster_articles(db_session, run.id, [article], _settings())

    db_session.refresh(article)
    assert article.embedding is not None
    assert article.embedding_model == "paraphrase-multilingual-MiniLM-L12-v2:v1"
