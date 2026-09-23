"""Batched category classification, the second lifespan loop.

Same shape as services/scheduler.py: load in one short session, classify,
persist in another. The OpenAI SDK is synchronous, so services/llm.py runs each
call on a worker thread — calling it inline would stall `/health` and every
in-flight feed fetch. See CLAUDE.md's runtime constraints.

Category filters; content_type and is_city_relevant are the two admission tests;
the seven engagement dimensions are the ranking score. An unclassified article is
not stuck waiting on this loop: the feed falls back to the deterministic is_local
test for locality, admits any unjudged content_type, and rank.py gives an unrated
article the floor score while the card hides it entirely.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.enums import ENGAGEMENT_DIMENSIONS
from app.db.models.article import Article
from app.db.models.city import City
from app.db.models.source import Source
from app.services.llm import (
    ClassificationError,
    LlmClient,
    TitleClassification,
    build_client,
    classify_titles,
)

logger = logging.getLogger(__name__)


async def load_unclassified(
    db: AsyncSession, now: datetime, settings: Settings
) -> list[tuple[str, str, str | None, str | None]]:
    """The next batch of (article_id, title, summary, feed_city) to classify:
    inside the window, not yet judged, and not past its retry budget. Oldest
    first, so a backlog drains in arrival order rather than starving old
    articles.

    Keyed on emotional_salience, the newest column — the same move as when
    location_scope, the shareability axes, community_impact, is_city_relevant and
    then content_type were added. An article classified before the engagement
    dimensions existed still needs a pass, and keying on an older column would
    leave every one of them permanently unrated. Unlike the content_type filter
    this is not a fail-open hole — an unrated row is hidden from the score rather
    than admitted by it — but it is a row that can never be picked on merit, and
    nothing on screen says so.

    Any one of the seven would do as the predicate, since the classifier writes
    all seven in one transaction; the first in weight order is the arbitrary but
    stable choice.

    The feed's city comes along because relevance is defined relative to it — the
    model cannot answer "is this about the feed's city" without being told which.
    """
    window_start = now - timedelta(hours=settings.story_window_hours)
    rows = (
        await db.execute(
            select(Article.id, Article.title, Article.summary, City.name)
            .join(Source, Article.source_id == Source.id)
            .join(City, Source.city_id == City.id)
            .where(
                Article.emotional_salience.is_(None),
                Article.category_attempts < settings.classify_max_attempts,
                Article.fetched_at >= window_start,
            )
            .order_by(Article.fetched_at)
            .limit(settings.classify_batch_size)
        )
    ).all()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


async def persist_categories(
    db: AsyncSession,
    assigned: dict[str, TitleClassification],
    attempted_ids: list[str],
) -> int:
    """Write what we got, and count the attempt against every article in the
    batch — including the ones that came back unclassified, or a title the model
    keeps refusing would be re-sent every pass forever."""
    for article_id in attempted_ids:
        article = await db.get(Article, article_id)
        if article is None:  # window rolled, or deleted mid-pass
            continue
        article.category_attempts += 1
        result = assigned.get(article_id)
        if result is None:
            continue
        article.category = result.category.value
        article.content_type = result.content_type.value
        article.is_city_relevant = result.is_city_relevant
        # The request schema can't carry min/max (OpenAI strict mode rejects
        # them), so the range is enforced here instead.
        article.relevance_confidence = min(1.0, max(0.0, result.confidence))
        # All seven together — rank.py treats a partial row as unrated, so a
        # half-written article would score the floor rather than score wrongly.
        for name in ENGAGEMENT_DIMENSIONS:
            setattr(article, name, getattr(result, name).value)
    await db.commit()
    return len(assigned)


async def run_classify_pass(
    session_factory: async_sessionmaker, settings: Settings, client: LlmClient
) -> int:
    """One batch: load candidates, one LLM call, persist. Separate from the
    loop so tests can drive a single pass."""
    now = datetime.now(UTC)

    async with session_factory() as db:
        batch = await load_unclassified(db, now, settings)
    if not batch:
        return 0

    article_ids = [article_id for article_id, _, _, _ in batch]
    items = [(title, summary, city) for _, title, summary, city in batch]

    try:
        async with session_factory() as db:
            try:
                by_index = await classify_titles(db, items, client, settings)
            finally:
                # LlmCall rows are usage accounting: keep them either way, and
                # above all when every attempt failed — those are still billed.
                await db.commit()
        assigned = {article_ids[index]: result for index, result in by_index.items()}
    except ClassificationError as exc:
        logger.warning(
            "classify: batch failed, classifications left null",
            extra={"stage": "classify", "batch_size": len(batch), "error": str(exc)},
        )
        assigned = {}

    async with session_factory() as db:
        return await persist_categories(db, assigned, article_ids)


async def classifier_loop(session_factory, settings: Settings) -> None:
    """Forever, until cancelled. A failing pass is logged and retried on the
    next interval — a dead loop means stories keep serving with no category and
    nothing errors, which is why the unclassified backlog is an alert."""
    try:
        client = build_client(settings)
    except RuntimeError as exc:
        # No API key: the fetch loop must still run, so this is a warning and a
        # return, never an exception that takes the lifespan down with it.
        logger.warning("classify: not starting", extra={"stage": "classify", "reason": str(exc)})
        return

    logger.info(
        "classify: started",
        extra={"stage": "classify", "tick_seconds": settings.classify_tick_seconds},
    )
    # As in scheduler_loop: the cancel almost always lands in the sleep, so the
    # CancelledError handler wraps the whole loop rather than just the pass.
    try:
        while True:
            try:
                classified = await run_classify_pass(session_factory, settings, client)
                if classified:
                    logger.info(
                        "classify: pass complete",
                        extra={"stage": "classify", "classified": classified},
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("classify: pass failed", extra={"stage": "classify"})

            await asyncio.sleep(settings.classify_tick_seconds)
    except asyncio.CancelledError:
        logger.info("classify: stopped", extra={"stage": "classify"})
        raise
