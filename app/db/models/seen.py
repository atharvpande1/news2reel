from datetime import datetime

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class SeenFingerprint(Base):
    """Cross-day dedupe history. Cannot be rebuilt from feeds — see CLAUDE.md's
    invariants. A fingerprint stores the high-IDF keyword set plus locality
    for a cluster; matching a new cluster against these (via the same
    similarity function as clustering, wider window) decides developing vs.
    repeat."""

    __tablename__ = "seen_fingerprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    keywords: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    locality: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Snapshot of the content last published for this fingerprint, so a later
    # match can be compared for new material facts.
    content_snapshot: Mapped[str] = mapped_column(String, nullable=False)

    first_published_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_story_id: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
