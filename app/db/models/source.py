from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, false, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class Source(Base):
    """A polled feed. Archived, never hard-deleted — Article.source_id points
    here forever, and a delete would cascade away history or trip an FK
    mid-fetch. See CLAUDE.md's invariants."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    feed_url: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Groups a masthead's multiple feeds (city edition + main) so per-publisher
    # reporting doesn't double-count one publisher.
    publisher_group: Mapped[str] = mapped_column(String, nullable=False)

    # The city this feed covers, and the city each of its articles is judged
    # relevant against. NOT NULL: a feed with no city is a feed whose stories
    # cannot be admitted or refused, and there is no way to create one.
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), nullable=False)
    city: Mapped["City"] = relationship(back_populates="sources", lazy="raise_on_sql")  # noqa: F821

    @property
    def city_name(self) -> str | None:
        """Flattened for SourceRead, which every consumer reads a city off.
        A property rather than a schema alias so `from_attributes` picks it up
        with no per-call plumbing — callers that serialise many sources should
        eager-load `city` so this does not become a query per row."""
        return self.city.name if self.city else None

    # This masthead only covers `city`, and a genuinely local outlet has no
    # reason to put the city in its URLs. Set it and derive_is_local skips the
    # /city/<slug>/ check — which is what keeps such an outlet's stories in the
    # feed during the tick before the classifier judges them, and what lets them
    # display a city at all where the URL names none.
    is_local_outlet: Mapped[bool] = mapped_column(
        default=False, server_default=false(), nullable=False
    )

    # How often the scheduler polls this feed. Per-source because publish
    # frequency varies wildly between mastheads.
    fetch_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30"), nullable=False
    )

    # Conditional GET support (see ingest step 1 in docs/plan-phase-1.md).
    etag: Mapped[str | None] = mapped_column(String, default=None)
    last_modified: Mapped[str | None] = mapped_column(String, default=None)

    # Parsed from the response's `cache-control: max-age`. Acts as a floor on
    # fetch_interval_minutes — never poll a feed more often than the origin
    # says is useful.
    cache_max_age_seconds: Mapped[int | None] = mapped_column(Integer, default=None)

    # Every ATTEMPT, including 304s and failures — this is what the scheduler's
    # due check reads. Distinct from last_success_at on purpose: keying the due
    # check off success would re-hit a broken feed every single tick.
    last_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    # Drives exponential backoff; reset to 0 on any success (200 or 304).
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

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
