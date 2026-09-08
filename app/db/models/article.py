from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class Article(Base):
    """Ingestion normalisation target — lenient, most columns nullable, since
    it mirrors messy feed reality. See CLAUDE.md: Article is lenient, Story is
    strict."""

    __tablename__ = "articles"

    # Stable hash of (source_id, canonical_url) — deterministic so re-ingesting
    # the same item is an upsert, not a duplicate row.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source: Mapped["Source"] = relationship(back_populates="articles")  # noqa: F821

    url: Mapped[str] = mapped_column(String, nullable=False)
    canonical_url: Mapped[str] = mapped_column(String, nullable=False)

    title: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str | None] = mapped_column(String, default=None)
    body_text: Mapped[str | None] = mapped_column(String, default=None)

    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    fetched_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )

    language: Mapped[str] = mapped_column(String, nullable=False)

    # Position in the feed at fetch time — a prominence proxy fed into the
    # prescore funnel.
    feed_position: Mapped[int | None] = mapped_column(Integer, default=None)
    category_raw: Mapped[str | None] = mapped_column(String, default=None)

    # [{url, width, height, credit, caption}]. Dimensions are feed-declared and
    # NOT verified — see videoability in docs/plan-phase-1.md step 5.
    images: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    embedding_model: Mapped[str | None] = mapped_column(String, default=None)

    # Over title+summary+body. Keys the embedding cache and, per-cluster, the
    # LLM score cache — see services/llm.py.
    content_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)

    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
