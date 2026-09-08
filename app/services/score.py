"""Prescore funnel (cheap features, keep the top ~30 clusters) and
videoability (computed, never LLM-scored). See CLAUDE.md's domain rules: the
LLM funnel is load-bearing — never score every article."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.core.config import Settings
from app.db.models.article import Article
from app.services.cluster import RawCluster
from app.services.llm import ClusterScoreResponse
from app.services.syndication import SyndicationResult, collapse_syndication

_DIGIT_RE = re.compile(r"\d")


@dataclass
class ClusterFeatures:
    raw_cluster: RawCluster
    articles: list[Article]
    syndication: SyndicationResult
    best_feed_position: int | None
    first_seen_at: datetime
    latest_at: datetime


def build_features(raw: RawCluster, articles: list[Article], settings: Settings) -> ClusterFeatures:
    """Computes syndication collapse as part of feature-building, not as a
    separate pipeline pass keyed by cluster identity — the two travel
    together from here through scoring and selection."""
    members = [articles[i] for i in raw.article_indices]
    positions = [a.feed_position for a in members if a.feed_position is not None]
    times = [a.published_at or a.fetched_at for a in members]
    return ClusterFeatures(
        raw_cluster=raw,
        articles=members,
        syndication=collapse_syndication(members, settings),
        best_feed_position=min(positions) if positions else None,
        first_seen_at=min(times),
        latest_at=max(times),
    )


def prescore(features: ClusterFeatures, now: datetime) -> float:
    """Cheap, feature-based, no LLM call — decides funnel membership only, not
    the final ranking (that's the LLM axis scores in select.py)."""
    masthead_component = min(features.syndication.effective_masthead_count / 5.0, 1.0)

    position_component = 0.0
    if features.best_feed_position is not None:
        position_component = max(0.0, 1.0 - (features.best_feed_position - 1) / 20.0)

    age_hours = max((now - features.latest_at).total_seconds() / 3600.0, 0.0)
    recency_component = max(0.0, 1.0 - age_hours / 36.0)

    return 0.5 * masthead_component + 0.3 * position_component + 0.2 * recency_component


def apply_funnel(
    scored: list[tuple[ClusterFeatures, float]], settings: Settings
) -> list[ClusterFeatures]:
    """Keep the top scoring_funnel_size clusters by prescore. Everything past
    this point is expensive (one LLM call per surviving cluster)."""
    ranked = sorted(scored, key=lambda pair: pair[1], reverse=True)
    return [features for features, _ in ranked[: settings.scoring_funnel_size]]


def _has_usable_image(articles: list[Article]) -> bool:
    """Feed-declared dimensions only — not fetched and verified, which would
    add an SSRF surface and decompression-bomb exposure. Spoofable, and
    acceptable: clips are typography-first, so this never gates rendering.
    See CLAUDE.md's domain rules."""
    for article in articles:
        for image in article.images:
            width, height = image.get("width"), image.get("height")
            if width and height and min(width, height) >= 1000:
                return True
    return False


def _has_number(articles: list[Article]) -> bool:
    text = " ".join(f"{a.title} {a.summary or ''} {a.body_text or ''}" for a in articles)
    return bool(_DIGIT_RE.search(text))


def compute_videoability(features: ClusterFeatures, llm_result: ClusterScoreResponse) -> dict:
    has_locality = bool(llm_result.locality.name.strip())
    has_number = _has_number(features.articles)
    is_discrete_event = llm_result.is_discrete_event
    has_usable_image = _has_usable_image(features.articles)

    # is_discrete_event weighted highest: an unwatchable analysis/opinion
    # piece is the failure mode that matters most here. has_usable_image
    # weighted lowest, matching "the flag never gates rendering" — images are
    # an enhancement layer, not a requirement.
    score = (
        0.25 * has_locality + 0.25 * has_number + 0.30 * is_discrete_event + 0.20 * has_usable_image
    )

    return {
        "has_usable_image": has_usable_image,
        "has_number": has_number,
        "has_locality": has_locality,
        "is_discrete_event": is_discrete_event,
        "score": score,
    }
