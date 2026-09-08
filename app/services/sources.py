"""Source CRUD and feed validation. Endpoints stay thin; this owns the logic
and the DB writes. See CLAUDE.md: business logic lives in services/."""

from datetime import UTC, datetime

import feedparser
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.net import FetchError, fetch_url
from app.db.models.source import Source
from app.schemas.source import SourceCheckResult, SourceCreate, SourceUpdate


class SourceNotFoundError(Exception):
    pass


def create_source(db: Session, data: SourceCreate) -> Source:
    source = Source(**data.model_dump())
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def list_sources(db: Session, *, include_archived: bool = False) -> list[Source]:
    stmt = select(Source)
    if not include_archived:
        stmt = stmt.where(Source.archived_at.is_(None))
    return list(db.scalars(stmt.order_by(Source.id)))


def get_source(db: Session, source_id: int) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise SourceNotFoundError(source_id)
    return source


def update_source(db: Session, source_id: int, data: SourceUpdate) -> Source:
    source = get_source(db, source_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    db.commit()
    db.refresh(source)
    return source


def archive_source(db: Session, source_id: int) -> Source:
    """Soft delete. There is no hard delete: Article.source_id points here
    forever, and archiving is idempotent (archiving an already-archived
    source just refreshes the timestamp)."""
    source = get_source(db, source_id)
    source.archived_at = datetime.now(UTC)
    db.commit()
    db.refresh(source)
    return source


def check_source(db: Session, source_id: int, settings: Settings) -> SourceCheckResult:
    """Fetch the source's feed_url right now through the SSRF-safe fetcher and
    confirm it parses as a feed with at least one entry. Also records the
    outcome on the Source row, the same fields a real ingest run would set, so
    a manual check and a scheduled poll leave consistent history."""
    source = get_source(db, source_id)
    now = datetime.now(UTC)

    try:
        result = fetch_url(source.feed_url, settings)
    except FetchError as exc:
        source.last_error = str(exc)
        source.last_error_at = now
        db.commit()
        return SourceCheckResult(ok=False, error=str(exc))

    parsed = feedparser.parse(result.content)
    if parsed.bozo and not parsed.entries:
        error = f"response did not parse as a feed: {parsed.bozo_exception}"
        source.last_error = error
        source.last_error_at = now
        db.commit()
        return SourceCheckResult(ok=False, status_code=result.status_code, error=error)

    source.last_success_at = now
    source.last_error = None
    source.last_error_at = None
    db.commit()
    return SourceCheckResult(
        ok=True, status_code=result.status_code, entry_count=len(parsed.entries)
    )
