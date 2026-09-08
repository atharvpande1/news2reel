from datetime import datetime

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class Source(Base):
    """A polled feed. Archived, never hard-deleted — Article.source_id points
    here forever, and a delete would cascade away history or trip an FK
    mid-run. See CLAUDE.md's invariants."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    articles: Mapped[list["Article"]] = relationship(back_populates="source")  # noqa: F821
    name: Mapped[str] = mapped_column(String, nullable=False)
    feed_url: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Groups a masthead's multiple feeds (city edition + main) so
    # effective_masthead_count doesn't double-count one publisher.
    publisher_group: Mapped[str] = mapped_column(String, nullable=False)

    # Source's own section label -> our fixed category enum (see CLAUDE.md's
    # domain rules). Validated at the schema layer, not the DB layer.
    category_map: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)

    # Conditional GET support (see ingest step 1 in docs/plan-phase-1.md).
    etag: Mapped[str | None] = mapped_column(String, default=None)
    last_modified: Mapped[str | None] = mapped_column(String, default=None)

    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    last_error: Mapped[str | None] = mapped_column(String, default=None)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
