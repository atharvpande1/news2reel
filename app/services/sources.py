"""Source CRUD and feed validation. Endpoints stay thin; this owns the logic
and the DB writes. See CLAUDE.md: business logic lives in services/."""

import asyncio
from datetime import UTC, datetime

import feedparser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import Settings
from app.core.net import FetchError, fetch_url
from app.db.models.source import Source
from app.schemas.source import SourceCheckResult, SourceUpdate


class SourceNotFoundError(Exception):
    pass


async def list_sources(db: AsyncSession, *, include_archived: bool = False) -> list[Source]:
    # joinedload: SourceRead carries the city's name, so without it serialising
    # the list is one extra query per source.
    stmt = select(Source).options(joinedload(Source.city))
    if not include_archived:
        stmt = stmt.where(Source.archived_at.is_(None))
    return list(await db.scalars(stmt.order_by(Source.id)))


async def get_source(db: AsyncSession, source_id: int) -> Source:
    """City eager-loaded (SourceRead reads city_name), and populate_existing so
    a copy already in the identity map is reloaded with it rather than
    returned as-is with the relationship unloaded."""
    source = await db.get(
        Source, source_id, options=[joinedload(Source.city)], populate_existing=True
    )
    if source is None:
        raise SourceNotFoundError(source_id)
    return source


async def update_source(db: AsyncSession, source_id: int, data: SourceUpdate) -> Source:
    source = await get_source(db, source_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    await db.commit()
    return await get_source(db, source_id)


async def archive_source(db: AsyncSession, source_id: int) -> Source:
    """Soft delete. There is no hard delete: Article.source_id points here
    forever, and archiving is idempotent (archiving an already-archived
    source just refreshes the timestamp)."""
    source = await get_source(db, source_id)
    source.archived_at = datetime.now(UTC)
    await db.commit()
    return await get_source(db, source_id)


async def check_source(db: AsyncSession, source_id: int, settings: Settings) -> SourceCheckResult:
    """Fetch the source's feed_url right now through the SSRF-safe fetcher and
    confirm it parses as a feed with at least one entry. Also records the
    outcome on the Source row, the same fields a real ingest run would set, so
    a manual check and a scheduled poll leave consistent history."""
    source = await get_source(db, source_id)
    now = datetime.now(UTC)

    try:
        result = await fetch_url(source.feed_url, settings)
    except FetchError as exc:
        source.last_error = str(exc)
        source.last_error_at = now
        await db.commit()
        return SourceCheckResult(ok=False, error=str(exc))

    parsed = await asyncio.to_thread(feedparser.parse, result.content)
    if parsed.bozo and not parsed.entries:
        error = f"response did not parse as a feed: {parsed.bozo_exception}"
        source.last_error = error
        source.last_error_at = now
        await db.commit()
        return SourceCheckResult(ok=False, status_code=result.status_code, error=error)

    source.last_success_at = now
    source.last_error = None
    source.last_error_at = None
    await db.commit()
    return SourceCheckResult(
        ok=True, status_code=result.status_code, entry_count=len(parsed.entries)
    )
