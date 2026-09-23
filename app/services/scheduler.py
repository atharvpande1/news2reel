"""Continuous per-source feed polling, started from the app lifespan.

Shape of a tick, and why: load the due sources in one short session, fetch
them all concurrently (I/O-bound, bounded by a semaphore) holding no session at
all, then persist in a second short session. FetchTarget/FetchAttempt are plain
data so the fetch phase carries no ORM instance and no pooled connection sits
idle for the length of the slowest feed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.net import FetchError, FetchResult, build_async_client, fetch_url_async
from app.db.models.city import City
from app.db.models.source import Source
from app.services.ingest import (
    SourceIngestOutcome,
    build_conditional_headers,
    parse_and_persist,
    record_fetch_failure,
)

logger = logging.getLogger(__name__)

# A failing source's interval doubles per consecutive failure; cap the exponent
# so a long-dead feed can't build an absurdly large intermediate integer.
_MAX_BACKOFF_EXPONENT = 32


@dataclass
class FetchTarget:
    """What the fetch phase needs for one source — plain data, no ORM
    instance, so it outlives the session that loaded it."""

    source_id: int
    feed_url: str
    conditional_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FetchAttempt:
    source_id: int
    attempted_at: datetime
    result: FetchResult | None = None
    error: str | None = None


def effective_interval_seconds(source: Source, settings: Settings) -> int:
    """The configured interval, floored by what the origin's `cache-control`
    said was useful, then extended by exponential backoff while the source is
    failing.

    The backoff ceiling clamps the *backoff*, never the base interval — a
    source deliberately configured to poll weekly must not be dragged back to
    every six hours by the cap.
    """
    base = max(source.fetch_interval_minutes * 60, source.cache_max_age_seconds or 0)
    if not source.consecutive_failures:
        return base
    exponent = min(source.consecutive_failures, _MAX_BACKOFF_EXPONENT)
    backed_off = base * 2**exponent
    return max(base, min(backed_off, settings.scheduler_max_backoff_seconds))


def is_due(source: Source, now: datetime, settings: Settings) -> bool:
    # last_fetched_at, not last_success_at: it's stamped on every attempt, so a
    # permanently broken feed waits out its backoff instead of being retried on
    # every single tick.
    if source.last_fetched_at is None:
        return True
    due_at = source.last_fetched_at + timedelta(
        seconds=effective_interval_seconds(source, settings)
    )
    return now >= due_at


def due_sources(sources: list[Source], now: datetime, settings: Settings) -> list[Source]:
    return [source for source in sources if is_due(source, now, settings)]


async def load_due_targets(
    db: AsyncSession, now: datetime, settings: Settings
) -> list[FetchTarget]:
    """Due-ness is computed in Python rather than SQL: the source count is in
    the tens, and this keeps the arithmetic under direct test."""
    sources = list(
        await db.scalars(
            select(Source).where(Source.enabled.is_(True), Source.archived_at.is_(None))
        )
    )
    return [
        FetchTarget(
            source_id=source.id,
            feed_url=source.feed_url,
            conditional_headers=build_conditional_headers(source),
        )
        for source in due_sources(sources, now, settings)
    ]


async def fetch_targets(targets: list[FetchTarget], settings: Settings) -> list[FetchAttempt]:
    """Fetch every target concurrently, bounded by a semaphore, over one shared
    connection pool. Touches no DB.

    Named for the targets rather than for due-ness: the forced refresh below
    sends sources that are explicitly *not* due through this same path."""
    if not targets:
        return []

    semaphore = asyncio.Semaphore(settings.scheduler_max_concurrent_fetches)

    async with build_async_client(settings) as client:

        async def fetch_one(target: FetchTarget) -> FetchAttempt:
            async with semaphore:
                attempted_at = datetime.now(UTC)
                try:
                    result = await fetch_url_async(
                        target.feed_url,
                        settings,
                        client=client,
                        conditional_headers=target.conditional_headers,
                    )
                except FetchError as exc:
                    return FetchAttempt(
                        source_id=target.source_id, attempted_at=attempted_at, error=str(exc)
                    )
                except Exception as exc:
                    # One source's unexpected failure must not lose the whole
                    # tick's other results. See CLAUDE.md's invariants.
                    logger.warning(
                        "scheduler: unexpected fetch failure",
                        extra={"stage": "scheduler", "source_id": target.source_id},
                        exc_info=exc,
                    )
                    return FetchAttempt(
                        source_id=target.source_id,
                        attempted_at=attempted_at,
                        error=f"unexpected fetch failure: {exc}",
                    )
                return FetchAttempt(
                    source_id=target.source_id, attempted_at=attempted_at, result=result
                )

        return list(await asyncio.gather(*(fetch_one(target) for target in targets)))


async def persist_attempts(
    db: AsyncSession, attempts: list[FetchAttempt]
) -> list[SourceIngestOutcome]:
    """One commit per source, so an unexpected failure part-way through a tick
    keeps the sources already written rather than discarding the batch."""
    # Shared across the whole tick, so two mastheads carrying the same story
    # collide before either insert runs.
    seen_ids: set[str] = set()
    outcomes = []

    for attempt in attempts:
        source = await db.get(Source, attempt.source_id)
        if source is None:  # archived or deleted mid-tick
            continue
        try:
            if attempt.error is not None:
                outcome = record_fetch_failure(source, attempt.error, attempt.attempted_at)
            else:
                outcome = await parse_and_persist(
                    db, source, attempt.result, attempt.attempted_at, seen_ids=seen_ids
                )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.exception(
                "scheduler: failed to persist source",
                extra={"stage": "scheduler", "source_id": attempt.source_id},
            )
            # populate_existing: the rollback expired the instance, and reading
            # an expired attribute would be implicit IO.
            source = await db.get(Source, attempt.source_id, populate_existing=True)
            outcome = record_fetch_failure(source, f"persist failed: {exc}", attempt.attempted_at)
            await db.commit()
        outcomes.append(outcome)

    return outcomes


async def run_tick(
    session_factory: async_sessionmaker, settings: Settings
) -> list[SourceIngestOutcome]:
    """One pass: find what's due, fetch it concurrently, persist it on one
    thread. Separate from the loop so tests can drive a single tick."""
    now = datetime.now(UTC)

    async with session_factory() as db:
        targets = await load_due_targets(db, now, settings)
    if not targets:
        return []

    attempts = await fetch_targets(targets, settings)

    async with session_factory() as db:
        return await persist_attempts(db, attempts)


@dataclass
class RefreshedSource:
    """One source's result from a forced refresh, carrying the name so the
    caller can report per-source outcomes without a second query."""

    source_id: int
    name: str
    ok: bool
    new_articles: int
    not_modified: bool
    error: str | None


class CityNotFoundError(Exception):
    pass


class RefreshInProgressError(Exception):
    """A refresh for this city is already running.

    Guarded rather than queued because the button is in the UI and a second
    click would send another full round of requests at third-party publishers
    while the first is still in flight.
    """


# City ids with a refresh in flight. A plain set is enough: the check and the
# add happen on the event loop with no await between them, and `--workers 1`
# means there is only ever one event loop.
_refreshing: set[int] = set()


async def load_city_targets(db: AsyncSession, city_id: int) -> list[FetchTarget]:
    """Every live source of one city, due or not.

    Backoff is deliberately ignored. A source that has been failing is exactly
    the one someone hits refresh to retry, and making them wait out an
    exponential backoff they cannot see would be its own small mystery.
    """
    if await db.get(City, city_id) is None:
        raise CityNotFoundError(city_id)

    sources = await db.scalars(
        select(Source).where(
            Source.city_id == city_id,
            Source.enabled.is_(True),
            Source.archived_at.is_(None),
        )
    )
    return [
        FetchTarget(
            source_id=source.id,
            feed_url=source.feed_url,
            conditional_headers=build_conditional_headers(source),
        )
        for source in sources
    ]


async def refresh_city(
    session_factory: async_sessionmaker, settings: Settings, city_id: int
) -> list[RefreshedSource]:
    """Poll one city's sources right now, ignoring the schedule.

    Same shape as run_tick — load, fetch holding no session, persist — so the
    forced path cannot drift from the scheduled one. Conditional GET is kept: a
    304 is a truthful "nothing new", not a cache getting in the way, and it
    keeps a refresh cheap for the publisher.

    This can overlap a scheduled tick writing the same sources. Row writes are
    last-writer-wins on bookkeeping columns, and the article insert is
    ON CONFLICT DO NOTHING, so the overlap cannot duplicate or fail.
    """
    if city_id in _refreshing:
        raise RefreshInProgressError(city_id)
    _refreshing.add(city_id)
    try:
        async with session_factory() as db:
            targets = await load_city_targets(db, city_id)
        if not targets:
            return []

        attempts = await fetch_targets(targets, settings)

        async with session_factory() as db:
            outcomes = await persist_attempts(db, attempts)
            names = {
                source.id: source.name
                for source in await db.scalars(
                    select(Source).where(Source.id.in_([o.source_id for o in outcomes]))
                )
            }
        return [
            RefreshedSource(
                source_id=outcome.source_id,
                name=names.get(outcome.source_id, "unknown source"),
                ok=outcome.ok,
                new_articles=outcome.new_articles,
                not_modified=outcome.not_modified,
                error=outcome.error,
            )
            for outcome in outcomes
        ]
    finally:
        _refreshing.discard(city_id)


async def scheduler_loop(session_factory, settings: Settings) -> None:
    """Forever, until cancelled. A failing tick is logged and retried on the
    next interval — it never kills the loop, because a dead loop means every
    run afterwards silently scores a stale corpus."""
    logger.info(
        "scheduler: started",
        extra={"stage": "scheduler", "tick_seconds": settings.scheduler_tick_seconds},
    )
    # The cancel almost always lands in the sleep below, not in a tick, so the
    # CancelledError handler wraps the whole loop rather than just the tick —
    # otherwise a clean shutdown never logs that the scheduler stopped.
    try:
        while True:
            try:
                outcomes = await run_tick(session_factory, settings)
                if outcomes:
                    logger.info(
                        "scheduler: tick complete",
                        extra={
                            "stage": "scheduler",
                            "sources_polled": len(outcomes),
                            "new_articles": sum(o.new_articles for o in outcomes),
                            "not_modified": sum(1 for o in outcomes if o.not_modified),
                            "failed": sum(1 for o in outcomes if not o.ok),
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler: tick failed", extra={"stage": "scheduler"})

            await asyncio.sleep(settings.scheduler_tick_seconds)
    except asyncio.CancelledError:
        logger.info("scheduler: stopped", extra={"stage": "scheduler"})
        raise
