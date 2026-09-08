"""Collapse syndicated wire copy within a cluster before counting mastheads.

Six papers running the same wire copy is one story from one source, not six
editors independently judging it important — and syndicated copy is the
most-duplicated text in the corpus, so uncollapsed it dominates every lineup.
See CLAUDE.md's domain rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.db.models.article import Article
from app.services.similarity import tfidf_matrix


@dataclass
class SyndicationResult:
    effective_masthead_count: int
    raw_masthead_count: int
    # Groups of positional indices (into the `articles` list passed in) whose
    # body text is near-identical — each group counts as one publisher_group.
    duplicate_groups: list[list[int]]


def _body_text(article: Article) -> str:
    return article.body_text or article.summary or article.title


def collapse_syndication(articles: list[Article], settings: Settings) -> SyndicationResult:
    """Groups cluster members by near-identical body text (TF-IDF only —
    wire copy is a lexical match, not just a topical one, so the embedding
    half isn't needed here), then counts distinct publisher_group across
    those groups. One masthead's multiple feeds (city edition + main) also
    collapse to one, via publisher_group rather than source_id."""
    n = len(articles)
    raw_masthead_count = len({a.source.publisher_group for a in articles})

    if n <= 1:
        return SyndicationResult(
            effective_masthead_count=raw_masthead_count,
            raw_masthead_count=raw_masthead_count,
            duplicate_groups=[list(range(n))],
        )

    texts = [_body_text(a) for a in articles]
    tfidf = tfidf_matrix(texts, settings)
    sim = (tfidf @ tfidf.T).toarray()

    visited = [False] * n
    groups: list[list[int]] = []
    for i in range(n):
        if visited[i]:
            continue
        group = [i]
        visited[i] = True
        for j in range(i + 1, n):
            if not visited[j] and sim[i, j] >= settings.syndication_similarity_threshold:
                group.append(j)
                visited[j] = True
        groups.append(group)

    effective_masthead_count = len({articles[group[0]].source.publisher_group for group in groups})

    return SyndicationResult(
        effective_masthead_count=effective_masthead_count,
        raw_masthead_count=raw_masthead_count,
        duplicate_groups=groups,
    )
